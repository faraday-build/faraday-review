"""Founder ruling 2026-11-14, O3 (runtime half): `aggregate` and
`readiness` refuse to write a repository identity that is not enrolled as
public into a destination inside a git checkout (a public repository), and
never print the identity they refuse."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from qualification_support import write_label, write_real_prs

from faraday_review.cli import main
from faraday_review.github.collection import (
    CollectionError,
    check_public_destination,
    named_repositories,
    public_identity_violations,
)

TS = "2026-11-12T00:00:00Z"
PRIVATE = "faraday-build/secret-thing"
PUBLIC = "faraday-build/pilot"
WF = ".github/workflows/faraday-review.yml"


def _enroll(path: Path, entries: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"repository": r, "visibility": v, "workflow": WF}
                                for r, v in entries]))
    return path


def _checkout(root: Path, entries) -> Path:
    (root / ".git").mkdir(parents=True)
    _enroll(root / "docs" / "qualification" / "enrollment.json", entries)
    return root


def test_named_repositories_walks_every_identity_field():
    doc = {"entries": [{"repository": "A/b", "repository_spellings": ["a/B", "A/b"]}],
           "anomalies": [{"execution": {"repository": "c/d"}}, {"caller_repo": "e/f"}],
           "other": "g/h"}
    assert named_repositories(doc) == ["A/b", "a/B", "c/d", "e/f"]


def test_violations_count_private_and_unenrolled_case_insensitively():
    enrollment = [{"repository": "Faraday-Build/Pilot", "visibility": "public", "workflow": WF},
                  {"repository": PRIVATE, "visibility": "private", "workflow": WF}]
    assert public_identity_violations({"repository": PUBLIC}, enrollment) == 0
    assert public_identity_violations({"repository": PRIVATE}, enrollment) == 1
    assert public_identity_violations({"repository": "x/unenrolled"}, enrollment) == 1
    assert public_identity_violations({"a": {"repository": PRIVATE},
                                       "b": {"repository": PRIVATE.upper()}}, enrollment) == 1


def test_destination_outside_a_checkout_is_not_checked(tmp_path):
    check_public_destination(tmp_path / "out" / "ledger.json", {"repository": PRIVATE}, None)


def test_private_identity_refused_inside_a_checkout(tmp_path):
    repo = _checkout(tmp_path / "repo", [(PRIVATE, "private")])
    with pytest.raises(CollectionError) as exc:
        check_public_destination(repo / "docs" / "qualification" / "ledger.json",
                                 {"repository": PRIVATE}, None)
    assert PRIVATE not in str(exc.value) and "secret-thing" not in str(exc.value)


def test_missing_enrollment_fails_closed_inside_a_checkout(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(CollectionError):
        check_public_destination(tmp_path / "repo" / "ledger.json", {"repository": PUBLIC}, None)


def test_explicit_enrollment_is_used(tmp_path):
    repo = _checkout(tmp_path / "repo", [])
    other = _enroll(tmp_path / "e.json", [(PUBLIC, "public")])
    check_public_destination(repo / "ledger.json", {"repository": PUBLIC}, other)
    with pytest.raises(CollectionError):
        check_public_destination(repo / "ledger.json", {"repository": PUBLIC}, None)


def _inputs(tmp_path: Path, repo: str) -> Path:
    """Laid out like `collect`'s archive: <owner>/<repo>/<run>/<file>."""
    src = tmp_path / "in"
    write_real_prs(src / repo, 2, repo=repo)
    write_label(src / repo, "l1", repo=repo, pr=1, head="h1")
    return src


def test_provenance_paths_name_repositories():
    doc = {"inputs": [{"path": "Owner/Repo/1/a/telemetry.jsonl"},
                      {"path": "collection-manifest-20261114T000000Z.json"}],
           "anomalies": [{"source": "o2/r2/1/x/telemetry.jsonl"},
                         {"sources": ["o3/r3/9/telemetry.jsonl"]}]}
    enrollment = [{"repository": r, "visibility": "public", "workflow": WF}
                  for r in ("owner/repo", "o2/r2", "o3/r3")]
    assert public_identity_violations(doc, enrollment) == 0
    assert public_identity_violations(doc, enrollment[:2]) == 1


