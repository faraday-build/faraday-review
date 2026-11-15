"""S6 F-2: rerun-durable attempt records.

When a run is re-run GitHub deletes the earlier attempt's artifact
(`faraday-review-pr<N>-<head>-a1`), so collection lost attempt 1's telemetry
(S6 retry, A5). Check runs survive. Every attempt now publishes one neutral
`faraday-review / attempt-record` check run (external_id
faraday-review-attempt:<run_id>:<attempt>) carrying its aggregation files
verbatim with sha256; `collect` reads it back only for attempts whose
artifact is gone, verifies every byte and identity, archives it with
provenance, and lists every rejected copy so aggregation reports it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faraday_review import cli
from faraday_review.canonical import write_canonical
from faraday_review.telemetry import attempt_record as ar
from faraday_review.telemetry.qualification import aggregate, local_validation_errors
from fake_github import FakeSession
from qualification_support import sha, write_run
from test_collection import (
    HEAD, PRIV, PUB, TS, WF, Api, _archive, _collect, _enroll, _kinds, _manifest,
    repo_root,  # noqa: F401 - pytest fixture
)

RUN = 601
_SEQ: list = []
EVENT_IDS = {1: "6b409bd7-2547-49fd-97bb-0718296ef9f0", 2: "703ba8b2-e392-4c4e-85dd-2b5d9868f7ff"}


def _attempt_files(tmp: Path, attempt: int, *, repo: str = PUB, run_id: int = RUN,
                   cls: str = "block", pr: int = 1, head: str = "h1",
                   exec_attempt: int | None = None) -> dict[str, bytes]:
    _SEQ.append(None)  # a fresh directory per call: write_run appends
    run_dir = write_run(tmp / "_attempts", f"{len(_SEQ)}-{run_id}-a{attempt}-{cls}",
                        repo=repo, pr=pr, head=head, cls=cls,
                        created_at=f"2026-11-10T10:0{attempt}:00Z",
                        event_id=EVENT_IDS.get(attempt, f"00000000-0000-4000-8000-{attempt:012d}"),
                        execution={"provider": "github_actions", "run_id": run_id,
                                   "run_attempt": exec_attempt or attempt})
    files = {p.name: p.read_bytes() for p in run_dir.iterdir()}
    binding = {"caller_repo": repo, "pr_number": str(pr), "head_sha": sha(head),
               "base_sha": sha("base"), "run_id": str(run_id), "run_attempt": str(attempt),
               "reviewer_sha": "a" * 40, "governance_sha": "b" * 40}
    files["run-binding.json"] = (json.dumps(binding, sort_keys=True, indent=2) + "\n").encode()
    return files


def _check_run(cid: int, attempt: int, files: dict[str, bytes] | None = None, *,
               text: str | None = None, run_id: int = RUN, head: str = HEAD,
               slug: str = "github-actions", ext_attempt: int | None = None) -> dict:
    if text is None:
        text = ar.build_record_text(run_id, attempt, files)
    return {"id": cid, "name": ar.ATTEMPT_RECORD_CHECK_NAME, "head_sha": head,
            "external_id": ar.external_id(run_id, ext_attempt or attempt),
            "status": "completed", "conclusion": "neutral", "app": {"slug": slug},
            "output": {"title": "t", "summary": "s", "text": text}}


def _rerun_api(tmp: Path, records: list[dict], *, repo: str = PUB, visibility: str = "public",
               first_artifact_id: int = 9000) -> tuple[Api, dict[str, bytes], dict[str, bytes]]:
    """A run re-run once: attempt 1's artifact is gone (as GitHub does),
    attempt 2's artifact is present."""
    a1 = _attempt_files(tmp, 1, repo=repo)
    a2 = _attempt_files(tmp, 2, repo=repo, cls="green")
    api = Api()
    api._next_artifact = first_artifact_id
    api.repo(repo, visibility).run(repo, RUN, attempt=2)
    api.artifact(repo, RUN, a2, attempt=2)
    api.routes[f"/repos/{repo}/commits/{HEAD}/check-runs"] = (
        200, {"total_count": len(records), "check_runs": records})
    return api, a1, a2


def _run_entry(manifest: dict) -> dict:
    (repo,) = manifest["repositories"]
    (run,) = repo["runs"]
    return run


# ------------------------------------------------------------ record format


