"""GitHub client behavior against a fake transport: retries, Retry-After,
budget accounting, idempotent publication. No network."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from faraday_review.config import GitHubApiConfig
from faraday_review.errors import BudgetExceededError, GitHubApiError
from faraday_review.github.budget import RequestBudget
from faraday_review.github.client import GitHubClient
from faraday_review.github.publisher import publish_summary_comment


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.links: dict = {}
        self.text = json.dumps(self._body)

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, script: list[FakeResponse]):
        self.script = list(script)
        self.headers: dict = {}
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method, url, json=None, params=None, timeout=None):
        assert timeout is not None, "every request must carry a timeout"
        self.calls.append((method, url, json))
        if not self.script:
            raise AssertionError("fake transport exhausted")
        return self.script.pop(0)


def _client(script, **cfg_overrides):
    config = GitHubApiConfig(**cfg_overrides)
    budget = RequestBudget(config)
    session = FakeSession(script)
    return GitHubClient("tok", config, budget, session=session), session, budget


def test_transient_retry_then_success():
    client, session, budget = _client(
        [FakeResponse(502), FakeResponse(200, {"ok": True})]
    )
    client._sleep = lambda s: None
    assert client.get_json("/x") == {"ok": True}
    assert budget.requests_used == 2
    assert budget.retries_used == 1


def test_retry_after_header_is_honored():
    slept: list[float] = []
    client, _, _ = _client([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {}),
    ])
    client._sleep = slept.append
    client.get_json("/x")
    assert slept == [7.0]


def test_permission_failure_is_not_retried():
    client, session, budget = _client([FakeResponse(403, {"message": "Resource not accessible"})])
    client._sleep = lambda s: None
    with pytest.raises(GitHubApiError):
        client.get_json("/x")
    assert budget.requests_used == 1


def test_secondary_rate_limit_is_retryable():
    client, _, budget = _client([
        FakeResponse(403, {"message": "You have exceeded a secondary rate limit"}),
        FakeResponse(200, {}),
    ])
    client._sleep = lambda s: None
    client.get_json("/x")
    assert budget.retries_used == 1


def test_budget_exhaustion_raises_typed_error():
    responses = [FakeResponse(200, {})] * 10
    client, _, _ = _client(responses, max_requests_per_run=121)
    client._sleep = lambda s: None
    client.budget.requests_used = 121  # simulate a spent run
    with pytest.raises(BudgetExceededError):
        client.get_json("/x")


DECISION = {
    "repository": "o/r",
    "pr_number": 5,
    "head_sha": "f" * 40,
    "risk_class": "yellow",
    "approval_authorized": False,
    "merge_authorized": False,
    "founder_review_required": True,
    "blocking_reasons": [],
    "policy_version": "1.0.0",
    "policy_digest": "sha256:" + "1" * 64,
    "evidence_fingerprint": "sha256:" + "2" * 64,
    "reviewer_version": "v0.1.0",
}


def test_comment_created_when_no_marker_exists():
    client, session, _ = _client([
        FakeResponse(200, []),                      # list comments
        FakeResponse(201, {"id": 99}),              # create
    ])
    assert publish_summary_comment(client, "o/r", 5, DECISION) == 99
    assert session.calls[-1][0] == "POST"


def test_comment_updated_in_place_when_marker_exists():
    existing = {"id": 42, "body": "<!-- faraday-review:o/r:5:" + "e" * 40 + " -->\nold"}
    client, session, _ = _client([
        FakeResponse(200, [existing]),              # list comments
        FakeResponse(200, {"id": 42}),              # patch
    ])
    assert publish_summary_comment(client, "o/r", 5, DECISION) == 42
    method, url, body = session.calls[-1]
    assert method == "PATCH" and "/comments/42" in url
    # the updated body carries the new head SHA marker
    assert f"o/r:5:{'f' * 40}" in body["body"]


def test_publisher_never_approves_or_merges():
    client, session, _ = _client([
        FakeResponse(200, []),
        FakeResponse(201, {"id": 1}),
    ])
    publish_summary_comment(client, "o/r", 5, DECISION)
    for method, url, _ in session.calls:
        assert "/reviews" not in url and "/merge" not in url
