"""Build ReviewTelemetryEvent records. No model cost fields — no model.

Authorization-bearing fields are copied verbatim from the Validator's
decision document via the passthrough list below; telemetry never derives
or overrides them (authorization-boundary guard).

Identity (agent-contracts v0.4.0, review-telemetry-identity.md P1-P4): the
caller passes a fresh `event_id` per emission and, when the platform
execution is known, `execution`. `execution` is never emitted without
`event_id` (P3).
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
    stale_state: str | None,
    duration_seconds: float,
    github_requests_used: int,
    retries: int,
    final_status: str,
    created_at: str,
    failure_mode: str | None = None,
    event_id: str | None = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if execution is not None and event_id is None:
        raise ValueError("execution requires event_id (review-telemetry-identity P3)")
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
            "duration_seconds": duration_seconds,
            "github_requests_used": github_requests_used,
            "retries": retries,
            "final_status": final_status,
            "created_at": created_at,
        }
    )
    if stale_state is not None:
        # None: the run ended before either freshness gate ran (P1).
        event["stale_state"] = stale_state
    if failure_mode is not None:
        event["failure_mode"] = failure_mode
    if event_id is not None:
        event["event_id"] = event_id
    if execution is not None:
        event["execution"] = dict(execution)
    return event
