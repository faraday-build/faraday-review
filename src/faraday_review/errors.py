"""Typed errors. Every one maps to a fail-closed outcome, never a pass."""

from __future__ import annotations


class FaradayReviewError(Exception):
    """Base. `reason` is the machine-readable blocking/escalation reason."""

    reason = "internal_error"


class ConfigurationError(FaradayReviewError):
    """Invalid configuration must fail at load, not silently Block every PR."""

    reason = "configuration_invalid"


class BudgetExceededError(FaradayReviewError):
    """GitHub API budget exhausted within pr_limits: an infrastructure
    fault. Page it, do not normalize it (Correction 2)."""

    reason = "github_api_budget_exceeded"


class PrTooLargeError(FaradayReviewError):
    """PR exceeds pr_limits: a real, expected, well-understood escalation,
    distinct from budget exhaustion (Correction 2)."""

    reason = "pr_too_large_for_deterministic_classification"


class GitHubApiError(FaradayReviewError):
    reason = "github_api_failure"


class StaleShaError(FaradayReviewError):
    reason = "evidence_stale"


class PublicationError(FaradayReviewError):
    reason = "publication_failure"
