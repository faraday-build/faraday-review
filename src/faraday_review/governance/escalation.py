"""Founder-review requirement per risk class under a pinned policy bundle.

Used by qualification readiness as the *truth* for escalation accuracy
(founder ruling 2026-11-14, O2): a PR's adjudicated class requires founder
review exactly when the policy it was evaluated under says so for that
class. This module only reads policy data; it authorizes nothing.

`RELEASED_REVIEW_REQUIREMENTS` pins the mapping for each governance-released
digest so that readiness (and `tools/qualification/reproduce.py`) never
needs the bundle at hand. A test derives it from the released bundle with
`review_requirements_from_bundle` and fails if the two ever differ. A digest
absent from the table has no known mapping, and readiness reports its PRs as
`unknown` (fail closed).
"""

from __future__ import annotations

from pathlib import Path

from ..canonical import bundle_digest
from .policy_loader import load_bundle

RISK_CLASSES = ("green", "yellow", "red", "block")

# governance v1.1.0 (preflight.RELEASED_POLICY_DIGEST), approval-policy.yaml risk_classes.
RELEASED_REVIEW_REQUIREMENTS: dict[str, dict[str, bool]] = {
    "sha256:32eeb3e07271fff267908bc428d77477caa403d9e0f8b0a0244d8c26aec73e12": {
        "green": False, "yellow": True, "red": True, "block": True,
    },
}


def review_requirements_from_bundle(root: Path) -> tuple[str, dict[str, bool]] | None:
    """(digest, {class: founder review required}) from a usable bundle, else
    None. Any load defect means no mapping, never a permissive default."""
    bundle, reasons = load_bundle(root)
    if bundle is None or reasons:
        return None
    classes = bundle.approval_policy["risk_classes"]
    return bundle_digest(root), {c: bool(classes[c]["founder_review_required"])
                                 for c in RISK_CLASSES}
