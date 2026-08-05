"""Collect PullRequestContext from the GitHub API."""

from __future__ import annotations

from datetime import datetime, timezone

from .. import CONTRACT_VERSION
from ..errors import GitHubApiError
from ..evidence.freshness import normalize_sha
from ..models import ObservedCheck, PullRequestContext
from .changed_files import collect_changed_files
from .checks import observe_required_checks
from .client import GitHubClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch_head_sha(client: GitHubClient, repository: str, pr_number: int) -> str:
    """Refetch the PR head SHA — spends a head-refetch budget slot."""
    client.budget.spend_head_refetch()
    pr = client.get_json(f"/repos/{repository}/pulls/{pr_number}")
    sha = normalize_sha(str(pr["head"]["sha"]))
    if sha is None:
        raise GitHubApiError(f"API returned non-normalizable head SHA for {repository}#{pr_number}")
    return sha


def collect_context(
    client: GitHubClient,
    repository: str,
    pr_number: int,
    required_check_contexts: list[str],
) -> PullRequestContext:
    pr = client.get_json(f"/repos/{repository}/pulls/{pr_number}")
    base_sha = normalize_sha(str(pr["base"]["sha"]))
    head_sha = normalize_sha(str(pr["head"]["sha"]))
    if base_sha is None or head_sha is None:
        raise GitHubApiError("PR SHAs are not full 40-hex values")

    changed_files, total_count, diff_bytes = collect_changed_files(client, repository, pr_number)
    observed: list[ObservedCheck] = observe_required_checks(
        client, repository, head_sha, required_check_contexts
    )

    return PullRequestContext(
        contract_version=CONTRACT_VERSION,
        repository=repository,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        base_ref=str(pr["base"]["ref"]),
        head_ref=str(pr["head"]["ref"]),
        draft=bool(pr.get("draft", False)),
        changed_file_count=total_count,
        diff_bytes=diff_bytes,
        changed_files=changed_files,
        required_checks_observed=observed,
        collected_at=_now_iso(),
    )
