"""Founder ruling 2026-11-14, O3 (committed-file half): every repository
named in committed qualification reports (ledger.json, readiness.json and
any collection manifest under docs/qualification/) must be enrolled as
public in docs/qualification/enrollment.json. Fails closed; never prints
the identity it rejects."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "tools" / "guards" / "public_identities.py"
WF = ".github/workflows/faraday-review.yml"
PRIVATE = "faraday-build/secret-thing"
PUBLIC = "faraday-build/pilot"


def _run(qual: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD), "--qualification-dir", str(qual)],
                          capture_output=True, text=True)


def _qual(tmp_path: Path, enrolled: list[tuple[str, str]] | None) -> Path:
    qual = tmp_path / "qualification"
    qual.mkdir()
    if enrolled is not None:
        (qual / "enrollment.json").write_text(json.dumps(
            [{"repository": r, "visibility": v, "workflow": WF} for r, v in enrolled]))
    return qual


def _ledger(repo: str) -> dict:
    return {"format": "faraday-review/qualification-ledger/v2",
            "entries": [{"repository": repo.lower(), "repository_spellings": [repo]}],
            "anomalies": [{"kind": "x", "execution": {"repository": repo.lower()}}]}


def test_committed_qualification_files_name_only_public_repositories():
    proc = _run(ROOT / "docs" / "qualification")
    assert proc.returncode == 0, proc.stderr


def test_public_enrolled_passes(tmp_path):
    qual = _qual(tmp_path, [("Faraday-Build/Pilot", "public")])
    (qual / "ledger.json").write_text(json.dumps(_ledger(PUBLIC)))
    (qual / "readiness.json").write_text(json.dumps(
        {"counted_prs": [{"repository": "faraday-BUILD/pilot"}]}))
    assert _run(qual).returncode == 0


def test_private_enrolled_in_ledger_fails_without_naming_it(tmp_path):
    qual = _qual(tmp_path, [(PUBLIC, "public"), (PRIVATE, "private")])
    (qual / "ledger.json").write_text(json.dumps(_ledger(PRIVATE)))
    proc = _run(qual)
    assert proc.returncode == 1
    assert "secret-thing" not in proc.stdout + proc.stderr


def test_unenrolled_in_readiness_fails(tmp_path):
    qual = _qual(tmp_path, [(PUBLIC, "public")])
    (qual / "readiness.json").write_text(json.dumps(
        {"synthetic_excluded": [{"repository": "x/unenrolled"}]}))
    assert _run(qual).returncode == 1


def test_private_in_nested_collection_manifest_fails(tmp_path):
    qual = _qual(tmp_path, [(PUBLIC, "public"), (PRIVATE, "private")])
    nested = qual / "artifacts" / "deep"
    nested.mkdir(parents=True)
    (nested / "collection-manifest-20261114T000000Z.json").write_text(json.dumps(
        {"repositories": [{"repository": PRIVATE, "runs": []}]}))
    assert _run(qual).returncode == 1


def test_missing_enrollment_fails_closed_when_reports_exist(tmp_path):
    qual = _qual(tmp_path, None)
    (qual / "ledger.json").write_text(json.dumps(_ledger(PUBLIC)))
    assert _run(qual).returncode != 0


def test_unreadable_report_fails_closed(tmp_path):
    qual = _qual(tmp_path, [(PUBLIC, "public")])
    (qual / "ledger.json").write_text("{not json")
    assert _run(qual).returncode != 0


def test_no_reports_passes_vacuously(tmp_path):
    assert _run(_qual(tmp_path, None)).returncode == 0


def test_identity_only_in_input_path_fails(tmp_path):
    """Codex P2 (#11): an archive path names its repository too."""
    qual = _qual(tmp_path, [(PUBLIC, "public")])
    (qual / "ledger.json").write_text(json.dumps(
        {"inputs": [{"path": f"{PRIVATE}/7/art/telemetry.jsonl"}],
         "anomalies": [{"source": f"{PRIVATE}/7/art/telemetry.jsonl"}]}))
    proc = _run(qual)
    assert proc.returncode == 1
    assert "secret-thing" not in proc.stdout + proc.stderr


def test_public_input_paths_and_root_manifest_pass(tmp_path):
    qual = _qual(tmp_path, [(PUBLIC, "public")])
    (qual / "readiness.json").write_text(json.dumps(
        {"evidence": {"ledger_inputs": [
            {"path": "Faraday-Build/pilot/7/art/telemetry.jsonl"},
            {"path": "collection-manifest-20261114T000000Z.json"}]},
         "anomalies": [{"sources": ["faraday-build/pilot/7/art/telemetry.jsonl"]}]}))
    assert _run(qual).returncode == 0


def test_path_without_provenance_fails_closed(tmp_path):
    qual = _qual(tmp_path, [(PUBLIC, "public")])
    (qual / "ledger.json").write_text(json.dumps({"inputs": [{"path": "run-1/telemetry.jsonl"}]}))
    assert _run(qual).returncode == 1
