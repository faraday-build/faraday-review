"""PR-size limits — a real, expected escalation, distinct from API budget
exhaustion (Correction 2)."""

from __future__ import annotations

from ..config import PrLimitsConfig
from ..models import DeterministicFinding, PullRequestContext


def evaluate_limits(context: PullRequestContext, limits: PrLimitsConfig) -> list[DeterministicFinding]:
    findings: list[DeterministicFinding] = []
    count = context.changed_file_count
    if count is not None and count > limits.max_changed_files:
        findings.append(
            DeterministicFinding(
                rule_id="max-changed-files",
                check="pr_limits",
                severity="blocking",
                message=f"changed files {count} exceed max_changed_files {limits.max_changed_files}",
            )
        )
    size = context.diff_bytes
    if size is not None and size > limits.max_diff_bytes:
        findings.append(
            DeterministicFinding(
                rule_id="max-diff-bytes",
                check="pr_limits",
                severity="blocking",
                message=f"diff size {size} exceeds max_diff_bytes {limits.max_diff_bytes}",
            )
        )
    return findings
