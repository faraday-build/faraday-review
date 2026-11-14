"""Decision assembly. THE ONLY MODULE TREE ALLOWED TO PRODUCE
approval_authorized / merge_authorized / founder_review_required /
risk_class — enforced by tools/guards/authorization_boundary.py.

Determinism contract: no clock reads except the injected evaluated_at, no
RNG, no set iteration reaching output, blocking_reasons is a sorted list.
"""

from __future__ import annotations

from typing import Any

from .. import CONTRACT_VERSION, REVIEWER_VERSION, SUPPORTED_CONTRACT_MAJOR, SUPPORTED_CONTRACT_MINOR
from ..models import CLASS_ORDER, DeterministicFinding

# Belt-and-braces runtime allowlist, independent of whichever schema
# validated the payload (an N-1 bundle could carry different membership).
# Fail closed. No default, no coercion, no lowercase normalization.
ACCEPTED_EVIDENCE_CLASSES = frozenset({"deterministic", "mixed"})

_REASON_BY_RULE = {
    ("required_checks", "required-check-failing"): "required_check_failing",
    ("required_checks", "required-check-missing"): "required_check_missing",
}
_REASON_BY_CHECK = {
    "secret_scan": "secret_detected",
    "pr_limits": "pr_too_large_for_deterministic_classification",
    "freshness": "evidence_stale",
}

INFRASTRUCTURE_REASONS = frozenset({"github_api_budget_exceeded"})


def _sorted_unique(reasons: list[str]) -> list[str]:
    # dict.fromkeys + sorted: deterministic dedup with no set iteration.
    return sorted(dict.fromkeys(reasons))


def reason_for_finding(finding: DeterministicFinding) -> str:
    by_rule = _REASON_BY_RULE.get((finding.check, finding.rule_id))
    if by_rule:
        return by_rule
    by_check = _REASON_BY_CHECK.get(finding.check)
    if by_check:
        return by_check
    # A blocking finding from an unrecognized check: fail closed.
    return "evidence_malformed"


def evidence_class_reasons(evidence_class: str | None, deterministic_only: bool) -> list[str]:
    if evidence_class not in ACCEPTED_EVIDENCE_CLASSES:
        return ["unknown_evidence_class"]
    if deterministic_only and evidence_class != "deterministic":
        # Evidence claims provider results in a phase with no providers:
        # malformed relative to the pinned policy.
        return ["evidence_malformed"]
    return []


def contract_version_reasons(contract_version: str | None) -> tuple[list[str], int | None]:
    """Ruling 16: accept contract minor N and N-1; record which."""
    if not contract_version or not contract_version.startswith("v"):
        return ["contract_version_unsupported"], None
    parts = contract_version[1:].split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ["contract_version_unsupported"], None
    major, minor = int(parts[0]), int(parts[1])
    if major != SUPPORTED_CONTRACT_MAJOR or minor not in (
        SUPPORTED_CONTRACT_MINOR,
        SUPPORTED_CONTRACT_MINOR - 1,
    ):
        return ["contract_version_unsupported"], None
    return [], minor


def assemble_decision(
    *,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    path_class: str,
    reasons: list[str],
    policy: dict[str, Any] | None,
    policy_version: str | None,
    policy_digest: str,
    digest_source: str,
    evaluated_contract_minor: int | None,
    evidence_class: str | None,
    evidence_fingerprint: str,
    evaluated_at: str,
    reviewer_version: str = REVIEWER_VERSION,
) -> dict[str, Any]:
    blocking_reasons = _sorted_unique(reasons)

    if blocking_reasons:
        risk_class = "block"
    elif path_class in CLASS_ORDER:
        risk_class = path_class
    else:
        risk_class = "block"
        blocking_reasons = ["evidence_malformed"]

    # Authorization posture comes from the pinned policy; with no usable
    # policy every class fails closed.
    if policy is not None and not blocking_reasons:
        class_policy = policy["risk_classes"][risk_class]
        approval_authorized = bool(class_policy["approval_authorized"])
        merge_authorized = bool(class_policy["merge_authorized"])
        founder_review_required = bool(class_policy["founder_review_required"])
    else:
        approval_authorized = False
        merge_authorized = False
        founder_review_required = True

    decision = {
        "contract_version": CONTRACT_VERSION,
        "repository": repository,
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "risk_class": risk_class,
        "approval_authorized": approval_authorized,
        "merge_authorized": merge_authorized,
        "founder_review_required": founder_review_required,
        "blocking_reasons": blocking_reasons,
        "policy_version": policy_version or "0.0.0",
        "policy_digest": policy_digest,
        "expected_policy_digest_source": digest_source,
        "evaluated_contract_minor": (
            evaluated_contract_minor if evaluated_contract_minor is not None else 0
        ),
        "reviewer_version": reviewer_version,
        "evidence_fingerprint": evidence_fingerprint,
        "evaluated_at": evaluated_at,
    }
    if evidence_class in ACCEPTED_EVIDENCE_CLASSES:
        decision["evidence_class"] = evidence_class
    return decision


def assemble_merge_authorization(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "repository": decision["repository"],
        "pr_number": decision["pr_number"],
        "head_sha": decision["head_sha"],
        "merge_authorized": bool(decision["merge_authorized"]),
        "authorized_by": "faraday-validator",
        "risk_class": decision["risk_class"],
        "policy_digest": decision["policy_digest"],
        "rationale": (
            "Phase 1: merge authorization is disabled for every risk class; "
            "founder review required for all non-green outcomes."
        ),
        "evaluated_at": decision["evaluated_at"],
    }


def assemble_founder_escalation(decision: dict[str, Any]) -> dict[str, Any] | None:
    if not decision["founder_review_required"]:
        return None
    reasons = list(decision["blocking_reasons"]) or [f"risk_class_{decision['risk_class']}"]
    return {
        "contract_version": CONTRACT_VERSION,
        "repository": decision["repository"],
        "pr_number": decision["pr_number"],
        "head_sha": decision["head_sha"],
        "reasons": _sorted_unique(reasons),
        "risk_class": decision["risk_class"],
        "evidence_fingerprint": decision["evidence_fingerprint"],
        "escalated_at": decision["evaluated_at"],
    }


def failed_run_posture() -> dict[str, Any]:
    """Authorization posture for a shadow run that ended before the
    Validator produced a decision (GHO-004 P1). Telemetry consumers copy it
    verbatim, exactly as they copy a decision: nothing is authorized and
    founder review is required. A fresh dict per call."""
    return {
        "contract_version": CONTRACT_VERSION,
        "approval_authorized": False,
        "merge_authorized": False,
        "founder_review_required": True,
    }
