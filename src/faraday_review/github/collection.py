"""`faraday-review collect`: durable archiving of shadow-run artifacts
(GHO-004 P2).

Read-only against GitHub (GET only). For each enrolled caller repository it
lists the caller workflow's runs and their artifacts, downloads every
unexpired `faraday-review-pr<N>-<head>-a<attempt>` artifact, and extracts
ONLY the files aggregation reads (telemetry.jsonl, decision.json,
adjudications.jsonl, run-binding.json). Path-bearing files
(pr-context.json, gitleaks-report.json, review evidence) never leave the
zip. Files land at <archive>/<owner>/<repo>/<run id>/<artifact name>/<file>; an
existing file is never overwritten with different bytes (fail closed).
Each invocation writes one immutable collection manifest per archive root.

Rerun durability (S6 F-2): GitHub deletes a run's earlier-attempt artifacts
on re-run. For every attempt 1..run_attempt without an archived telemetry
artifact, `collect` reads that attempt's `faraday-review / attempt-record`
check run on the run's head (check runs survive re-runs), verifies it
(external_id, app, sha256 of every file, schema, and identity: run id and
attempt, repository, PR, head) and archives its files under
<run id>/attempt-record-a<attempt>/ with provenance in the manifest's
`attempt_records`. A record that fails any check is never archived; it is
listed as rejected and surfaces as an anomaly in aggregation.

Privacy: a repository is public only if the enrollment says so AND the
live API says so. Private repositories are refused unless an archive
directory outside the public repository is given; then their files and
manifest go only there, and the public archive never mentions them.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..canonical import canonical_bytes
from ..errors import FaradayReviewError
from ..telemetry.attempt_record import (
    ATTEMPT_RECORD_CHECK_NAME,
    EXTERNAL_ID_RE,
    AttemptRecordError,
    parse_record_text,
)
from ..telemetry.qualification import (
    COLLECTION_MANIFEST_FORMAT,
    COLLECTION_MANIFEST_PREFIX,
    ARCHIVED_FILES,
    TELEMETRY_FILE,
    persisted_event_diagnostics,
    persisted_local_diagnostics,
    repo_identity,
    require_timestamp,
)
from .client import GitHubClient

ARTIFACT_NAME_RE = re.compile(r"^faraday-review-pr([1-9][0-9]*)-([0-9a-f]{40})-a([1-9][0-9]*)$")
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024


class CollectionError(FaradayReviewError):
    reason = "collection_failed"


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_enrollment(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"cannot read enrollment {path}: {exc}") from exc
    errors = persisted_local_diagnostics("qualification-enrollment", data)
    if errors:
        raise CollectionError(f"enrollment fails schema: {errors[:3]}")
    seen: set[str] = set()
    for index, entry in enumerate(data):
        key = repo_identity(entry["repository"])
        if key in seen:
            raise CollectionError(f"enrollment entry #{index} repeats a repository "
                                  "(identity is case-insensitive)")
        seen.add(key)
    return data


# Keys whose values name a repository in qualification outputs (ledger,
# readiness, collection manifest, anomalies). Founder ruling 2026-11-14, O3.
_IDENTITY_KEYS = ("repository", "caller_repo")
_IDENTITY_LIST_KEYS = ("repository_spellings",)
# Archive-relative provenance paths (inputs[].path, files[].path, anomaly
# source/sources). `collect` lays the archive out as <owner>/<repo>/<run>/...,
# so such a path names its repository too (Codex P2, #11).
_PATH_KEYS = ("path", "source", "sources")
_ROOT_MANIFEST_RE = re.compile(r"^collection-manifest-[0-9TZ]+\.json$")


def named_repositories(document: Any) -> list[str]:
    """Every repository identity a JSON document names under an identity
    key, at any depth; sorted, unique, as spelled."""
    found: set[str] = set()
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _IDENTITY_KEYS and isinstance(value, str):
                    found.add(value)
                elif key in _IDENTITY_LIST_KEYS and isinstance(value, list):
                    found.update(v for v in value if isinstance(v, str))
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(found)


def _provenance_paths(document: Any) -> set[str]:
    found: set[str] = set()
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _PATH_KEYS and isinstance(value, str):
                    found.add(value)
                elif key in _PATH_KEYS and isinstance(value, list) and \
                        all(isinstance(v, str) for v in value):
                    found.update(value)
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def public_identity_violations(document: Any, enrollment: list[dict[str, Any]]) -> int:
    """How many distinct repository identities (case-insensitive) the
    document names that the enrollment does not declare public, plus every
    distinct provenance path whose repository cannot be established. A
    repository absent from the enrollment counts: nothing is public by
    default. A path names <owner>/<repo> by its first two components; only a
    root collection manifest carries no repository in its path."""
    public = {repo_identity(e["repository"]) for e in enrollment if e["visibility"] == "public"}
    named = {repo_identity(r) for r in named_repositories(document)}
    unprovable = 0
    for path in _provenance_paths(document):
        parts = path.split("/")
        if len(parts) == 1 and _ROOT_MANIFEST_RE.match(path):
            continue
        if len(parts) >= 3 and all(parts[:2]):
            named.add(repo_identity(f"{parts[0]}/{parts[1]}"))
        else:
            unprovable += 1
    return len(named - public) + unprovable


def check_public_destination(output: Path, document: Any, enrollment_path: Path | None) -> None:
    """Refuse to write `document` to `output` inside a git checkout (a public
    repository, as for `collect`) when it names any repository that the
    enrollment does not declare public. The enrollment defaults to the
    checkout's docs/qualification/enrollment.json; a missing or invalid one
    fails closed. Outside any checkout nothing is checked (the private side,
    like --private-archive-dir). The refused identities are never printed."""
    root = _git_root(output.resolve().parent)
    if root is None:
        return
    path = enrollment_path if enrollment_path is not None \
        else root / "docs" / "qualification" / "enrollment.json"
    enrollment = load_enrollment(path)
    count = public_identity_violations(document, enrollment)
    if count:
        raise CollectionError(
            f"refusing to write {count} repository identit{'y' if count == 1 else 'ies'} not "
            f"enrolled as public into {output}, which is inside a git checkout (public). "
            "Write it outside the repository instead (founder ruling O3).")


def _git_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def observed_visibility(client: GitHubClient, repository: str) -> str:
    """Live visibility. Anything other than an explicit public answer is
    private (fail closed: internal, missing fields, odd values)."""
    data = client.get_json(f"/repos/{repository}")
    if isinstance(data, dict) and data.get("visibility") == "public" and data.get("private") is False:
        return "public"
    return "private"


def _check_destination(path: Path, root: Path) -> None:
    """Every component from the archive root down to `path` must be a real
    directory (or not exist yet), and `path` itself must not be a symlink or
    a directory. A symlinked ancestor could redirect writes anywhere,
    including into the public repository (Codex P1, #10)."""
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise CollectionError(f"refusing symlinked directory {current}")
        if current.exists() and not current.is_dir():
            raise CollectionError(f"{current} exists and is not a directory")
    if path.is_symlink() or path.is_dir():
        raise CollectionError(f"refusing non-regular destination {path}")


def _stage_bytes(fh, raw: bytes) -> None:
    fh.write(raw)
    fh.flush()
    os.fsync(fh.fileno())


def _publish(items: list[tuple], forbidden: Path | None = None) -> None:
    """Write (root, path, bytes) all-or-nothing. Each file is staged
    complete in a sibling temp file and hard-linked into place (never
    replacing an existing name); on any failure every file and directory
    this call created is removed again, so no partial or truncated
    publication survives (Codex P2, #10)."""
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    try:
        for item in items:
            root, path, raw = item[:3]
            no_go = item[3] if len(item) > 3 else forbidden
            if no_go is not None and _inside(path, no_go):
                # Private bytes never land inside the public checkout,
                # whatever the archive roots look like (Codex P1, #10).
                raise CollectionError(f"refusing to write a private destination inside the "
                                      f"public repository: {path}")
            _check_destination(path, root)
            if path.exists():
                if path.read_bytes() != raw:
                    raise CollectionError(f"{path} exists with different bytes; never overwritten")
                continue
            if not root.exists():
                root.mkdir(parents=True)
                created_dirs.append(root)
            current = root
            for part in path.relative_to(root).parts[:-1]:
                current = current / part
                if not current.exists():
                    current.mkdir()
                    created_dirs.append(current)
            _check_destination(path, root)
            staging = path.with_name(f".{path.name}.partial-{os.getpid()}")
            try:
                with staging.open("xb") as fh:
                    _stage_bytes(fh, raw)
                os.link(staging, path)
            finally:
                staging.unlink(missing_ok=True)
            created_files.append(path)
    except BaseException as exc:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            # Only directories this call created, and only if now empty.
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        if isinstance(exc, OSError):
            raise CollectionError(f"archive write failed ({exc.strerror or exc}); "
                                  "nothing from this collection was kept") from exc
        raise


def _write_immutable(path: Path, raw: bytes) -> None:
    """Single-file form of `_publish` (root = the file's directory)."""
    _publish([(path.parent, path, raw)])


def _extract(raw: bytes) -> dict[str, bytes]:
    """Only top-level members with an exact wanted name; size-capped."""
    out: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.filename not in ARCHIVED_FILES:
                    continue
                if info.file_size > MAX_FILE_BYTES:
                    raise CollectionError(f"artifact member {info.filename} exceeds {MAX_FILE_BYTES} bytes")
                # ZipExtFile never yields more than the header's file_size,
                # so the header check above bounds the read.
                with zf.open(info) as fh:
                    data = fh.read()
                if info.filename in out:
                    raise CollectionError(f"artifact repeats member {info.filename}")
                out[info.filename] = data
    except zipfile.BadZipFile as exc:
        raise CollectionError(f"artifact is not a valid zip: {exc}") from exc
    return out


def _existing_files(directory: Path, root: Path) -> list[dict[str, Any]]:
    files = []
    for name in ARCHIVED_FILES:
        path = directory / name
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            files.append({"name": name, "path": path.relative_to(root).as_posix(),
                          "sha256": _sha256(raw), "size": len(raw)})
    return sorted(files, key=lambda f: f["name"])


def _plan_artifact(client: GitHubClient, repository: str, run_id: int, artifact: dict[str, Any],
                   root: Path, writes: list[tuple[Path, Path, bytes]]) -> dict[str, Any] | None:
    match = ARTIFACT_NAME_RE.match(str(artifact.get("name", "")))
    if not match:
        return None
    owner, name = repository.split("/")
    # The artifact name is unique within a run only: two runs on one head
    # (e.g. opened, then ready_for_review) share it, so the run id is part
    # of the path.
    directory = root / owner / name / str(run_id) / artifact["name"]
    digest = artifact.get("digest")
    if digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest)):
        raise CollectionError(f"artifact {artifact['id']}: unexpected digest format")
    record: dict[str, Any] = {
        "artifact_id": int(artifact["id"]),
        "name": artifact["name"],
        "pr_number": int(match.group(1)),
        "head_sha": match.group(2),
        "attempt": int(match.group(3)),
        "size_in_bytes": int(artifact.get("size_in_bytes") or 0),
        "digest": digest,
        "expired": bool(artifact.get("expired")),
        "expires_at": artifact.get("expires_at"),
        "archived": False,
        "files": [],
    }
    if record["expired"]:
        record["files"] = _existing_files(directory, root)
        record["archived"] = bool(record["files"])
        return record
    raw = client.download(f"/repos/{repository}/actions/artifacts/{record['artifact_id']}/zip",
                          MAX_ZIP_BYTES)
    if digest is not None and _sha256(raw) != digest:
        raise CollectionError(f"artifact {record['artifact_id']}: digest mismatch; nothing archived")
    if digest is None and len(raw) != record["size_in_bytes"]:
        raise CollectionError(f"artifact {record['artifact_id']}: size mismatch "
                              f"({len(raw)} != {record['size_in_bytes']}); nothing archived")
    members = _extract(raw)
    for member in sorted(members):
        path = directory / member
        data = members[member]
        _check_destination(path, root)
        if path.exists() and path.read_bytes() != data:
            raise CollectionError(f"{path} exists with different bytes; never overwritten")
        writes.append((root, path, data))
        record["files"].append({"name": member, "path": path.relative_to(root).as_posix(),
                                "sha256": _sha256(data), "size": len(data)})
    record["archived"] = bool(record["files"])
    return record


