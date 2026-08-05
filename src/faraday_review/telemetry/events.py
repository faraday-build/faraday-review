"""Build ReviewTelemetryEvent records. No model cost fields — no model.

Authorization-bearing fields are copied verbatim from the Validator's
decision document via the passthrough list below; telemetry never derives
or overrides them (authorization-boundary guard).
"""

from __future__ import annotations

from typing import Any

_DECISION_PASSTHROUGH = (
    "contract_version",
    "repository",
    "pr_number",
    "base_sha",
    "head_sha",
    "reviewer_version",
    "evidence_class",
    "risk_class",
    "approval_authorized",
    "merge_authorized",
    "founder_review_required",
    "blocking_reasons",
)


def build_run_event(
    *,
    review_id: str,
    fingerprint: str,
    decision: dict[str, Any],
    finding_counts: dict[str, int],
    stale_state: str,
    duration_seconds: float,
    github_requests_used: int,
    retries: int,
    final_status: str,
    created_at: str,
    failure_mode: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {}
    for key in _DECISION_PASSTHROUGH:
        if decision.get(key) is not None:
            event[key] = decision[key]

    event.update(
        {
            "review_id": review_id,
            "fingerprint": fingerprint,
            "deterministic_finding_counts": {
                "info": int(finding_counts.get("info", 0)),
                "warning": int(finding_counts.get("warning", 0)),
                "blocking": int(finding_counts.get("blocking", 0)),
            },
            "stale_state": stale_state,
            "duration_seconds": duration_seconds,
            "github_requests_used": github_requests_used,
            "retries": retries,
            "final_status": final_status,
            "created_at": created_at,
        }
    )
    if failure_mode is not None:
        event["failure_mode"] = failure_mode
    return event
