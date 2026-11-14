#!/usr/bin/env python3
"""Qualification reproducibility check (GHO-004 P2, item 5).

If <dir>/ledger.json is committed, re-run `faraday-review aggregate` over
<dir>/artifacts with the ledger's own generated_at and founder_node_id and
byte-compare. If <dir>/readiness.json is committed too, re-run `readiness`
over the committed ledger and synthetic list with its own generated_at and
threshold and byte-compare. With --prior-ledger (the base branch's ledger),
aggregate also enforces input continuity against it.

No ledger committed: passes vacuously. No network; outputs go to a private
temporary directory. Exit 0 = reproduces, 1 = differs, 2 = cannot run.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reproduce")
    parser.add_argument("--qualification-dir", default="docs/qualification")
    parser.add_argument("--prior-ledger", default=None)
    args = parser.parse_args(argv)
    qual = Path(args.qualification_dir)
    ledger_path, readiness_path = qual / "ledger.json", qual / "readiness.json"
    if not ledger_path.is_file():
        if readiness_path.exists():
            print("reproduce: readiness.json is committed without ledger.json", file=sys.stderr)
            return 1
        print("reproduce: no committed ledger.json; vacuous pass", file=sys.stderr)
        return 0

    from faraday_review.cli import main as cli

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    failures = []
    with tempfile.TemporaryDirectory(prefix="faraday-reproduce-") as tmp:
        out = Path(tmp) / "ledger.json"
        argv_agg = ["aggregate", "--input-dir", str(qual / "artifacts"), "--output", str(out),
                    "--generated-at", str(ledger.get("generated_at")),
                    "--founder-node-id", str(ledger.get("founder_node_id"))]
        if args.prior_ledger:
            argv_agg += ["--prior-ledger", args.prior_ledger]
        if cli(argv_agg) != 0:
            print("reproduce: aggregate failed over the committed archive", file=sys.stderr)
            return 1
        if out.read_bytes() != ledger_path.read_bytes():
            failures.append("ledger.json")
        if readiness_path.is_file():
            ready = json.loads(readiness_path.read_text(encoding="utf-8"))
            rout = Path(tmp) / "readiness.json"
            argv_rdy = ["readiness", "--ledger", str(ledger_path),
                        "--synthetic", str(qual / "synthetic-prs.json"),
                        "--generated-at", str(ready.get("generated_at")),
                        "--threshold", str(ready.get("gate", {}).get("threshold")),
                        "--output", str(rout)]
            if cli(argv_rdy) != 0:
                print("reproduce: readiness failed over the committed ledger", file=sys.stderr)
                return 1
            if rout.read_bytes() != readiness_path.read_bytes():
                failures.append("readiness.json")
    for name in failures:
        print(f"reproduce: committed {name} does not reproduce from the committed archive",
              file=sys.stderr)
    if not failures:
        print("reproduce: committed qualification outputs reproduce byte-for-byte", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
