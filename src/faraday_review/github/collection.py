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
from ..telemetry.qualification import (
    COLLECTION_MANIFEST_FORMAT,
    COLLECTION_MANIFEST_PREFIX,
    ARCHIVED_FILES,
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
                      index: int) -> list[dict[str, Any]]:
    """Every item of a listing, or CollectionError. A listing that returns
    fewer distinct items than its total_count (a server-side result cap, or
    a page that never arrived) is refused, never archived as complete
    history (Codex r18 F2). The message names the enrollment entry index
    only, never the repository (it may be private)."""
    items, total = client.paginate_key_counted(path, key)
    distinct = {int(item["id"]) for item in items}
    if total is not None and len(distinct) < total:
        raise CollectionError(f"enrollment entry #{index}: {what} listing truncated "
                              f"({len(distinct)} of {total} items); nothing archived")
    return items


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
        out_runs.append({
            "run_id": int(run["id"]),
            "run_attempt": int(run.get("run_attempt") or 1),
            "status": str(run.get("status")),
            "conclusion": run.get("conclusion"),
            "created_at": run["created_at"],
            "artifacts": records,
        })
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
