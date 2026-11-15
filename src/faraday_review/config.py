"""Runtime configuration: GitHub API budget and PR-size limits.

The two limits are deliberately distinct (Correction 2):

- exceeding pr_limits escalates as pr_too_large_for_deterministic_classification
- exhausting github_api within pr_limits escalates as github_api_budget_exceeded

A configuration whose budget cannot cover the worst case implied by
pr_limits would guarantee permanent escalation; that must fail at load.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ConfigurationError


@dataclass(frozen=True)
class GitHubApiConfig:
    max_requests_per_run: int = 120
    max_head_refetches: int = 3
    min_head_refetches: int = 2  # floor: validation + publication
    retry_attempts: int = 4
    base_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 30.0
    respect_retry_after: bool = True
    jitter: str = "full"


@dataclass(frozen=True)
class PrLimitsConfig:
    max_changed_files: int = 400
    max_diff_bytes: int = 5_000_000


# Hard maxima for the required-check wait (S6 F-1). The reusable workflow's
# job timeout-minutes must cover PENDING_TIMEOUT_MAX_SECONDS plus the rest of
# the job (tests/guards/test_shadow_workflow.py pins the relation).
MISSING_GRACE_MAX_SECONDS = 900
PENDING_TIMEOUT_MAX_SECONDS = 1800
POLL_MAX_SECONDS_CAP = 120


@dataclass(frozen=True)
class RequiredChecksWaitConfig:
    """Bounded wait for policy-required checks before evaluation (S6 F-1).

    The shadow job starts on the same pull_request event as the caller's
    required checks, so at first observation they are usually still running.
    shadow-run polls until every required context is final (success,
    failure, error) or a bound expires:

    - a context with no run at all ('missing') gets `missing_grace_seconds`
      to appear; after that it is genuinely missing and classified as today
      (required_check_missing);
    - a context still 'pending' at `pending_timeout_seconds` is NOT
      classified: the run ends failed (required_checks_pending_timeout) and
      never counts as a Block.

    Polling backs off from `poll_initial_seconds`, doubling up to
    `poll_max_seconds`."""

    missing_grace_seconds: int = 300
    pending_timeout_seconds: int = 900
    poll_initial_seconds: float = 10.0
    poll_max_seconds: float = 60.0


@dataclass(frozen=True)
class ReviewConfig:
    github_api: GitHubApiConfig
    pr_limits: PrLimitsConfig
    required_checks_wait: RequiredChecksWaitConfig = field(default_factory=RequiredChecksWaitConfig)

    def worst_case_requests(self) -> int:
        """Worst-case request count implied by pr_limits.

        PR metadata (1) + changed files paginated at 100/page + check runs
        (3) + commit statuses (1) + comment list (3) + check-run create and
        update (2) + comment create/update (1) + head refetches + secret
        scanning alerts (1), all multiplied by retry attempts where a call
        can be retried.
        """
        pages = -(-self.pr_limits.max_changed_files // 100)  # ceil
        base = 1 + pages + 3 + 1 + 3 + 2 + 1 + self.github_api.max_head_refetches + 1
        return base + self.github_api.retry_attempts


def validate_wait(wait: RequiredChecksWaitConfig) -> RequiredChecksWaitConfig:
    """Hard bounds; a value outside them is a configuration error (exit 2),
    never silently clamped."""
    numbers = (wait.missing_grace_seconds, wait.pending_timeout_seconds,
               wait.poll_initial_seconds, wait.poll_max_seconds)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in numbers):
        raise ConfigurationError("required_checks_wait values must be numbers")
    if not 0 <= wait.missing_grace_seconds <= MISSING_GRACE_MAX_SECONDS:
        raise ConfigurationError(
            f"required_checks_wait.missing_grace_seconds must be within 0..{MISSING_GRACE_MAX_SECONDS}")
    if not 0 <= wait.pending_timeout_seconds <= PENDING_TIMEOUT_MAX_SECONDS:
        raise ConfigurationError(
            f"required_checks_wait.pending_timeout_seconds must be within 0..{PENDING_TIMEOUT_MAX_SECONDS}")
    if wait.missing_grace_seconds > wait.pending_timeout_seconds:
        raise ConfigurationError("required_checks_wait.missing_grace_seconds must not exceed "
                                 "pending_timeout_seconds")
    if not 1 <= wait.poll_initial_seconds <= wait.poll_max_seconds <= POLL_MAX_SECONDS_CAP:
        raise ConfigurationError("required_checks_wait requires 1 <= poll_initial_seconds <= "
                                 f"poll_max_seconds <= {POLL_MAX_SECONDS_CAP}")
    return wait


def _validated(cfg: ReviewConfig) -> ReviewConfig:
    gh, pr = cfg.github_api, cfg.pr_limits
    validate_wait(cfg.required_checks_wait)
    if gh.max_head_refetches < gh.min_head_refetches:
        raise ConfigurationError(
            "max_head_refetches "
            f"({gh.max_head_refetches}) < min_head_refetches ({gh.min_head_refetches})"
        )
    if pr.max_changed_files < 1 or pr.max_diff_bytes < 1:
        raise ConfigurationError("pr_limits must be positive")
    worst = cfg.worst_case_requests()
    if gh.max_requests_per_run <= worst:
        raise ConfigurationError(
            f"max_requests_per_run ({gh.max_requests_per_run}) must exceed the "
            f"worst-case request count implied by pr_limits ({worst}); this "
            "configuration would guarantee permanent escalation"
        )
    if gh.retry_attempts < 0 or gh.base_backoff_seconds <= 0 or gh.max_backoff_seconds <= 0:
        raise ConfigurationError("retry/backoff values must be positive")
    if gh.jitter != "full":
        raise ConfigurationError("only full jitter is supported (thundering-herd control)")
    return cfg


def default_config() -> ReviewConfig:
    return _validated(ReviewConfig(github_api=GitHubApiConfig(), pr_limits=PrLimitsConfig()))


def load_config(path: Path) -> ReviewConfig:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}: config root must be a mapping")
    try:
        gh = GitHubApiConfig(**(raw.get("github_api") or {}))
        pr = PrLimitsConfig(**(raw.get("pr_limits") or {}))
        wait = RequiredChecksWaitConfig(**(raw.get("required_checks_wait") or {}))
    except TypeError as exc:
        raise ConfigurationError(f"{path}: {exc}") from exc
    return _validated(ReviewConfig(github_api=gh, pr_limits=pr, required_checks_wait=wait))
