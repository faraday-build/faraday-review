"""tools/qualification/reproduce.py (GHO-004 P2, item 5): a committed
ledger/readiness must re-derive byte-for-byte from the committed archive.
Vacuously passes when no ledger is committed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from faraday_review import cli
from qualification_support import write_run

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "qualification" / "reproduce.py"
TS = "2026-11-14T00:00:00Z"


def _run(qual: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--qualification-dir", str(qual)],
                          capture_output=True, text=True)


def _commit_ledger(qual: Path, with_readiness: bool = True) -> None:
    archive = qual / "artifacts"
    write_run(archive, "r1")
    (qual / "synthetic-prs.json").write_text("[]\n")
    assert cli.main(["aggregate", "--input-dir", str(archive), "--output",
                     str(qual / "ledger.json"), "--generated-at", TS]) == 0
    if with_readiness:
        assert cli.main(["readiness", "--ledger", str(qual / "ledger.json"), "--synthetic",
                         str(qual / "synthetic-prs.json"), "--generated-at", TS,
                         "--output", str(qual / "readiness.json")]) == 0


def test_no_ledger_passes_vacuously(tmp_path):
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "vacuous" in proc.stderr


def test_committed_repo_state_reproduces():
    proc = _run(ROOT / "docs" / "qualification")
    assert proc.returncode == 0, proc.stderr


def test_matching_ledger_and_readiness_pass(tmp_path):
    _commit_ledger(tmp_path)
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_tampered_ledger_fails(tmp_path):
    _commit_ledger(tmp_path)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(ledger.read_text().replace('"events_read":1', '"events_read":2'))
    assert '"events_read":2' in ledger.read_text()
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "ledger.json" in proc.stderr


def test_archive_change_fails(tmp_path):
    _commit_ledger(tmp_path)
    write_run(tmp_path / "artifacts", "r2", pr=2)
    assert _run(tmp_path).returncode == 1


def test_tampered_readiness_fails(tmp_path):
    _commit_ledger(tmp_path)
    ready = tmp_path / "readiness.json"
    ready.write_text(ready.read_text().replace('"real_pr_count":1', '"real_pr_count":20'))
    assert '"real_pr_count":20' in ready.read_text()
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "readiness.json" in proc.stderr


def test_ledger_without_readiness_checks_ledger_only(tmp_path):
    _commit_ledger(tmp_path, with_readiness=False)
    assert _run(tmp_path).returncode == 0


def test_prior_ledger_continuity_enforced(tmp_path):
    """The base branch's ledger is the prior ledger: an archive shrunk AND a
    ledger regenerated to match it still fails, because the base recorded
    inputs that are gone."""
    _commit_ledger(tmp_path)
    write_run(tmp_path / "artifacts", "r2", pr=2)
    assert cli.main(["aggregate", "--input-dir", str(tmp_path / "artifacts"), "--output",
                     str(tmp_path / "ledger.json"), "--generated-at", TS]) == 0
    (tmp_path / "readiness.json").unlink()
    prior = tmp_path.parent / f"{tmp_path.name}-base-ledger.json"
    prior.write_bytes((tmp_path / "ledger.json").read_bytes())
    for p in (tmp_path / "artifacts" / "r2").iterdir():
        p.unlink()
    assert cli.main(["aggregate", "--input-dir", str(tmp_path / "artifacts"), "--output",
                     str(tmp_path / "ledger.json"), "--generated-at", TS]) == 0
    assert _run(tmp_path).returncode == 0  # self-consistent without the base
    proc = subprocess.run([sys.executable, str(SCRIPT), "--qualification-dir", str(tmp_path),
                           "--prior-ledger", str(prior)], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "missing" in proc.stderr


def test_deleted_ledger_with_prior_ledger_fails(tmp_path):
    """Codex P2 #10 reproduce.py:36: removing ledger, readiness and archive
    together must not pass vacuously when the base branch had a ledger."""
    _commit_ledger(tmp_path)
    prior = tmp_path.parent / f"{tmp_path.name}-base-ledger.json"
    prior.write_bytes((tmp_path / "ledger.json").read_bytes())
    (tmp_path / "ledger.json").unlink()
    (tmp_path / "readiness.json").unlink()
    proc = subprocess.run([sys.executable, str(SCRIPT), "--qualification-dir", str(tmp_path),
                           "--prior-ledger", str(prior)], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "prior ledger" in proc.stderr