def test_identity_only_in_archive_path_is_refused(tmp_path):
    """Codex P2 (#11): malformed telemetry leaves the identity only in
    inputs[].path / anomaly sources; it must still be checked."""
    enrollment = [{"repository": PUBLIC, "visibility": "public", "workflow": WF}]
    doc = {"inputs": [{"path": f"{PRIVATE}/7/art/telemetry.jsonl", "kind": "telemetry"}],
           "anomalies": [{"kind": "invalid_telemetry_event",
                          "source": f"{PRIVATE}/7/art/telemetry.jsonl"}]}
    assert public_identity_violations(doc, enrollment) == 1


def test_path_without_repository_provenance_fails_closed():
    enrollment = [{"repository": PUBLIC, "visibility": "public", "workflow": WF}]
    assert public_identity_violations({"inputs": [{"path": "telemetry.jsonl"}]}, enrollment) == 1
    assert public_identity_violations({"sources": ["loose/telemetry.jsonl"]}, enrollment) == 1


def test_cli_refuses_private_path_only_identity(tmp_path, capsys):
    src = tmp_path / "in" / PRIVATE / "7" / "art"
    src.mkdir(parents=True)
    (src / "telemetry.jsonl").write_text("{not json\n")
    repo = _checkout(tmp_path / "repo", [(PUBLIC, "public")])
    out = repo / "docs" / "qualification" / "ledger.json"
    assert main(["aggregate", "--input-dir", str(tmp_path / "in"), "--output", str(out),
                 "--generated-at", TS]) == 2
    assert not out.exists()
    assert "secret-thing" not in capsys.readouterr().err


def test_cli_aggregate_refuses_private_identity_in_public_checkout(tmp_path, capsys):
    src = _inputs(tmp_path, PRIVATE)
    repo = _checkout(tmp_path / "repo", [(PRIVATE, "private")])
    out = repo / "docs" / "qualification" / "ledger.json"
    assert main(["aggregate", "--input-dir", str(src), "--output", str(out),
                 "--generated-at", TS]) == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "secret-thing" not in err


def test_cli_aggregate_and_readiness_write_public_identity(tmp_path):
    src = _inputs(tmp_path, PUBLIC)
    repo = _checkout(tmp_path / "repo", [(PUBLIC, "public")])
    qual = repo / "docs" / "qualification"
    synth = qual / "synthetic-prs.json"
    synth.write_text("[]\n")
    assert main(["aggregate", "--input-dir", str(src), "--output", str(qual / "ledger.json"),
                 "--generated-at", TS]) == 0
    assert main(["readiness", "--ledger", str(qual / "ledger.json"), "--synthetic", str(synth),
                 "--generated-at", TS, "--output", str(qual / "readiness.json")]) == 0


def test_cli_readiness_refuses_private_identity_in_public_checkout(tmp_path, capsys):
    src = _inputs(tmp_path, PRIVATE)
    ledger = tmp_path / "private" / "ledger.json"
    assert main(["aggregate", "--input-dir", str(src), "--output", str(ledger),
                 "--generated-at", TS]) == 0  # outside any checkout: allowed
    repo = _checkout(tmp_path / "repo", [(PRIVATE, "private")])
    synth = tmp_path / "s.json"
    synth.write_text("[]\n")
    out = repo / "docs" / "qualification" / "readiness.json"
    capsys.readouterr()
    assert main(["readiness", "--ledger", str(ledger), "--synthetic", str(synth),
                 "--generated-at", TS, "--output", str(out)]) == 2
    assert not out.exists()
    assert "secret-thing" not in capsys.readouterr().err


def test_cli_enrollment_flag(tmp_path):
    src = _inputs(tmp_path, PUBLIC)
    repo = _checkout(tmp_path / "repo", [])
    other = _enroll(tmp_path / "e.json", [(PUBLIC, "public")])
    out = repo / "ledger.json"
    assert main(["aggregate", "--input-dir", str(src), "--output", str(out),
                 "--generated-at", TS]) == 2
    assert main(["aggregate", "--input-dir", str(src), "--output", str(out),
                 "--generated-at", TS, "--enrollment", str(other)]) == 0
