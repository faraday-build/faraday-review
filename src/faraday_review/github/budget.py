"""GitHub API request budget and retry backoff (Correction 2).

The request counter increments on EVERY call including retries. Exhaustion
raises BudgetExceededError, which maps to github_api_budget_exceeded — an
infrastructure fault, distinct from pr_too_large_for_deterministic_classification.

The jitter RNG is the one permitted source of nondeterminism in the
runtime. It affects timing only and must never reach a decision — the
Validator has no RNG at all, and the determinism harness proves it.
"""

from __future__ import annotations

import random

from ..config import GitHubApiConfig
from ..errors import BudgetExceededError


class RequestBudget:
    def __init__(self, config: GitHubApiConfig) -> None:
        self._config = config
        self.requests_used = 0
        self.retries_used = 0
        self.head_refetches_used = 0

    def spend(self, *, is_retry: bool = False) -> None:
        self.requests_used += 1
        if is_retry:
            self.retries_used += 1
        if self.requests_used > self._config.max_requests_per_run:
            raise BudgetExceededError(
                f"GitHub API budget exhausted: {self.requests_used} > "
                f"max_requests_per_run={self._config.max_requests_per_run}"
            )

    def spend_head_refetch(self) -> None:
        self.head_refetches_used += 1
        if self.head_refetches_used > self._config.max_head_refetches:
            raise BudgetExceededError(
                f"head refetches exhausted: {self.head_refetches_used} > "
                f"max_head_refetches={self._config.max_head_refetches}"
            )

    def backoff_seconds(self, attempt: int, retry_after: float | None = None) -> float:
        """Full jitter: sleep = uniform(0, min(cap, base * 2**attempt)).

        Fixed exponential backoff across a multi-org estate synchronizes
        retries and makes secondary rate limits worse. Retry-After, when
        present and respected, overrides the computed backoff.
        """
        if retry_after is not None and self._config.respect_retry_after:
            return max(0.0, retry_after)
        cap = min(
            self._config.max_backoff_seconds,
            self._config.base_backoff_seconds * (2 ** attempt),
        )
        return random.uniform(0.0, cap)