def test_record_round_trips_every_byte():
    files = {"telemetry.jsonl": b'{"a":1}\n', "decision.json": "{\"é\":\"\\u2028\"}\n".encode()}
    text = ar.build_record_text(5, 2, files)
    assert len(text) <= ar.MAX_TEXT_CHARS
    parsed = ar.parse_record_text(text)
    assert parsed == {"run_id": 5, "run_attempt": 2, "files": files}
    assert ar.external_id(5, 2) == "faraday-review-attempt:5:2"
    assert ar.build_record_text(5, 2, files) == text  # deterministic


def test_record_never_carries_path_bearing_files():
    with pytest.raises(ar.AttemptRecordError):
        ar.build_envelope(1, 1, {"telemetry.jsonl": b"{}", "pr-context.json": b"{}"})


def test_oversize_record_fails_closed():
    with pytest.raises(ar.AttemptRecordError) as exc:
        ar.build_record_text(1, 1, {"telemetry.jsonl": b"x" * (ar.MAX_TEXT_CHARS + 1)})
    assert exc.value.reason == "oversize"


@pytest.mark.parametrize("mutate,reason", [
    (lambda t: t.replace('"size":', '"size": '), "malformed"),
    (lambda t: t.replace(ar.END_MARKER, ""), "malformed"),
    (lambda t: t + ar.BEGIN_MARKER, "malformed"),
    (lambda t: t.replace("\\\"a\\\":1", "\\\"a\\\":2"), "sha256_mismatch"),
])
def test_tampered_text_is_rejected(mutate, reason):
    text = ar.build_record_text(5, 2, {"telemetry.jsonl": b'{"a":1}\n'})
    with pytest.raises(ar.AttemptRecordError) as exc:
        ar.parse_record_text(mutate(text))
    assert exc.value.reason == reason


# ------------------------------------------------------------ publication


REPO = "faraday-build/shadow-target"


def _publish(monkeypatch, artifacts: Path, *, run_id=43664575693, attempt=1, head="f" * 40):
    session = FakeSession({f"/repos/{REPO}/check-runs": (201, {"id": 4242})})
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-secret")
    code = cli.main(["publish-attempt-record", "--repository", REPO, "--pr", "7",
                     "--head-sha", head, "--run-id", str(run_id), "--run-attempt", str(attempt),
                     "--artifacts-dir", str(artifacts)])
    return code, [(m, p, body) for m, p, _, body in session.sent]


def _failed_attempt(tmp: Path, attempt: int = 1) -> Path:
    out = tmp / f"a{attempt}"
    assert cli.main(["record-failure", "--repository", REPO, "--pr", "7", "--head-sha", "f" * 40,
                     "--base-sha", "e" * 40, "--run-id", "43664575693", "--run-attempt",
                     str(attempt), "--failure-mode", "workflow_step_failed",
                     "--artifacts-dir", str(out)]) == 0
    return out


def test_failed_attempt_publishes_its_record(monkeypatch, tmp_path):
    out = _failed_attempt(tmp_path)
    (out / "pr-context.json").write_text('{"changed_files": ["src/secret_path.py"]}')
    (out / "gitleaks-report.json").write_text('[{"File": "src/leak.py"}]')
    code, sent = _publish(monkeypatch, out)
    assert code == 0
    ((method, path, body),) = sent
    assert (method, path) == ("POST", f"/repos/{REPO}/check-runs")
    assert body["name"] == "faraday-review / attempt-record"
    assert body["external_id"] == "faraday-review-attempt:43664575693:1"
    assert body["head_sha"] == "f" * 40
    assert (body["status"], body["conclusion"]) == ("completed", "neutral")
    text = body["output"]["text"]
    parsed = ar.parse_record_text(text)
    assert parsed["files"] == {"telemetry.jsonl": (out / "telemetry.jsonl").read_bytes()}
    assert "secret_path" not in text and "leak.py" not in text


def test_completed_attempt_publishes_telemetry_decision_and_binding(monkeypatch, tmp_path):
    files = _attempt_files(tmp_path, 1, repo=REPO, pr=7, run_id=43664575693)
    out = tmp_path / "artifacts"
    out.mkdir()
    for name, raw in files.items():
        (out / name).write_bytes(raw)
    code, sent = _publish(monkeypatch, out, head=sha("h1"))
    assert code == 0
    ((_, _, body),) = sent
    assert ar.parse_record_text(body["output"]["text"])["files"] == files


