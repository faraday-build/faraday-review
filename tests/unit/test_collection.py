"""`faraday-review collect` (GHO-004 P2): durable, read-only, fail-closed
archiving of shadow-run artifacts, plus the aggregate-side anomalies that
make absent evidence visible (run_without_telemetry,
artifact_expired_unarchived, enrolled_repo_without_runs)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from faraday_review import cli
from faraday_review.telemetry.qualification import aggregate, local_validation_errors
from qualification_support import sha, write_run
from fake_github import FakeSession, make_zip

ROOT = Path(__file__).resolve().parents[2]
PUB = "faraday-build/pilot"
PRIV = "secret-org/hidden-repo"
WF = ".github/workflows/faraday-review.yml"
TS = "2026-11-14T12:00:00Z"
HEAD = sha("h1")
PRIV_HEAD = sha("private-head")


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _run_files(tmp: Path, name: str, **kw) -> dict[str, bytes]:
    run_dir = write_run(tmp / "_build", name, **kw)
    files = {p.name: p.read_bytes() for p in run_dir.iterdir()}
    binding = {"caller_repo": kw.get("repo", PUB), "pr_number": str(kw.get("pr", 1))}
    files["run-binding.json"] = json.dumps(binding).encode()
    return files


class Api:
    """Builder for a fake GitHub with enrolled repos, runs and artifacts."""

    def __init__(self) -> None:
        self.routes: dict = {}
        self._next_artifact = 9000

    def repo(self, repo: str, visibility: str = "public") -> "Api":
        self.routes[f"/repos/{repo}"] = (200, {"full_name": repo, "visibility": visibility,
                                               "private": visibility != "public"})
        self.routes.setdefault(f"/repos/{repo}/actions/workflows/faraday-review.yml/runs",
                               (200, {"total_count": 0, "workflow_runs": []}))
        return self

    def run(self, repo: str, run_id: int, *, attempt: int = 1, conclusion: str | None = "success",
            status: str = "completed") -> "Api":
        path = f"/repos/{repo}/actions/workflows/faraday-review.yml/runs"
        runs = self.routes[path][1]["workflow_runs"]
        runs.append({"id": run_id, "run_attempt": attempt, "status": status,
                     "conclusion": conclusion, "created_at": "2026-11-10T10:00:00Z",
                     "head_sha": HEAD, "pull_requests": [{"number": 1}]})
        self.routes[path][1]["total_count"] = len(runs)
        self.routes.setdefault(f"/repos/{repo}/actions/runs/{run_id}/artifacts",
                               (200, {"total_count": 0, "artifacts": []}))
        return self

    def artifact(self, repo: str, run_id: int, members: dict[str, bytes] | None, *, pr: int = 1,
                 head: str = HEAD, attempt: int = 1, expired: bool = False,
                 digest: str | None | bool = True, name: str | None = None) -> int:
        self._next_artifact += 1
        aid = self._next_artifact
        raw = make_zip(members or {})
        listing = self.routes[f"/repos/{repo}/actions/runs/{run_id}/artifacts"][1]
        listing["artifacts"].append({
            "id": aid, "name": name or f"faraday-review-pr{pr}-{head}-a{attempt}",
            "size_in_bytes": len(raw), "expired": expired,
            "expires_at": "2027-02-08T10:00:00Z",
            "digest": _digest(raw) if digest is True else (digest or None),
            "workflow_run": {"id": run_id},
        })
        listing["total_count"] = len(listing["artifacts"])
        if not expired:
            self.routes[f"/repos/{repo}/actions/artifacts/{aid}/zip"] = (200, raw)
        return aid


@pytest.fixture()
def repo_root(tmp_path):
    root = tmp_path / "faraday-review"
    (root / ".git").mkdir(parents=True)
    (root / "docs" / "qualification").mkdir(parents=True)
    return root


def _enroll(root: Path, *entries: dict) -> Path:
    path = root / "docs" / "qualification" / "enrollment.json"
    path.write_text(json.dumps(list(entries)))
    return path


def _collect(monkeypatch, api: Api, root: Path, *extra: str) -> tuple[int, FakeSession]:
    session = FakeSession(api.routes)
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-secret")
    code = cli.main(["collect", "--enrollment", str(root / "docs/qualification/enrollment.json"),
                     "--archive-dir", str(root / "docs/qualification/artifacts"),
                     "--collected-at", TS, *extra])
    return code, session


def _archive(root: Path) -> Path:
    return root / "docs" / "qualification" / "artifacts"


def _manifest(archive: Path) -> dict:
    (path,) = sorted(archive.glob("collection-manifest-*.json"))
    return json.loads(path.read_text())


def _kinds(ledger: dict) -> set[str]:
    return {a["kind"] for a in ledger["anomalies"]}


# ------------------------------------------------------------ happy path


def test_collect_archives_only_aggregation_files(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 501)
    files = _run_files(tmp_path, "r1")
    files["pr-context.json"] = b'{"changed_files": ["src/secret_path.py"]}'
    files["gitleaks-report.json"] = b'[{"File": "src/leak.py", "StartLine": 7}]'
    files["review-evidence.json"] = b"{}"
    api.artifact(PUB, 501, files)
    code, session = _collect(monkeypatch, api, repo_root)
    assert code == 0
    archive = _archive(repo_root)
    art = archive / "faraday-build" / "pilot" / "501" / f"faraday-review-pr1-{HEAD}-a1"
    assert sorted(p.name for p in art.iterdir()) == ["decision.json", "run-binding.json",
                                                     "telemetry.jsonl"]
    for name in ("decision.json", "run-binding.json", "telemetry.jsonl"):
        assert (art / name).read_bytes() == files[name]
    # Path-bearing files never leave the artifact.
    blob = b"".join(p.read_bytes() for p in archive.rglob("*") if p.is_file())
    assert b"secret_path" not in blob and b"leak.py" not in blob
    manifest = _manifest(archive)
    assert local_validation_errors("qualification-collection-manifest", manifest) == []
    (run,) = manifest["repositories"][0]["runs"]
    (artifact,) = run["artifacts"]
    assert artifact["archived"] is True and artifact["attempt"] == 1
    assert {f["name"]: f["sha256"] for f in artifact["files"]}["telemetry.jsonl"] == \
        _digest(files["telemetry.jsonl"])
    assert all(method == "GET" for method, _ in session.calls)


def test_collect_is_idempotent_and_byte_stable(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 501)
    api.artifact(PUB, 501, _run_files(tmp_path, "r1"))
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    before = {p: p.read_bytes() for p in _archive(repo_root).rglob("*") if p.is_file()}
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    after = {p: p.read_bytes() for p in _archive(repo_root).rglob("*") if p.is_file()}
    assert before == after


def test_aggregate_records_manifest_as_input(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 501)
    api.artifact(PUB, 501, _run_files(tmp_path, "r1"))
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    kinds = sorted(i["kind"] for i in ledger["inputs"])
    assert kinds == ["collection_manifest", "decision", "run_binding", "telemetry"]
    assert not {"run_without_telemetry", "artifact_expired_unarchived",
                "enrolled_repo_without_runs"} & _kinds(ledger)
    assert local_validation_errors("qualification-ledger", ledger) == []


def test_prior_ledger_holds_the_manifest(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 501)
    api.artifact(PUB, 501, _run_files(tmp_path, "r1"))
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    archive = _archive(repo_root)
    prior = aggregate(archive, generated_at=TS)
    (manifest,) = archive.glob("collection-manifest-*.json")
    manifest.unlink()
    with pytest.raises(ValueError, match="collection_manifest"):
        aggregate(archive, generated_at=TS, prior_ledger=prior)


# ------------------------------------------------------------ absent-state anomalies


def test_two_runs_on_one_head_never_collide(monkeypatch, tmp_path, repo_root):
    """Artifact names are unique per run only (same PR, head, attempt)."""
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 520).run(PUB, 521)
    api.artifact(PUB, 520, _run_files(tmp_path, "x1"))
    api.artifact(PUB, 521, _run_files(tmp_path, "x2", created_at="2026-11-01T11:00:00Z"))
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    found = sorted(p.parent.parent.name for p in _archive(repo_root).rglob("telemetry.jsonl"))
    assert found == ["520", "521"]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "success", "timed_out"])
def test_run_without_telemetry_reported(monkeypatch, tmp_path, repo_root, conclusion):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 501).run(PUB, 502, conclusion=conclusion)
    api.artifact(PUB, 501, _run_files(tmp_path, "r1"))
    api.artifact(PUB, 502, {"run-binding.json": b"{}"})
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    hits = [a for a in ledger["anomalies"] if a["kind"] == "run_without_telemetry"]
    assert hits == [{"kind": "run_without_telemetry", "repository": PUB.casefold(),
                     "run_id": 502, "run_attempt": 1, "conclusion": conclusion}]


def test_run_with_no_artifact_at_all_reported(monkeypatch, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 503, conclusion="failure")
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    assert "run_without_telemetry" in _kinds(ledger)


def test_every_attempt_needs_telemetry(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 504, attempt=2)
    api.artifact(PUB, 504, _run_files(tmp_path, "r2"), attempt=2)
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    hits = [a for a in ledger["anomalies"] if a["kind"] == "run_without_telemetry"]
    assert [(a["run_id"], a["run_attempt"]) for a in hits] == [(504, 1)]


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "skipped")])
def test_unfinished_or_skipped_runs_are_not_missing_telemetry(monkeypatch, repo_root,
                                                              status, conclusion):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 505, status=status, conclusion=conclusion)
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    assert "run_without_telemetry" not in _kinds(ledger)


def test_expired_unarchived_artifact_reported(monkeypatch, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 506)
    aid = api.artifact(PUB, 506, None, expired=True)
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    hits = [a for a in ledger["anomalies"] if a["kind"] == "artifact_expired_unarchived"]
    assert hits == [{"kind": "artifact_expired_unarchived", "repository": PUB.casefold(),
                     "run_id": 506, "run_attempt": 1, "artifact_id": aid,
                     "expires_at": "2027-02-08T10:00:00Z"}]
    # The lost run is not double-reported as merely missing telemetry.
    assert "run_without_telemetry" not in _kinds(ledger)


def test_expired_but_previously_archived_is_fine(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 507)
    aid = api.artifact(PUB, 507, _run_files(tmp_path, "r1"))
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    listing = api.routes[f"/repos/{PUB}/actions/runs/507/artifacts"][1]["artifacts"]
    listing[0]["expired"] = True
    del api.routes[f"/repos/{PUB}/actions/artifacts/{aid}/zip"]
    assert _collect(monkeypatch, api, repo_root, "--collected-at", "2027-03-01T00:00:00Z")[0] == 0
    later = json.loads((_archive(repo_root) / "collection-manifest-20270301T000000Z.json").read_text())
    (art,) = later["repositories"][0]["runs"][0]["artifacts"]
    assert art["expired"] is True and art["archived"] is True
    assert sorted(f["name"] for f in art["files"]) == ["decision.json", "run-binding.json",
                                                       "telemetry.jsonl"]
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    assert not {"artifact_expired_unarchived", "run_without_telemetry"} & _kinds(ledger)


def test_enrolled_repo_without_runs_reported(monkeypatch, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB)
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    assert {"kind": "enrolled_repo_without_runs", "repository": PUB.casefold()} \
        in ledger["anomalies"]


def test_manifest_claims_a_file_that_is_missing(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 508)
    api.artifact(PUB, 508, _run_files(tmp_path, "r1"))
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    archive = _archive(repo_root)
    (archive / "faraday-build" / "pilot" / "508" / f"faraday-review-pr1-{HEAD}-a1" / "telemetry.jsonl").unlink()
    ledger = aggregate(archive, generated_at=TS)
    assert {"collection_file_missing", "run_without_telemetry"} <= _kinds(ledger)


def test_invalid_manifest_is_an_anomaly(repo_root):
    archive = _archive(repo_root)
    archive.mkdir(parents=True)
    (archive / "collection-manifest-20261114T120000Z.json").write_text('{"format": "nope"}')
    ledger = aggregate(archive, generated_at=TS)
    assert "invalid_collection_manifest" in _kinds(ledger)


# ------------------------------------------------------------ fail closed


def test_never_overwrites_with_different_bytes(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 509)
    api.artifact(PUB, 509, _run_files(tmp_path, "r1"))
    art = _archive(repo_root) / "faraday-build" / "pilot" / "509" / f"faraday-review-pr1-{HEAD}-a1"
    art.mkdir(parents=True)
    (art / "telemetry.jsonl").write_bytes(b"earlier bytes\n")
    code, _ = _collect(monkeypatch, api, repo_root)
    assert code == 2
    assert (art / "telemetry.jsonl").read_bytes() == b"earlier bytes\n"
    assert not list(_archive(repo_root).glob("collection-manifest-*.json"))
    # Detected at plan time: nothing else from the run was written either.
    assert sorted(p.name for p in art.iterdir()) == ["telemetry.jsonl"]


def test_write_immutable_never_replaces_bytes(tmp_path):
    from faraday_review.github.collection import CollectionError, _write_immutable
    target = tmp_path / "a" / "telemetry.jsonl"
    _write_immutable(target, b"one\n")
    _write_immutable(target, b"one\n")  # identical bytes: idempotent
    with pytest.raises(CollectionError, match="never overwritten"):
        _write_immutable(target, b"two\n")
    assert target.read_bytes() == b"one\n"


def test_manifest_never_overwritten(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 510)
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    (manifest,) = _archive(repo_root).glob("collection-manifest-*.json")
    before = manifest.read_bytes()
    api.run(PUB, 511)
    api.artifact(PUB, 511, _run_files(tmp_path, "late"))
    assert _collect(monkeypatch, api, repo_root)[0] == 2  # same --collected-at, new content
    assert manifest.read_bytes() == before
    # Refused before any file of the new run was written.
    assert not list(_archive(repo_root).rglob("telemetry.jsonl"))


def test_digest_mismatch_fails_closed(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 512)
    api.artifact(PUB, 512, _run_files(tmp_path, "r1"), digest="sha256:" + "0" * 64)
    assert _collect(monkeypatch, api, repo_root)[0] == 2
    assert not [p for p in _archive(repo_root).rglob("*") if p.is_file()]


def test_size_mismatch_fails_closed(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 513)
    api.artifact(PUB, 513, _run_files(tmp_path, "r1"), digest=None)
    api.routes[f"/repos/{PUB}/actions/runs/513/artifacts"][1]["artifacts"][0]["size_in_bytes"] += 1
    assert _collect(monkeypatch, api, repo_root)[0] == 2


def test_oversized_member_fails_closed(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 514)
    big = b"x" * (5 * 1024 * 1024 + 1)
    api.artifact(PUB, 514, {"telemetry.jsonl": big})
    assert _collect(monkeypatch, api, repo_root)[0] == 2


def test_nested_or_traversal_members_ignored(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 515)
    api.artifact(PUB, 515, {"../telemetry.jsonl": b"evil\n", "sub/decision.json": b"{}",
                            "run-binding.json": b"{}"})
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    files = sorted(p.relative_to(_archive(repo_root)).as_posix()
                   for p in _archive(repo_root).rglob("*") if p.is_file()
                   and not p.name.startswith("collection-manifest-"))
    assert files == [f"faraday-build/pilot/515/faraday-review-pr1-{HEAD}-a1/run-binding.json"]
    assert not (repo_root / "docs" / "qualification" / "telemetry.jsonl").exists()


def test_foreign_artifact_names_ignored(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 516)
    api.artifact(PUB, 516, _run_files(tmp_path, "r1"), name="coverage-report")
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    (run,) = _manifest(_archive(repo_root))["repositories"][0]["runs"]
    assert run["artifacts"] == []


def test_api_error_fails_closed(monkeypatch, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB)
    api.routes[f"/repos/{PUB}/actions/workflows/faraday-review.yml/runs"] = (403, {"message": "no"})
    assert _collect(monkeypatch, api, repo_root)[0] == 2
    assert not _archive(repo_root).exists() or not list(_archive(repo_root).rglob("*.json"))


def test_missing_token_fails(monkeypatch, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert cli.main(["collect", "--enrollment", str(repo_root / "docs/qualification/enrollment.json"),
                     "--archive-dir", str(_archive(repo_root)), "--collected-at", TS]) == 2


def test_enrollment_rejects_case_duplicate(monkeypatch, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF},
            {"repository": PUB.upper(), "visibility": "public", "workflow": WF})
    code, session = _collect(monkeypatch, Api().repo(PUB).repo(PUB.upper()), repo_root)
    assert code == 2
    assert session.calls == []  # rejected while loading the enrollment


# ------------------------------------------------------------ privacy (item 7)


def _private_fixture(tmp_path, declared: str = "private") -> Api:
    api = Api().repo(PUB).run(PUB, 601).repo(PRIV, "private").run(PRIV, 70123457, conclusion="failure")
    api.artifact(PUB, 601, _run_files(tmp_path, "pub"))
    priv = _run_files(tmp_path, "priv", repo=PRIV, pr=42, head="private-head")
    priv["gitleaks-report.json"] = b'[{"File": "config/prod-credentials.yml", "StartLine": 13}]'
    priv["pr-context.json"] = b'{"changed_files": ["billing/ledger_private.py"]}'
    api.artifact(PRIV, 70123457, priv, pr=42, head=PRIV_HEAD)
    return api


@pytest.mark.parametrize("declared", ["private", "public"])
def test_private_repo_refused_without_private_archive(monkeypatch, tmp_path, repo_root, declared):
    """Visibility is verified live: an entry declared public that GitHub
    reports private is still private."""
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF},
            {"repository": PRIV, "visibility": declared, "workflow": WF})
    code, session = _collect(monkeypatch, _private_fixture(tmp_path), repo_root)
    assert code == 2
    archive = _archive(repo_root)
    assert not archive.exists() or not [p for p in archive.rglob("*") if p.is_file()]
    # Refused before any run, artifact or download request.
    assert all("/actions/" not in path for _, path in session.calls)


def test_declared_private_but_public_is_still_private(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PRIV, "visibility": "private", "workflow": WF})
    api = Api().repo(PRIV, "public")
    assert _collect(monkeypatch, api, repo_root)[0] == 2


@pytest.mark.parametrize("where", ["inside_repo", "inside_public_archive"])
def test_private_archive_inside_public_repo_refused(monkeypatch, tmp_path, repo_root, where):
    _enroll(repo_root, {"repository": PRIV, "visibility": "private", "workflow": WF})
    target = (repo_root / "private-store") if where == "inside_repo" else (_archive(repo_root) / "p")
    code, _ = _collect(monkeypatch, _private_fixture(tmp_path), repo_root,
                       "--private-archive-dir", str(target))
    assert code == 2
    assert not target.exists()


def test_private_metadata_never_lands_in_public_archive(monkeypatch, tmp_path, repo_root):
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF},
            {"repository": PRIV, "visibility": "private", "workflow": WF})
    private_store = tmp_path / "operator-private-archive"
    code, _ = _collect(monkeypatch, _private_fixture(tmp_path), repo_root,
                       "--private-archive-dir", str(private_store))
    assert code == 0
    public_blob = b"".join((p.read_bytes() if p.is_file() else b"") + p.as_posix().encode()
                           for p in _archive(repo_root).rglob("*"))
    for needle in (b"secret-org", b"hidden-repo", PRIV_HEAD.encode(), PRIV_HEAD[:12].encode(),
                   b"pr42", b"\"pr_number\": 42", b"prod-credentials", b"ledger_private",
                   b"70123457"):
        assert needle not in public_blob, needle
    # The private store holds the private run, and still never the path files.
    private_blob = b"".join(p.read_bytes() for p in private_store.rglob("*") if p.is_file())
    assert b"hidden-repo" in private_blob or any("hidden-repo" in p.as_posix()
                                                 for p in private_store.rglob("*"))
    assert b"prod-credentials" not in private_blob and b"ledger_private" not in private_blob
    # Each archive aggregates on its own.
    pub_ledger = aggregate(_archive(repo_root), generated_at=TS)
    assert b"hidden-repo" not in json.dumps(pub_ledger).encode()
    priv_ledger = aggregate(private_store, generated_at=TS)
    assert {e["repository"] for e in priv_ledger["entries"]} == {PRIV.casefold()}


def test_committed_enrollment_is_valid_and_lists_no_private_repo():
    data = json.loads((ROOT / "docs" / "qualification" / "enrollment.json").read_text())
    assert local_validation_errors("qualification-enrollment", data) == []
    assert all(e["visibility"] == "public" for e in data)


# ------------------------------------------------------------ v0.3 / v0.4 compatibility (item 6)


def test_collected_v03_legacy_artifacts_aggregate_with_bounds(monkeypatch, tmp_path, repo_root):
    """A v0.3.0 producer (no event_id) re-uploaded the same record in two
    attempts' artifacts. C8: execution count unknown, bounded min 1 max 2."""
    _enroll(repo_root, {"repository": PUB, "visibility": "public", "workflow": WF})
    api = Api().repo(PUB).run(PUB, 801, attempt=2)
    legacy = _run_files(tmp_path, "legacy", contract_version="v0.3.0")
    assert b"event_id" not in legacy["telemetry.jsonl"]
    api.artifact(PUB, 801, legacy, attempt=1)
    api.artifact(PUB, 801, legacy, attempt=2)
    v04 = _run_files(tmp_path, "v04", pr=2, head="h2",
                     event_id="4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f601",
                     execution={"provider": "github_actions", "run_id": 802, "run_attempt": 1})
    api.run(PUB, 802).artifact(PUB, 802, v04, pr=2, head=sha("h2"))
    assert _collect(monkeypatch, api, repo_root)[0] == 0
    ledger = aggregate(_archive(repo_root), generated_at=TS)
    identity = ledger["counts"]["identity"]
    assert identity["executions_exact"] == 1
    assert identity["execution_count"] is None
    assert identity["execution_count_bounds"] == {"min": 2, "max": 3}
    assert "legacy_execution_identity_unknown" in _kinds(ledger)
    assert "run_without_telemetry" not in _kinds(ledger)
