"""Observe the states of policy-required check contexts for a head SHA."""

from __future__ import annotations

from ..models import ObservedCheck
from .client import GitHubClient

_SUCCESS_CONCLUSIONS = frozenset({"success"})
_FAILURE_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required", "stale"})


def observe_required_checks(
    client: GitHubClient, repository: str, head_sha: str, required_contexts: list[str]
) -> list[ObservedCheck]:
    check_runs = client.paginate(f"/repos/{repository}/commits/{head_sha}/check-runs")
    statuses = client.get_json(f"/repos/{repository}/commits/{head_sha}/status")

    states: dict[str, str] = {}
    for run in check_runs:
        name = str(run.get("name", ""))
        if run.get("status") != "completed":
            state = "pending"
        elif run.get("conclusion") in _SUCCESS_CONCLUSIONS:
            state = "success"
        elif run.get("conclusion") in _FAILURE_CONCLUSIONS:
            state = "failure"
        else:
            state = "error"
        # A re-run supersedes; keep the most successful state per context so
        # a green rerun after a red run does not fail closed spuriously.
        rank = {"success": 3, "pending": 2, "failure": 1, "error": 0}
        if name not in states or rank[state] > rank[states[name]]:
            states[name] = state
    for status in statuses.get("statuses", []):
        name = str(status.get("context", ""))
        mapped = {"success": "success", "pending": "pending", "failure": "failure"}.get(
            str(status.get("state")), "error"
        )
        states.setdefault(name, mapped)

    return [
        ObservedCheck(context=ctx, state=states.get(ctx, "missing"))
        for ctx in sorted(required_contexts)
    ]
