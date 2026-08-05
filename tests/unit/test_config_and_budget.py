import pytest

from faraday_review.config import GitHubApiConfig, PrLimitsConfig, ReviewConfig, _validated, default_config
from faraday_review.errors import BudgetExceededError, ConfigurationError
from faraday_review.github.budget import RequestBudget


def test_default_config_valid():
    cfg = default_config()
    assert cfg.github_api.max_requests_per_run > cfg.worst_case_requests()


def test_budget_below_worst_case_fails_at_load():
    """A configuration that guarantees permanent escalation must fail at
    load, not silently Block every PR (Correction 2)."""
    with pytest.raises(ConfigurationError):
        _validated(
            ReviewConfig(
                github_api=GitHubApiConfig(max_requests_per_run=10),
                pr_limits=PrLimitsConfig(max_changed_files=400),
            )
        )


def test_refetch_floor_enforced():
    with pytest.raises(ConfigurationError):
        _validated(
            ReviewConfig(
                github_api=GitHubApiConfig(max_head_refetches=1, min_head_refetches=2),
                pr_limits=PrLimitsConfig(),
            )
        )


def test_budget_exhaustion_is_typed_and_distinct():
    budget = RequestBudget(GitHubApiConfig(max_requests_per_run=120))
    for _ in range(120):
        budget.spend()
    with pytest.raises(BudgetExceededError) as exc:
        budget.spend()
    assert exc.value.reason == "github_api_budget_exceeded"
    # Distinct from the PR-size escalation by construction:
    from faraday_review.errors import PrTooLargeError

    assert PrTooLargeError.reason != BudgetExceededError.reason


def test_retry_after_overrides_computed_backoff():
    budget = RequestBudget(GitHubApiConfig())
    assert budget.backoff_seconds(0, retry_after=17.0) == 17.0


def test_full_jitter_bounded_and_capped():
    budget = RequestBudget(GitHubApiConfig(base_backoff_seconds=2, max_backoff_seconds=30))
    for attempt in range(8):
        for _ in range(50):
            s = budget.backoff_seconds(attempt)
            assert 0.0 <= s <= 30.0


def test_retries_counted_separately():
    budget = RequestBudget(GitHubApiConfig())
    budget.spend()
    budget.spend(is_retry=True)
    assert budget.requests_used == 2
    assert budget.retries_used == 1
