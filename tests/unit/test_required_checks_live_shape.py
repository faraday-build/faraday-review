"""S6 synthetic smoke (2026-11-15) found that the first live shadow run failed
with `paginate expected a list at /repos/.../commits/<sha>/check-runs`.
GitHub's check-runs endpoint returns an object
({"total_count": n, "check_runs": [...]}), not a bare list, and no test
exercised the real response shape. These tests use the real GitHubClient
against the real response shapes."""

from __future__ import annotations

from fake_github import FakeSession

from faraday_review.config import GitHubApiConfig
from faraday_review.github.budget import RequestBudget
from faraday_review.github.checks import observe_required_checks
from faraday_review.github.client import GitHubClient
from faraday_review.models import ObservedCheck

REPO = "tomrivera-ops/ecosystem-audit"
SHA = "9988c1d9072a7df5ba0baf441729b62be61ae044"
RUNS = f"/repos/{REPO}/commits/{SHA}/check-runs"
STATUS = f"/repos/{REPO}/commits/{SHA}/status"


def _client(routes) -> tuple[GitHubClient, FakeSession]:
    session = FakeSession(routes)
    config = GitHubApiConfig()
    return GitHubClient("test-token-not-a-secret", config, RequestBudget(config),
                        session=session, sleeper=lambda _s: None), session


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_check_runs_object_shape_is_read():
    client, _ = _client({
        RUNS: (200, {"total_count": 3, "check_runs": [
            _run("required-checks"),
            _run("lint", conclusion="failure"),
            _run("slow", status="in_progress", conclusion=None),
        ]}),
        STATUS: (200, {"state": "pending", "statuses": [{"context": "legacy", "state": "success"}]}),
    })
    observed = observe_required_checks(client, REPO, SHA,
                                       ["required-checks", "lint", "slow", "legacy", "absent"])
    assert observed == [
        ObservedCheck("absent", "missing"),
        ObservedCheck("legacy", "success"),
        ObservedCheck("lint", "failure"),
        ObservedCheck("required-checks", "success"),
        ObservedCheck("slow", "pending"),
    ]


def test_check_runs_follow_pagination_and_keep_the_best_rerun():
    page2 = "https://api.github.com/repos/" + REPO + f"/commits/{SHA}/check-runs-page-2"
    client, session = _client({
        RUNS: (200, {"total_count": 2, "check_runs": [_run("required-checks", conclusion="failure")]},
               {"next": {"url": page2}}),
        f"/repos/{REPO}/commits/{SHA}/check-runs-page-2":
            (200, {"total_count": 2, "check_runs": [_run("required-checks")]}),
        STATUS: (200, {"state": "success", "statuses": []}),
    })
    assert observe_required_checks(client, REPO, SHA, ["required-checks"]) == [
        ObservedCheck("required-checks", "success")]
    assert [p for _, p in session.calls].count(RUNS) == 1


def test_bare_list_response_still_fails_closed():
    # A malformed (non-object) response is an API failure, never "no checks".
    import pytest

    from faraday_review.errors import GitHubApiError

    client, _ = _client({RUNS: (200, [_run("required-checks")]),
                         STATUS: (200, {"state": "success", "statuses": []})})
    with pytest.raises(GitHubApiError):
        observe_required_checks(client, REPO, SHA, ["required-checks"])


def test_truncated_check_runs_listing_fails_closed():
    import pytest

    from faraday_review.errors import GitHubApiError

    client, _ = _client({RUNS: (200, {"total_count": 5, "check_runs": [_run("required-checks")]}),
                         STATUS: (200, {"state": "success", "statuses": []})})
    with pytest.raises(GitHubApiError):
        observe_required_checks(client, REPO, SHA, ["required-checks"])


def test_listing_one_short_of_total_fails_closed():
    import pytest

    from faraday_review.errors import GitHubApiError

    client, _ = _client({RUNS: (200, {"total_count": 2, "check_runs": [_run("required-checks")]}),
                         STATUS: (200, {"state": "success", "statuses": []})})
    with pytest.raises(GitHubApiError):
        observe_required_checks(client, REPO, SHA, ["required-checks"])
