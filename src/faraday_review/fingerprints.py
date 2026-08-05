"""Idempotency fingerprints. Inputs ordered; no timestamp anywhere."""

from __future__ import annotations

from typing import Any

from .canonical import digest


def evidence_fingerprint(evidence_json: dict[str, Any]) -> str:
    """Digest of the full evidence document minus its volatile field."""
    stable = {k: v for k, v in evidence_json.items() if k != "collected_at"}
    return digest(stable)


def run_fingerprint(repository: str, pr_number: int, base_sha: str, head_sha: str,
                    policy_digest: str, reviewer_version: str) -> str:
    """Fingerprint of one review run's identity-defining inputs, in a fixed
    key order via canonical serialization. Never includes a timestamp."""
    return digest(
        {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "policy_digest": policy_digest,
            "pr_number": pr_number,
            "repository": repository,
            "reviewer_version": reviewer_version,
        }
    )


def idempotency_marker(repository: str, pr_number: int, head_sha: str) -> str:
    """HTML marker locating the governed summary comment for this head."""
    return f"<!-- faraday-review:{repository}:{pr_number}:{head_sha} -->"
