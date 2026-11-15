"""Observe the states of policy-required check contexts for a head SHA."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..config import RequiredChecksWaitConfig
from ..errors import BudgetExceededError, GitHubApiError, RequiredChecksPendingTimeout
from ..evidence.freshness import normalize_sha
from ..models import ObservedCheck
from .client import GitHubClient

_SUCCESS_CONCLUSIONS = frozenset({"success"})
_FAILURE_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required", "stale"})
FINAL_STATES = frozenset({"success", "failure", "error"})
# One poll: PR head re-check + check-runs page + commit statuses.
POLL_REQUESTS = 3


def observe_required_checks(
    client: GitHubClient, repository: str, head_sha: str, required_contexts: list[str]
) -> list[ObservedCheck]:
    # The check-runs endpoint wraps its list: {"total_count": n, "check_runs": [...]}
    # (S6 live smoke, 2026-11-15). A short listing fails closed, never "missing".
    check_runs, total = client.paginate_key_counted(
        f"/repos/{repository}/commits/{head_sha}/check-runs", "check_runs")
    if total is not None and len(check_runs) < total:
        raise GitHubApiError(
            f"check-runs listing for {head_sha} truncated ({len(check_runs)} of {total})")
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


@dataclass(frozen=True)
class CheckWaitResult:
    """What the bounded wait ended with. `outcome`:

    - "final": every required context is success/failure/error, or one has
      failed (which ends the wait at once: the result is a Block anyway);
    - "missing_after_grace": the only non-final contexts never got a run
      within the grace period; they are genuinely missing;
    - "head_moved": the PR head changed while waiting; `head_sha_now` is the
      new head and the caller ends the run as stale (never classifies the
      old head as if it were current)."""

    observed: list[ObservedCheck]
    outcome: str
    polls: int
    waited_seconds: float
    head_sha_now: str | None = None


def _current_head(client: GitHubClient, repository: str, pr_number: int) -> str:
    """The PR head right now. Spends ordinary request budget, never a
    head-refetch slot (those are reserved for the two freshness gates)."""
    pr = client.get_json(f"/repos/{repository}/pulls/{pr_number}")
    sha = normalize_sha(str(pr["head"]["sha"]))
    if sha is None:
        raise GitHubApiError(f"API returned non-normalizable head SHA for {repository}#{pr_number}")
    return sha


def wait_for_required_checks(
    client: GitHubClient,
    repository: str,
    pr_number: int,
    head_sha: str,
    required_contexts: list[str],
    wait: RequiredChecksWaitConfig,
    *,
    max_requests: int,
    reserve_requests: int,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> CheckWaitResult:
    """Observe the required contexts until they are final or a bound
    expires (S6 F-1). The first observation costs exactly what a single
    `observe_required_checks` call does, so a run whose checks are already
    final behaves as before.

    Fails closed:
    - pending at `pending_timeout_seconds` -> RequiredChecksPendingTimeout;
    - another poll would leave fewer than `reserve_requests` of
      `max_requests` for the rest of the run -> BudgetExceededError
      (github_api_budget_exceeded), raised before the poll is spent.
    """
    contexts = sorted(required_contexts)
    start = clock()
    interval = float(wait.poll_initial_seconds)
    polls = 0
    while True:
        observed = observe_required_checks(client, repository, head_sha, contexts)
        polls += 1
        elapsed = clock() - start
        states = [c.state for c in observed]
        if any(s in ("failure", "error") for s in states) or all(s in FINAL_STATES for s in states):
            return CheckWaitResult(observed, "final", polls, elapsed)
        pending = [c.context for c in observed if c.state not in FINAL_STATES and c.state != "missing"]
        if pending:
            if elapsed >= wait.pending_timeout_seconds:
                raise RequiredChecksPendingTimeout(
                    f"{len(pending)} required check(s) still pending after "
                    f"{wait.pending_timeout_seconds}s: {', '.join(pending)}; run not classified")
            deadline = wait.pending_timeout_seconds
        else:
            # Only 'missing' contexts remain.
            if elapsed >= wait.missing_grace_seconds:
                return CheckWaitResult(observed, "missing_after_grace", polls, elapsed)
            deadline = wait.missing_grace_seconds
        if client.budget.requests_used + POLL_REQUESTS + reserve_requests > max_requests:
            raise BudgetExceededError(
                f"GitHub API budget: another required-check poll ({POLL_REQUESTS} requests) would "
                f"leave fewer than {reserve_requests} of max_requests_per_run={max_requests} "
                f"for the rest of the run (used {client.budget.requests_used})")
        sleeper(max(1.0, min(interval, deadline - elapsed)))
        interval = min(interval * 2, float(wait.poll_max_seconds))
        head_now = _current_head(client, repository, pr_number)
        if head_now != head_sha:
            return CheckWaitResult(observed, "head_moved", polls, clock() - start, head_now)
