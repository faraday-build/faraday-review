#!/usr/bin/env python3
"""Public-identity guard for committed qualification reports (founder
ruling 2026-11-14, O3).

Every repository named in a committed qualification report (ledger.json,
readiness.json, and every collection-manifest-*.json at any depth under the
qualification directory) must be enrolled with visibility "public" in
<dir>/enrollment.json. Repository identity is case-insensitive. A repository
absent from the enrollment fails: nothing is public by default.

Fails closed: an unreadable report or enrollment is an error. Output names
files and counts only, never a repository identity.

Standalone (stdlib only, no package import) so it runs in the guard job.
Exit 0 = clean (or nothing to check), 1 = violation, 2 = cannot check.

Usage:
    python tools/guards/public_identities.py --qualification-dir docs/qualification
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

IDENTITY_KEYS = ("repository", "caller_repo")
IDENTITY_LIST_KEYS = ("repository_spellings",)
REPORTS = ("ledger.json", "readiness.json")
MANIFEST_GLOB = "collection-manifest-*.json"


def named(document) -> set[str]:
    found: set[str] = set()
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in IDENTITY_KEYS and isinstance(value, str):
                    found.add(value.casefold())
                elif key in IDENTITY_LIST_KEYS and isinstance(value, list):
                    found.update(v.casefold() for v in value if isinstance(v, str))
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="public_identities")
    parser.add_argument("--qualification-dir", default="docs/qualification")
    args = parser.parse_args(argv)
    qual = Path(args.qualification_dir)
    files = [qual / name for name in REPORTS if (qual / name).exists()]
    files += sorted(p for p in qual.rglob(MANIFEST_GLOB))
    if not files:
        print("public_identities: no committed qualification report; vacuous pass",
              file=sys.stderr)
        return 0
    try:
        enrollment = _load(qual / "enrollment.json")
        if not isinstance(enrollment, list):
            raise ValueError("enrollment is not a list")
        public = {str(e["repository"]).casefold() for e in enrollment
                  if isinstance(e, dict) and e.get("visibility") == "public"}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"public_identities: cannot read enrollment.json ({type(exc).__name__}); "
              "fail closed", file=sys.stderr)
        return 2
    violations = 0
    for path in files:
        rel = path.relative_to(qual).as_posix()
        try:
            document = _load(path)
        except (OSError, ValueError) as exc:
            print(f"public_identities: cannot read {rel} ({type(exc).__name__}); fail closed",
                  file=sys.stderr)
            return 2
        bad = len(named(document) - public)
        if bad:
            violations += bad
            print(f"public_identities: {rel} names {bad} repository identit"
                  f"{'y' if bad == 1 else 'ies'} not enrolled as public", file=sys.stderr)
    if violations:
        return 1
    print(f"public_identities: {len(files)} file(s) name only public enrolled repositories",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