def _complete_listing(client: GitHubClient, path: str, key: str, what: str,
                      index: int, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every item of a listing, or CollectionError. A listing that returns
    fewer distinct items than its total_count (a server-side result cap, or
    a page that never arrived) is refused, never archived as complete
    history (Codex r18 F2). The message names the enrollment entry index
    only, never the repository (it may be private)."""
    items, total = client.paginate_key_counted(path, key, params)
    distinct = {int(item["id"]) for item in items}
    if total is not None and len(distinct) < total:
        raise CollectionError(f"enrollment entry #{index}: {what} listing truncated "
                              f"({len(distinct)} of {total} items); nothing archived")
    return items


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The app that owns check runs created with a workflow's GITHUB_TOKEN.
ATTEMPT_RECORD_APP = "github-actions"


def _archived_attempt(root: Path, repository: str, run_id: int, attempt: int) -> bool:
    """A previous collection already archived this attempt's artifact
    telemetry (before GitHub deleted it): no recovery needed."""
    owner, name = repository.split("/")
    run_dir = root / owner / name / str(run_id)
    if not run_dir.is_dir() or run_dir.is_symlink():
        return False
    for child in run_dir.iterdir():
        match = ARTIFACT_NAME_RE.match(child.name)
        if match and int(match.group(3)) == attempt and child.is_dir() and not child.is_symlink():
            telemetry = child / TELEMETRY_FILE
            if telemetry.is_file() and not telemetry.is_symlink():
                return True
    return False


def _verify_attempt_record(check_run: dict[str, Any], repository: str, run_id: int, attempt: int,
                           head_sha: str, prs: set[int]) -> dict[str, bytes]:
    """Every byte of one attempt-record check run, or AttemptRecordError
    with the rejection reason. Nothing is trusted from the check run that the
    record does not prove about itself AND that matches the collected run."""
    app = check_run.get("app")
    if not isinstance(app, dict) or app.get("slug") != ATTEMPT_RECORD_APP:
        raise AttemptRecordError("untrusted_app", "attempt record not created by GitHub Actions")
    if check_run.get("head_sha") != head_sha:
        raise AttemptRecordError("identity_mismatch", "attempt record head differs from the run")
    output = check_run.get("output")
    record = parse_record_text(output.get("text") if isinstance(output, dict) else None)
    if (record["run_id"], record["run_attempt"]) != (run_id, attempt):
        raise AttemptRecordError("identity_mismatch", "attempt record names another execution")
    if not prs:
        raise AttemptRecordError("identity_unverifiable", "the run names no pull request")
    files = record["files"]
    try:
        events = [json.loads(line) for line in files[TELEMETRY_FILE].decode("utf-8").splitlines()
                  if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttemptRecordError("invalid_telemetry", "attempt record telemetry is not JSONL") from exc
    if not events:
        raise AttemptRecordError("invalid_telemetry", "attempt record telemetry is empty")
    for event in events:
        if not isinstance(event, dict) or persisted_event_diagnostics(event):
            raise AttemptRecordError("invalid_telemetry", "attempt record telemetry fails schema")
        execution = event.get("execution")
        if not isinstance(execution, dict) or execution.get("provider") != "github_actions" \
                or (int(execution["run_id"]), int(execution["run_attempt"])) != (run_id, attempt):
            raise AttemptRecordError("identity_mismatch", "telemetry execution differs from record")
        if repo_identity(event["repository"]) != repo_identity(repository) \
                or event["head_sha"] != head_sha or event["pr_number"] not in prs:
            raise AttemptRecordError("identity_mismatch", "telemetry subject differs from the run")
    for name in ("decision.json", "run-binding.json"):
        if name not in files:
            continue
        try:
            doc = json.loads(files[name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AttemptRecordError("malformed", f"attempt record {name} is not JSON") from exc
        if not isinstance(doc, dict):
            raise AttemptRecordError("malformed", f"attempt record {name} is not an object")
        if name == "decision.json":
            ok = (isinstance(doc.get("repository"), str)
                  and repo_identity(doc["repository"]) == repo_identity(repository)
                  and doc.get("pr_number") in prs and doc.get("head_sha") == head_sha)
        else:
            ok = (isinstance(doc.get("caller_repo"), str)
                  and repo_identity(doc["caller_repo"]) == repo_identity(repository)
                  and doc.get("head_sha") == head_sha and doc.get("run_id") == str(run_id)
                  and doc.get("run_attempt") == str(attempt)
                  and doc.get("pr_number") in {str(p) for p in prs})
        if not ok:
            raise AttemptRecordError("identity_mismatch", f"attempt record {name} names another run")
    return files


def _recover_attempts(client: GitHubClient, repository: str, run: dict[str, Any],
                      attempts: list[int], prs: set[int], root: Path,
                      writes: list[tuple[Path, Path, bytes]], index: int) -> list[dict[str, Any]]:
    """Attempt records for `attempts` (S6 F-2), one manifest entry per
    attempt that has at least one candidate check run. Accepted only when
    every verified copy is byte-identical; differing verified copies reject
    them all (conflicting_records). An attempt with no candidate gets no
    entry: aggregation then reports it as run_without_telemetry."""
    run_id = int(run["id"])
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or not _SHA_RE.match(head_sha):
        return []
    check_runs = _complete_listing(
        client, f"/repos/{repository}/commits/{head_sha}/check-runs", "check_runs", "check run",
        index, params={"check_name": ATTEMPT_RECORD_CHECK_NAME, "filter": "all"})
    by_attempt: dict[int, list[dict[str, Any]]] = {}
    for check_run in check_runs:
        if check_run.get("name") != ATTEMPT_RECORD_CHECK_NAME:
            continue
        match = EXTERNAL_ID_RE.match(str(check_run.get("external_id") or ""))
        if not match or int(match.group(1)) != run_id or int(match.group(2)) not in attempts:
            continue
        by_attempt.setdefault(int(match.group(2)), []).append(check_run)
    owner, name = repository.split("/")
    out: list[dict[str, Any]] = []
    for attempt in sorted(by_attempt):
        verified: list[tuple[int, dict[str, bytes]]] = []
        rejected: list[dict[str, Any]] = []
        for check_run in sorted(by_attempt[attempt], key=lambda c: int(c["id"])):
            try:
                files = _verify_attempt_record(check_run, repository, run_id, attempt, head_sha, prs)
            except AttemptRecordError as exc:
                rejected.append({"check_run_id": int(check_run["id"]), "reason": exc.reason})
                continue
            verified.append((int(check_run["id"]), files))
        entry: dict[str, Any] = {
            "attempt": attempt, "provenance": "check_run",
            "external_id": f"faraday-review-attempt:{run_id}:{attempt}",
            "status": "rejected", "accepted_check_run_ids": [], "rejected": rejected, "files": []}
        if len({canonical_bytes({k: v.hex() for k, v in f.items()}) for _, f in verified}) > 1:
            entry["rejected"] = sorted(
                rejected + [{"check_run_id": cid, "reason": "conflicting_records"}
                            for cid, _ in verified], key=lambda r: r["check_run_id"])
        elif verified:
            files = verified[0][1]
            directory = root / owner / name / str(run_id) / f"attempt-record-a{attempt}"
            for member in sorted(files):
                path = directory / member
                data = files[member]
                _check_destination(path, root)
                if path.exists() and path.read_bytes() != data:
                    raise CollectionError(f"{path} exists with different bytes; never overwritten")
                writes.append((root, path, data))
                entry["files"].append({"name": member, "path": path.relative_to(root).as_posix(),
                                       "sha256": _sha256(data), "size": len(data)})
            entry["status"] = "accepted"
            entry["accepted_check_run_ids"] = [cid for cid, _ in verified]
        out.append(entry)
    return out


def _created_on_or_after(run: dict[str, Any], cutoff: date, index: int) -> bool:
    try:
        created = datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise CollectionError(f"enrollment entry #{index}: run {run.get('id')} has no valid "
                              "created_at") from exc
    if created.tzinfo is None:
        raise CollectionError(f"enrollment entry #{index}: run {run.get('id')} created_at "
                              "is not timezone-aware")
    return created.astimezone(timezone.utc).date() >= cutoff


def _collect_repo(client: GitHubClient, entry: dict[str, Any], visibility: str, root: Path,
                  writes: list[tuple[Path, Path, bytes]], index: int) -> dict[str, Any]:
    repository = entry["repository"]
    workflow = entry["workflow"].rsplit("/", 1)[-1]
    # No server-side `created` filter: GitHub caps a filtered run listing at
    # 1,000 results, so the enrollment cutoff is applied locally to the
    # uncapped listing (Codex r18 F2).
    runs = _complete_listing(client, f"/repos/{repository}/actions/workflows/{workflow}/runs",
                             "workflow_runs", "workflow run", index)
    if entry.get("enrolled_from"):
        cutoff = date.fromisoformat(entry["enrolled_from"])
        runs = [r for r in runs if _created_on_or_after(r, cutoff, index)]
    out_runs = []
    for run in sorted(runs, key=lambda r: int(r["id"])):
        artifacts = _complete_listing(
            client, f"/repos/{repository}/actions/runs/{int(run['id'])}/artifacts",
            "artifacts", "artifact", index)
        records = []
        for artifact in sorted(artifacts, key=lambda a: int(a["id"])):
            record = _plan_artifact(client, repository, int(run["id"]), artifact, root, writes)
            if record is not None:
                records.append(record)
        run_attempt = int(run.get("run_attempt") or 1)
        out_run = {
            "run_id": int(run["id"]),
            "run_attempt": run_attempt,
            "status": str(run.get("status")),
            "conclusion": run.get("conclusion"),
            "created_at": run["created_at"],
            "artifacts": records,
        }
        # S6 F-2: attempts whose artifact telemetry is gone (deleted on
        # re-run, or expired) are recovered from their attempt record.
        have = {r["attempt"] for r in records
                if any(f["name"] == TELEMETRY_FILE for f in r["files"])}
        lost = [a for a in range(1, run_attempt + 1)
                if a not in have and not _archived_attempt(root, repository, int(run["id"]), a)]
        if lost:
            prs = {r["pr_number"] for r in records}
            prs |= {int(p["number"]) for p in run.get("pull_requests") or []
                    if isinstance(p, dict) and isinstance(p.get("number"), int)}
            attempt_records = _recover_attempts(client, repository, run, lost, prs, root, writes,
                                                index)
            if attempt_records:
                out_run["attempt_records"] = attempt_records
        out_runs.append(out_run)
    return {"repository": repository, "visibility": visibility, "workflow": entry["workflow"],
            "runs": out_runs}


def collect(client: GitHubClient, enrollment: list[dict[str, Any]], *, public_archive: Path,
            private_archive: Path | None, collected_at: str) -> dict[str, Any]:
    """Collect every enrolled repository. All visibility checks run before
    any run/artifact request; all writes happen only after every download
    verified, so a failure leaves the archives unchanged."""
    require_timestamp(collected_at, "collected_at")
    public_archive = public_archive.resolve()
    repo_root = _git_root(public_archive) or public_archive
    if private_archive is not None:
        private_archive = private_archive.resolve()
        # Disjoint in BOTH directions: a private archive that contains the
        # public checkout would place owner/repo paths inside it (Codex P1, #10).
        if (_inside(private_archive, repo_root) or _inside(private_archive, public_archive)
                or _inside(repo_root, private_archive) or _inside(public_archive, private_archive)):
            raise CollectionError("--private-archive-dir must not overlap the public repository "
                                  "(neither inside it nor containing it)")

    plan: list[tuple[dict[str, Any], str, int]] = []
    for index, entry in enumerate(enrollment):
        observed = observed_visibility(client, entry["repository"])
        visibility = "public" if (entry["visibility"] == "public" and observed == "public") else "private"
        if visibility == "private" and private_archive is None:
            # The repository name is deliberately not printed.
            raise CollectionError(
                f"enrollment entry #{index} is private (declared {entry['visibility']}, "
                f"observed {observed}); refusing to write it into the public archive. "
                "Pass --private-archive-dir outside this repository (founder decision P8).")
        plan.append((entry, visibility, index))

    stamp = re.sub(r"[^0-9TZ]", "", collected_at)
    per_root: dict[Path, list[dict[str, Any]]] = {}
    writes: list[tuple[Path, Path, bytes]] = []
    for entry, visibility, index in plan:
        root = public_archive if visibility == "public" else private_archive
        assert root is not None
        per_root.setdefault(root, []).append(_collect_repo(client, entry, visibility, root, writes,
                                                           index))

    manifests: list[tuple[Path, Path, bytes]] = []
    for root, repos in per_root.items():
        manifest = {"format": COLLECTION_MANIFEST_FORMAT, "collected_at": collected_at,
                    "repositories": sorted(repos, key=lambda r: repo_identity(r["repository"]))}
        errors = persisted_local_diagnostics("qualification-collection-manifest", manifest)
        if errors:
            raise CollectionError(f"manifest fails schema: {errors[:3]}")
        path = root / f"{COLLECTION_MANIFEST_PREFIX}{stamp}.json"
        raw = canonical_bytes(manifest) + b"\n"
        # An existing manifest with different bytes is refused by _publish,
        # which then rolls back everything this collection wrote.
        manifests.append((root, path, raw))

    # Manifests last: a manifest never names a file that is not in place.
    _publish([(root, path, raw, repo_root if root == private_archive else None)
              for root, path, raw in writes + manifests])
    return {
        "repositories": sum(len(r) for r in per_root.values()),
        "runs": sum(len(r["runs"]) for repos in per_root.values() for r in repos),
        "files_written": len(writes),
        "manifests": [p.as_posix() for _, p, _ in manifests],
    }
