"""QA-1 L1 independent reference path tier (pure; no network, no clock).

The rules are not this repository's: they are `reference_path_tiers`,
`a0_path_set` and `a0_path_exclusions` of the ADR-0014 tier policy in
tomrivera-ops/agent-contracts, vendored byte-for-byte under
`reference_policy/` and pinned by sha256 in `reference_policy/VENDORED.json`
(proposed; PR #15). The vendored conformance vectors must all pass
(tests/unit/test_reference_tier.py), so this evaluator and the policy's own
checker (agent-contracts tests/adr-0014-policy-check.py) agree.

The reference tier is qualification evidence only. It never selects a
merge path and never authorizes anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

POLICY_DIR = Path(__file__).resolve().parent / "reference_policy"
POLICY_FILE = "tier-policy.json"
VECTORS_FILE = "reference-tier-vectors.json"
TIER_ORDER = ("A0", "A1", "A2", "A3", "A4")
FLOORS = ("A0", "A3")
UNKNOWN = "unknown"


class ReferencePolicyError(ValueError):
    """The vendored policy is missing, altered or malformed (fail closed)."""


def _glob_re(glob: str) -> re.Pattern[str]:
    """agent-contracts semantics: `**/` = zero or more leading directories,
    `**` = any characters, `*` = any characters except `/`; anchored."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    """The vendored policy, verified against its pin. Returns the compiled
    rules plus provenance (`source`, `source_commit`, `sha256`)."""
    try:
        pin = json.loads((POLICY_DIR / "VENDORED.json").read_text(encoding="utf-8"))
        for name, digest in pin["files"].items():
            if _sha256(POLICY_DIR / name) != digest:
                raise ReferencePolicyError(f"vendored {name} does not match its pin")
        policy = json.loads((POLICY_DIR / POLICY_FILE).read_text(encoding="utf-8"))
        ref = policy["reference_path_tiers"]
        rules = {
            "a3": [_glob_re(g) for g in ref["a3_paths"]],
            "a1": [_glob_re(g) for g in ref["a1_paths"]],
            "a0_in": [_glob_re(g) for g in policy["a0_path_set"]],
            "a0_out": [_glob_re(g) for g in policy["a0_path_exclusions"]],
            "default": ref["default"],
        }
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ReferencePolicyError):
            raise
        raise ReferencePolicyError(f"vendored reference policy unreadable: {exc}") from exc
    if rules["default"] not in TIER_ORDER or rules["default"] in ("A0", "A1"):
        raise ReferencePolicyError("reference default tier must be A2 or higher")
    return {"rules": rules,
            "provenance": {"source": pin["source"], "source_commit": pin["source_commit"],
                           "sha256": pin["files"][POLICY_FILE], "status": pin.get("status")}}


def path_tier(path: str) -> str:
    rules = load_policy()["rules"]
    if any(r.match(path) for r in rules["a3"]):
        return "A3"
    if any(r.match(path) for r in rules["a0_in"]) and not any(r.match(path) for r in rules["a0_out"]):
        return "A0"
    if any(r.match(path) for r in rules["a1"]):
        return "A1"
    return rules["default"]


def pr_tier(paths: Iterable[str], floor: str) -> str:
    """Highest path tier raised to the repository floor. No paths, or an
    unknown floor, is `unknown` (never counts)."""
    paths = list(paths)
    if not paths or floor not in FLOORS:
        return UNKNOWN
    return max([path_tier(p) for p in paths] + [floor], key=TIER_ORDER.index)


def tier_counts(paths: Iterable[str]) -> dict[str, int]:
    counts = {t: 0 for t in TIER_ORDER[:4]}
    for p in paths:
        counts[path_tier(p)] += 1
    return counts


def vectors() -> dict[str, Any]:
    return json.loads((POLICY_DIR / VECTORS_FILE).read_text(encoding="utf-8"))
