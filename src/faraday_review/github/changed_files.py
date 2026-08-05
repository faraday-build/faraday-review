"""Changed-file collection with pr_limits awareness.

Collection itself never raises PrTooLargeError — the limit evaluation
happens in deterministic/pr_limits.py so that an oversized PR produces a
well-formed context and a pr_limits Block finding, not a crash.
"""

from __future__ import annotations

from ..models import ChangedFile
from .client import GitHubClient


def collect_changed_files(
    client: GitHubClient, repository: str, pr_number: int
) -> tuple[list[ChangedFile], int, int]:
    """Return (files, total_count, approximate_diff_bytes)."""
    raw = client.paginate(f"/repos/{repository}/pulls/{pr_number}/files")
    files: list[ChangedFile] = []
    diff_bytes = 0
    for entry in raw:
        patch = entry.get("patch") or ""
        diff_bytes += len(patch.encode("utf-8"))
        files.append(
            ChangedFile(
                path=str(entry["filename"]),
                status=str(entry["status"]),
                additions=int(entry.get("additions", 0)),
                deletions=int(entry.get("deletions", 0)),
                previous_path=entry.get("previous_filename"),
            )
        )
    return files, len(files), diff_bytes
