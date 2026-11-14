"""Codex r18 F3: the documented qualification sequence in
docs/PHASE_2_INPUTS.md must work from the initialized state (no ledger yet)
and on every later update. The documented aggregate commands are executed
verbatim, with docs/qualification/ remapped to a temporary directory."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

from faraday_review import cli

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "PHASE_2_INPUTS.md"
QUAL = "docs/qualification/"


def _commands(tool: str) -> list[list[str]]:
    text = DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
    out = []
    for block in blocks:
        joined = re.sub(r"\\\n\s*", " ", block)
        for line in joined.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith(f"faraday-review {tool}"):
                out.append(shlex.split(line)[1:])
    return out


def _remap(argv: list[str], qual: Path) -> list[str]:
    return [str(qual / a[len(QUAL):]) if a.startswith(QUAL) else a for a in argv]


def test_documented_sequence_creates_then_updates_the_ledger(tmp_path):
    aggregates = _commands("aggregate")
    assert len(aggregates) >= 2, "document both the first ledger and later updates"
    first, update = aggregates[0], aggregates[1]
    assert "--prior-ledger" not in first
    assert "--prior-ledger" in update
    qual = tmp_path / "qualification"
    (qual / "artifacts").mkdir(parents=True)
    shutil.copy(ROOT / "docs" / "qualification" / "synthetic-prs.json", qual / "synthetic-prs.json")
    assert not (qual / "ledger.json").exists()
    assert cli.main(_remap(first, qual)) == 0
    ledger = json.loads((qual / "ledger.json").read_text())
    assert cli.main(_remap(update, qual)) == 0
    for argv in _commands("readiness"):
        assert cli.main(_remap(argv, qual)) == 0
    assert json.loads((qual / "ledger.json").read_text())["inputs"] == ledger["inputs"]
