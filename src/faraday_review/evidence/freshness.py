"""Exact-SHA binding and freshness.

Full 40-hex, lowercase-normalized. Short SHAs are rejected, never
prefix-compared. Freshness is equality between the SHA the evidence was
built for and the SHA observed at validation/publication time.
"""

from __future__ import annotations

import re

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def normalize_sha(sha: str) -> str | None:
    """Lowercase-normalize; None if not a full 40-hex SHA."""
    candidate = sha.strip().lower()
    return candidate if _FULL_SHA.match(candidate) else None


def is_fresh(evidence_head_sha: str, observed_head_sha: str) -> bool:
    a, b = normalize_sha(evidence_head_sha), normalize_sha(observed_head_sha)
    if a is None or b is None:
        return False  # malformed SHAs are never fresh — fail closed
    return a == b
