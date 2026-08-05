"""Pinned policy digest verification. Absent pin → Block. Mismatch → Block."""

from __future__ import annotations

from pathlib import Path

from ..canonical import bundle_digest

DIGEST_SOURCES = ("cli_flag", "environment", "absent")


def verify_pinned_digest(
    bundle_root: Path, expected: str | None, source: str
) -> tuple[str, list[str]]:
    """Return (actual_digest, reasons)."""
    if source not in DIGEST_SOURCES:
        raise ValueError(f"unknown digest source {source!r}")
    actual = bundle_digest(bundle_root)
    if expected is None or source == "absent":
        return actual, ["policy_digest_unpinned"]
    if expected != actual:
        return actual, ["policy_digest_mismatch"]
    return actual, []
