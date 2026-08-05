"""Throughput metrics over the append-only telemetry log.

Derivable from telemetry alone — this is the primary input to
docs/PHASE_2_INPUTS.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _percentile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


def compute_throughput(events: list[dict[str, Any]]) -> dict[str, Any]:
    requested = [e for e in events if e.get("founder_review_required")]
    completed = [e for e in requested if e.get("founder_classification")]

    latencies = sorted(
        (_parse(e["founder_classification_at"]) - _parse(e["created_at"])).total_seconds()
        for e in completed
        if e.get("founder_classification_at") and e.get("created_at")
    )

    distribution = {c: 0 for c in ("green", "yellow", "red", "block")}
    for event in events:
        cls = event.get("risk_class")
        if cls in distribution:
            distribution[cls] += 1

    yellow_causes: dict[str, int] = {}
    for event in events:
        if event.get("risk_class") == "yellow":
            for reason in event.get("blocking_reasons", []) or ["path_classification"]:
                yellow_causes[reason] = yellow_causes.get(reason, 0) + 1

    false_positive_candidates = sorted(
        {
            f"{e['repository']}#{e['pr_number']}"
            for e in events
            if e.get("risk_class") in ("yellow", "red")
            and e.get("founder_classification") == "green"
        }
    )

    return {
        "founder_reviews_requested": len(requested),
        "founder_reviews_completed": len(completed),
        "median_seconds_to_adjudication": _percentile(latencies, 0.5),
        "p90_seconds_to_adjudication": _percentile(latencies, 0.9),
        "risk_class_distribution": distribution,
        "top_yellow_causes": sorted(
            yellow_causes.items(), key=lambda kv: (-kv[1], kv[0])
        ),
        "false_positive_candidates": false_positive_candidates,
        "pr_too_large_escalations": sum(
            1
            for e in events
            if "pr_too_large_for_deterministic_classification" in (e.get("blocking_reasons") or [])
        ),
        "api_budget_exceeded_escalations": sum(
            1
            for e in events
            if "github_api_budget_exceeded" in (e.get("blocking_reasons") or [])
        ),
        "agreement_rate": (
            sum(1 for e in completed if e.get("agreement")) / len(completed)
            if completed
            else None
        ),
    }
