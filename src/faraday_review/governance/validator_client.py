"""`faraday-validator evaluate-pr` — deterministic evaluation pipeline.

No model calls, no network, no hidden policy, fail closed. Deterministic
for identical inputs: the injected clock feeds `evaluated_at` only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..canonical import digest
from ..deterministic.protected_paths import PathClassifier
from ..deterministic.required_checks import evaluate_checks
from ..evidence.freshness import normalize_sha
from ..evidence.schema_validation import validation_errors
from ..fingerprints import evidence_fingerprint
from ..models import DeterministicFinding, ObservedCheck
from . import authorization
from .digest import verify_pinned_digest
from .policy_loader import load_bundle


@dataclass(frozen=True)
class EvaluationResult:
    decision: dict[str, Any]
    merge_authorization: dict[str, Any]
    founder_escalation: dict[str, Any] | None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_document(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _normalize_shas(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    for key in ("base_sha", "head_sha"):
        value = out.get(key)
        if isinstance(value, str):
            normalized = normalize_sha(value)
            # Full-SHA mixed case is normalized; anything else is left as-is
            # so schema validation rejects it (short SHAs are never accepted).
            out[key] = normalized if normalized is not None else value
    return out


def evaluate(
    context_path: Path,
    evidence_path: Path,
    bundle_root: Path,
    expected_digest: str | None,
    digest_source: str,
    infrastructure_failure: str | None = None,
    evaluated_at: str | None = None,
) -> EvaluationResult:
    reasons: list[str] = []

    # Policy bundle and pin.
    bundle, policy_reasons = load_bundle(bundle_root)
    reasons.extend(policy_reasons)
    # A missing bundle directory digests as the empty manifest, which can
    # never equal a pinned digest — the mismatch Block composes with
    # policy_missing from the loader.
    actual_digest, digest_reasons = verify_pinned_digest(bundle_root, expected_digest, digest_source)
    reasons.extend(digest_reasons)

    # Context is the anchor document: without it there is no PR to decide
    # about, so an unreadable context is an invocation error, not a decision.
    context_doc = _read_document(context_path)
    if context_doc is None:
        raise ValueError(f"context unreadable: {context_path}")
    context_doc = _normalize_shas(context_doc)
    context_errors = validation_errors("pull-request-context", context_doc)
    if context_errors:
        raise ValueError(f"context invalid: {context_errors[0]}")

    evidence_doc = _read_document(evidence_path)
    if evidence_doc is None:
        evidence_doc = {}
        reasons.append("evidence_malformed")
        fingerprint = digest({})
    else:
        evidence_doc = _normalize_shas(evidence_doc)
        fingerprint = evidence_fingerprint(evidence_doc)
        if validation_errors("pull-request-review-evidence", evidence_doc):
            reasons.append("evidence_malformed")

    # Cross-checks: evidence must bind to this exact context.
    if evidence_doc:
        for key in ("repository", "pr_number", "base_sha"):
            if evidence_doc.get(key) != context_doc.get(key):
                reasons.append("evidence_malformed")
                break
        evidence_head = evidence_doc.get("head_sha")
        if isinstance(evidence_head, str) and evidence_head != context_doc["head_sha"]:
            reasons.append("evidence_stale")

    # Contract version (Ruling 16) and evidence class (belt-and-braces).
    version_reasons, evaluated_minor = authorization.contract_version_reasons(
        evidence_doc.get("contract_version") if evidence_doc else None
    )
    reasons.extend(version_reasons)
    deterministic_only = bool(
        bundle.provider_requirements.get("deterministic_only", True) if bundle else True
    )
    reasons.extend(
        authorization.evidence_class_reasons(evidence_doc.get("evidence_class"), deterministic_only)
    )

    # Blocking findings carried in evidence (secret_scan, pr_limits, ...).
    for raw in evidence_doc.get("deterministic_findings", []) or []:
        if not isinstance(raw, dict) or raw.get("severity") != "blocking":
            continue
        finding = DeterministicFinding(
            rule_id=str(raw.get("rule_id", "unknown")),
            check=str(raw.get("check", "unknown")),
            severity="blocking",
            message=str(raw.get("message", "")),
        )
        reasons.append(authorization.reason_for_finding(finding))

    # Required checks, evaluated from the observed states in context.
    observed = [
        ObservedCheck(context=str(c["context"]), state=str(c["state"]))
        for c in context_doc.get("required_checks_observed", [])
    ]
    for finding in evaluate_checks(observed):
        reasons.append(authorization.reason_for_finding(finding))

    # Infrastructure failure injected by the runner (Correction 2: distinct
    # from pr_too_large, which arrives as a pr_limits finding).
    if infrastructure_failure is not None:
        if infrastructure_failure not in authorization.INFRASTRUCTURE_REASONS:
            raise ValueError(f"unknown infrastructure failure {infrastructure_failure!r}")
        reasons.append(infrastructure_failure)

    # Path classification from the pinned policy.
    if bundle is not None:
        classifier = PathClassifier(
            classes=bundle.protected_paths["classes"],
            default_class=bundle.protected_paths["default_class"],
        )
        changed_paths = [
            str(f["path"]) for f in context_doc.get("changed_files", []) if isinstance(f, dict)
        ]
        path_class, _path_findings = classifier.classify(changed_paths)
    else:
        path_class = "yellow"

    decision = authorization.assemble_decision(
        repository=context_doc["repository"],
        pr_number=context_doc["pr_number"],
        base_sha=context_doc["base_sha"],
        head_sha=context_doc["head_sha"],
        path_class=path_class,
        reasons=reasons,
        policy=bundle.approval_policy if bundle else None,
        policy_version=bundle.policy_version if bundle else None,
        policy_digest=actual_digest,
        digest_source=digest_source,
        evaluated_contract_minor=evaluated_minor,
        evidence_class=evidence_doc.get("evidence_class") if evidence_doc else None,
        evidence_fingerprint=fingerprint,
        evaluated_at=evaluated_at or _utc_now_iso(),
    )
    return EvaluationResult(
        decision=decision,
        merge_authorization=authorization.assemble_merge_authorization(decision),
        founder_escalation=authorization.assemble_founder_escalation(decision),
    )