def test_no_telemetry_publishes_nothing(monkeypatch, tmp_path):
    (tmp_path / "empty").mkdir()
    code, sent = _publish(monkeypatch, tmp_path / "empty")
    assert code == 0 and sent == []


@pytest.mark.parametrize("kw", [{"run_id": 1}, {"attempt": 2}])
def test_foreign_telemetry_is_never_published(monkeypatch, tmp_path, kw):
    out = _failed_attempt(tmp_path)
    code, sent = _publish(monkeypatch, out, **kw)
    assert code == 2 and sent == []


def test_oversize_attempt_is_not_published(monkeypatch, tmp_path):
    out = _failed_attempt(tmp_path)
    (out / "decision.json").write_text(json.dumps({"pad": "x" * ar.MAX_TEXT_CHARS}))
    code, sent = _publish(monkeypatch, out)
    assert code == 2 and sent == []


# ------------------------------------------------------------ collection


def test_collect_reconstructs_a_deleted_attempt_from_its_check_run(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    a1 = _attempt_files(tmp_path, 1)
    records = [_check_run(70, 1, a1), _check_run(71, 2, _attempt_files(tmp_path, 2, cls="green"))]
    api, a1, a2 = _rerun_api(tmp_path, records)
    code, session = _collect(monkeypatch, api, repo_root)
    assert code == 0
    archive = _archive(repo_root)
    recovered = archive / "faraday-build" / "pilot" / str(RUN) / "attempt-record-a1"
    assert {p.name: p.read_bytes() for p in recovered.iterdir()} == a1
    manifest = _manifest(archive)
    assert local_validation_errors("qualification-collection-manifest", manifest) == []
    (entry,) = _run_entry(manifest)["attempt_records"]
    assert entry["attempt"] == 1 and entry["provenance"] == "check_run"
    assert entry["status"] == "accepted" and entry["accepted_check_run_ids"] == [70]
    assert entry["external_id"] == f"faraday-review-attempt:{RUN}:1"
    assert entry["rejected"] == []
    assert sorted(f["name"] for f in entry["files"]) == sorted(a1)
    # The listing asked for every attempt record, not only the latest.
    (params,) = [p for m, path, p, _ in session.sent if path.endswith("/check-runs")]
    assert params["filter"] == "all" and params["check_name"] == ar.ATTEMPT_RECORD_CHECK_NAME
    assert all(method == "GET" for method, _ in session.calls)

    ledger = aggregate(archive, generated_at=TS)
    assert "run_without_telemetry" not in _kinds(ledger)
    assert "attempt_record_rejected" not in _kinds(ledger)
    assert ledger["counts"]["events_read"] == 2
    classes = sorted(o["classifier_class"] for e in ledger["entries"] for r in e["runs"]
                     for o in r["occurrence_log"])
    assert classes == ["block", "green"]  # attempt 1's classification survives
    occurrences = [o for e in ledger["entries"] for r in e["runs"] for o in r["occurrence_log"]]
    assert all(o["policy_digest_verified"] for o in occurrences)


def test_attempt_with_artifact_is_never_looked_up(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, RUN)
    api.artifact(PUB, RUN, _attempt_files(tmp_path, 1))
    code, session = _collect(monkeypatch, api, repo_root)
    assert code == 0
    assert not any(p.endswith("/check-runs") for _, p in session.calls)
    assert "attempt_records" not in _run_entry(_manifest(_archive(repo_root)))


def test_missing_record_is_run_without_telemetry(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api, _, _ = _rerun_api(tmp_path, [])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    archive = _archive(repo_root)
    assert "attempt_records" not in _run_entry(_manifest(archive))
    ledger = aggregate(archive, generated_at=TS)
    (gap,) = [a for a in ledger["anomalies"] if a["kind"] == "run_without_telemetry"]
    assert gap["run_attempt"] == 1


def _telemetry_only(tmp: Path, **kw) -> dict[str, bytes]:
    return {"telemetry.jsonl": _attempt_files(tmp, 1, **kw)["telemetry.jsonl"]}


def test_telemetry_only_record_of_the_run_is_accepted(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api, _, _ = _rerun_api(tmp_path, [_check_run(80, 1, _telemetry_only(tmp_path))])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    (entry,) = _run_entry(_manifest(_archive(repo_root)))["attempt_records"]
    assert entry["status"] == "accepted"


def test_recovered_file_lost_from_archive_is_not_counted(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api, _, _ = _rerun_api(tmp_path, [_check_run(70, 1, _attempt_files(tmp_path, 1))])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    archive = _archive(repo_root)
    (archive / "faraday-build" / "pilot" / str(RUN) / "attempt-record-a1" / "decision.json").unlink()
    ledger = aggregate(archive, generated_at=TS)
    missing = [a for a in ledger["anomalies"] if a["kind"] == "collection_file_missing"]
    assert [(a["file"], a["check_run_ids"]) for a in missing] == [("decision.json", [70])]
    (gap,) = [a for a in ledger["anomalies"] if a["kind"] == "run_without_telemetry"]
    assert gap["run_attempt"] == 1


@pytest.mark.parametrize("make,reason", [
    (lambda t: _check_run(80, 1, text=ar.build_record_text(RUN, 1, _attempt_files(t, 1))
                          .replace('\\"risk_class\\":\\"block\\"', '\\"risk_class\\":\\"green\\"')),
     "sha256_mismatch"),
    (lambda t: _check_run(80, 1, text="no marker block"), "malformed"),
    (lambda t: _check_run(80, 1, _attempt_files(t, 1, exec_attempt=2)), "identity_mismatch"),
    (lambda t: _check_run(80, 1, _attempt_files(t, 1, repo="other-org/other")), "identity_mismatch"),
    (lambda t: _check_run(80, 1, _attempt_files(t, 1, pr=9)), "identity_mismatch"),
    (lambda t: _check_run(80, 1, _attempt_files(t, 1, head="h9")), "identity_mismatch"),
    (lambda t: _check_run(80, 1, text=ar.build_record_text(RUN, 2, _attempt_files(t, 1))),
     "identity_mismatch"),
    (lambda t: _check_run(80, 1, _attempt_files(t, 1), slug="some-app"), "untrusted_app"),
    (lambda t: _check_run(80, 1, _attempt_files(t, 1), head="0" * 40), "identity_mismatch"),
    # Telemetry alone (no decision/run-binding to cross-check) must still
    # name the run's repository, head and PR.
    (lambda t: _check_run(80, 1, _telemetry_only(t, repo="other-org/other")), "identity_mismatch"),
    (lambda t: _check_run(80, 1, _telemetry_only(t, head="h9")), "identity_mismatch"),
    (lambda t: _check_run(80, 1, _telemetry_only(t, pr=9)), "identity_mismatch"),
    (lambda t: _check_run(80, 1, text=ar.build_record_text(
        RUN, 1, {"telemetry.jsonl": b'{"not": "an event"}\n'})), "invalid_telemetry"),
])
def test_tampered_or_foreign_record_is_an_anomaly_never_accepted(monkeypatch, tmp_path, repo_root,
                                                                 make, reason):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api, _, _ = _rerun_api(tmp_path, [make(tmp_path)])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    archive = _archive(repo_root)
    assert not (archive / "faraday-build" / "pilot" / str(RUN) / "attempt-record-a1").exists()
    (entry,) = _run_entry(_manifest(archive))["attempt_records"]
    assert entry["status"] == "rejected" and entry["files"] == []
    assert entry["rejected"] == [{"check_run_id": 80, "reason": reason}]
    ledger = aggregate(archive, generated_at=TS)
    (rej,) = [a for a in ledger["anomalies"] if a["kind"] == "attempt_record_rejected"]
    assert (rej["run_attempt"], rej["check_run_id"], rej["reason"]) == (1, 80, reason)
    assert "run_without_telemetry" in _kinds(ledger)
    assert ledger["counts"]["events_read"] == 1


def test_conflicting_valid_records_are_all_rejected(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    block = _attempt_files(tmp_path, 1)
    green = _attempt_files(tmp_path, 1, cls="green")
    api, _, _ = _rerun_api(tmp_path, [_check_run(81, 1, green), _check_run(80, 1, block)])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    (entry,) = _run_entry(_manifest(_archive(repo_root)))["attempt_records"]
    assert entry["status"] == "rejected"
    assert entry["rejected"] == [{"check_run_id": 80, "reason": "conflicting_records"},
                                 {"check_run_id": 81, "reason": "conflicting_records"}]


def test_identical_copies_are_one_record_and_a_bad_copy_is_reported(monkeypatch, tmp_path,
                                                                    repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    a1 = _attempt_files(tmp_path, 1)
    api, _, _ = _rerun_api(tmp_path, [_check_run(90, 1, a1), _check_run(80, 1, a1),
                                      _check_run(85, 1, text="garbage")])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    archive = _archive(repo_root)
    (entry,) = _run_entry(_manifest(archive))["attempt_records"]
    assert entry["status"] == "accepted" and entry["accepted_check_run_ids"] == [80, 90]
    assert entry["rejected"] == [{"check_run_id": 85, "reason": "malformed"}]
    ledger = aggregate(archive, generated_at=TS)
    assert "run_without_telemetry" not in _kinds(ledger)
    assert "attempt_record_rejected" in _kinds(ledger)


def test_records_of_other_runs_or_names_are_ignored(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    other_run = _check_run(80, 1, _attempt_files(tmp_path, 1, run_id=999), run_id=999)
    renamed = dict(_check_run(81, 1, _attempt_files(tmp_path, 1)), name="faraday-review / other")
    api, _, _ = _rerun_api(tmp_path, [other_run, renamed])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    assert "attempt_records" not in _run_entry(_manifest(_archive(repo_root)))


def test_archived_artifact_from_an_earlier_collection_needs_no_record(monkeypatch, tmp_path,
                                                                     repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    a1 = _attempt_files(tmp_path, 1)
    api = Api().repo(PUB).run(PUB, RUN)
    api.artifact(PUB, RUN, a1)
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    # Re-run: GitHub deletes a1's artifact; a later collection must not
    # re-fetch it from a check run (it is already archived).
    api2, _, _ = _rerun_api(tmp_path, [_check_run(70, 1, a1)], first_artifact_id=9500)
    session = FakeSession(api2.routes)
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    code = cli.main(["collect", "--enrollment", str(repo_root / "docs/qualification/enrollment.json"),
                     "--archive-dir", str(_archive(repo_root)),
                     "--collected-at", "2026-11-15T12:00:00Z"])
    assert code == 0
    assert not any(p.endswith("/check-runs") for _, p in session.calls)
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    assert "run_without_telemetry" not in _kinds(ledger)


def test_private_repo_record_goes_only_to_the_private_archive(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PRIV, "visibility": "private", "workflow": WF})
    a1 = _attempt_files(tmp_path, 1, repo=PRIV)
    api, _, _ = _rerun_api(tmp_path, [_check_run(70, 1, a1)], repo=PRIV, visibility="private")
    private = tmp_path / "private-archive"
    code, _ = _collect(monkeypatch, api, repo_root, "--private-archive-dir", str(private))
    assert code == 0
    owner, name = PRIV.split("/")
    recovered = private / owner / name / str(RUN) / "attempt-record-a1"
    assert {p.name: p.read_bytes() for p in recovered.iterdir()} == a1
    public = _archive(repo_root)
    assert not public.exists() or not any(public.rglob("*"))
    (entry,) = _run_entry(_manifest(private))["attempt_records"]
    assert entry["status"] == "accepted"


def test_private_repo_without_private_archive_is_still_refused(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PRIV, "visibility": "private", "workflow": WF})
    api, _, _ = _rerun_api(tmp_path, [_check_run(70, 1, _attempt_files(tmp_path, 1, repo=PRIV))],
                           repo=PRIV, visibility="private")
    code, session = _collect(monkeypatch, api, repo_root)
    assert code == 2
    assert not any(p.endswith("/check-runs") for _, p in session.calls)


def test_recovery_is_deterministic(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    a1 = _attempt_files(tmp_path, 1)
    api, _, _ = _rerun_api(tmp_path, [_check_run(71, 1, a1), _check_run(70, 1, a1)])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    archive = _archive(repo_root)
    before = {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    api.routes[f"/repos/{PUB}/commits/{HEAD}/check-runs"][1]["check_runs"].reverse()
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    assert {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()} == before
    out1, out2 = tmp_path / "l1.json", tmp_path / "l2.json"
    write_canonical(out1, aggregate(archive, generated_at=TS))
    write_canonical(out2, aggregate(archive, generated_at=TS))
    assert out1.read_bytes() == out2.read_bytes()


def test_manifest_without_attempt_records_is_still_valid(monkeypatch, tmp_path, repo_root):
    """Manifests written before this change stay valid inputs."""
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api, _, _ = _rerun_api(tmp_path, [])
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    manifest = _manifest(_archive(repo_root))
    assert "attempt_records" not in _run_entry(manifest)
    assert local_validation_errors("qualification-collection-manifest", manifest) == []
