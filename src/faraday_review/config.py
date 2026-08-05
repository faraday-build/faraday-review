"""Runtime configuration: GitHub API budget and PR-size limits.

The two limits are deliberately distinct (Correction 2):

- exceeding pr_limits escalates as pr_too_large_for_deterministic_classification
- exhausting github_api within pr_limits escalates as github_api_budget_exceeded

A configuration whose budget cannot cover the worst case implied by
pr_limits would guarantee permanent escalation; that must fail at load.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class ReviewConfig:
    github_api: GitHubApiConfig
    pr_limits: PrLimitsConfig

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


def _validated(cfg: ReviewConfig) -> ReviewConfig:
    gh, pr = cfg.github_api, cfg.pr_limits
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
    except TypeError as exc:
        raise ConfigurationError(f"{path}: {exc}") from exc
    return _validated(ReviewConfig(github_api=gh, pr_limits=pr))
