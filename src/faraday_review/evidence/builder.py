"""Assemble PullRequestReviewEvidence from context plus deterministic
findings. Emits findings only — no authorization semantics here (guard-
enforced). provider_results / required_providers are empty arrays, not
absent keys (Correction 4)."""

from __future__ import annotations

from datetime import datetime, timezone

from .. import CONTRACT_VERSION, REVIEWER_VERSION
from ..models import DeterministicFinding, PullRequestContext, ReviewEvidence


def _finding_sort_key(f: DeterministicFinding) -> tuple:
    return (f.check, f.rule_id, f.path or "", f.line or 0, f.message)


def build_evidence(
    context: PullRequestContext,
    findings: list[DeterministicFinding],
    collected_at: str | None = None,
) -> ReviewEvidence:
    return ReviewEvidence(
        contract_version=CONTRACT_VERSION,
        repository=context.repository,
        pr_number=context.pr_number,
        base_sha=context.base_sha,
        head_sha=context.head_sha,
        reviewer_version=REVIEWER_VERSION,
        evidence_class="deterministic",
        deterministic_findings=sorted(findings, key=_finding_sort_key),
        provider_results=[],
        required_providers=[],
        collected_at=collected_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
