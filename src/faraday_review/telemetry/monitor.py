"""`faraday-review monitor-report` and `monitor-notify`: the scheduled
qualification monitor's status view (GHO-AUTONOMOUS-PRODUCTION-010 M1).

Pure and read-only: no network, no clock (the caller passes
--generated-at), no RNG; sorted iteration only, so the output is
byte-identical across hash seeds.

The monitor REPORTS; it never decides. Every qualification figure (gate
count, exclusions, escalation condition, agreement, latency, infrastructure
failures, anomalies) is copied from the readiness report and its ledger.
Pending founder adjudications are the readiness `counted_prs` whose
recorded status is `unadjudicated`; nothing here re-derives eligibility.

What the monitor adds:

* checkpoint (CP-5/10/15/20) reached by the adjudicated qualifying count;
* runs the latest collection manifest lists as not completed (stalled once
  older than --stall-hours);
* an authorization scan over every archived decision.json, telemetry.jsonl
  and adjudications.jsonl (artifact copies and attempt-record copies): any
  approval_authorized / merge_authorized that is not exactly false, a
  risk_class outside the four classes, a non-boolean
  founder_review_required, or (decision.json) a missing field is an
  AUTHORIZATION ANOMALY: status CRITICAL with the explicit shadow kill-switch
  instruction. Values are never echoed: path, field and problem only.

Privacy (founder ruling O3): every input and the output may name only the
--repository the monitor was given (case-insensitive). Anything else -
another repository in the archive, a manifest, the ledger, the readiness
report, the synthetic list, an archived file, or an unprovable provenance
path - is refused before any output is written, and the refused identity is
never printed.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..canonical import canonical_bytes
from ..errors import FaradayReviewError
from ..github.collection import public_identity_violations
from .qualification import (
    ADJUDICATIONS_FILE,
    DECISION_FILE,
    TELEMETRY_FILE,
    persisted_local_diagnostics,
    repo_identity,
    require_timestamp,
)

MONITOR_FORMAT = "faraday-review/qualification-monitor/v1"
CHECKPOINTS = (5, 10, 15, 20)
STATUSES = ("OK", "ATTENTION", "CRITICAL")
SHADOW_KILL_SWITCH = "FARADAY_REVIEW_SHADOW_ENABLED"
MONITOR_KILL_SWITCH = "FARADAY_REVIEW_MONITOR_ENABLED"
ISSUE_TITLE = "Faraday Review qualification status"
ISSUE_LABEL = "faraday-review-qualification"
MARKER_PREFIX = "<!-- faraday-review-qualification-monitor: "
MARKER_SUFFIX = " -->"
MARKER_VERSION = 1
EXIT_CRITICAL = 3
DEFAULT_STALL_HOURS = 6
# GitHub caps an issue body at 65,536 characters; the full list stays in the
# status JSON artifact.
MAX_RENDERED_FINDINGS = 50

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MANIFEST_RE = re.compile(r"^collection-manifest-[0-9TZ]+\.json$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CLASSES = ("green", "yellow", "red", "block")
# Fields scanned in every archived document. Names only: this module never
# produces any of them (authorization_boundary guard).
_FLAG_FIELDS = ("approval_authorized", "merge_authorized")
_CLASS_FIELD = "risk_class"
_REVIEW_FIELD = "founder_review_required"
_LEDGER_AUTH_KINDS = ("authorization_flag_true_in_telemetry",)
_SCANNED = (DECISION_FILE, TELEMETRY_FILE, ADJUDICATIONS_FILE)


class MonitorError(FaradayReviewError):
    reason = "monitor_refused"


def checkpoint_reached(count: int) -> int | None:
    reached = None
    for level in CHECKPOINTS:
        if count >= level:
            reached = level
    return reached


def _next_checkpoint(count: int) -> int | None:
    for level in CHECKPOINTS:
        if count < level:
            return level
    return None


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _load_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitorError(f"cannot read {what}") from exc


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ------------------------------------------------------------ identity


def _identity_guard(repository: str, documents: list[Any]) -> None:
    enrollment = [{"repository": repository, "visibility": "public"}]
    total = sum(public_identity_violations(doc, enrollment) for doc in documents)
    if total:
        raise MonitorError(
            f"refusing: inputs name {total} repository identit{'y' if total == 1 else 'ies'} "
            "other than --repository (founder ruling O3); nothing was written")


def _archive_files(archive: Path, repository: str) -> list[tuple[str, Path]]:
    """Every regular file under the archive, as (relative path, path).
    A symlink anywhere, or a file outside <owner>/<repo>/ of --repository
    (other than a root collection manifest), is refused."""
    if not archive.is_dir() or archive.is_symlink():
        raise MonitorError("--archive-dir is not a directory")
    key = repo_identity(repository)
    files = []
    for path in sorted(archive.rglob("*")):
        rel = path.relative_to(archive).as_posix()
        if path.is_symlink():
            raise MonitorError("refusing: the archive contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise MonitorError("refusing: the archive contains a non-regular file")
        parts = rel.split("/")
        if len(parts) == 1:
            if not _MANIFEST_RE.match(rel):
                raise MonitorError("refusing: unexpected file at the archive root")
        elif len(parts) < 3 or repo_identity(f"{parts[0]}/{parts[1]}") != key:
            raise MonitorError("refusing: the archive holds a repository other than "
                               "--repository (founder ruling O3)")
        files.append((rel, path))
    return files


# ------------------------------------------------- authorization scan


def _check_document(doc: dict[str, Any], *, rel: str, line: int | None, required: bool,
                    findings: list[dict[str, Any]]) -> None:
    def add(field: str, problem: str) -> None:
        findings.append({"path": rel, "line": line, "field": field, "problem": problem})

    for field in _FLAG_FIELDS:
        if field not in doc:
            if required:
                add(field, "missing")
            continue
        value = doc[field]
        if value is True:
            add(field, "true")
        elif value is not False:
            add(field, "unexpected_value")
    if _CLASS_FIELD in doc:
        value = doc[_CLASS_FIELD]
        if not ((value is None and not required) or (isinstance(value, str) and value in _CLASSES)):
            add(_CLASS_FIELD, "unexpected_value")
    elif required:
        add(_CLASS_FIELD, "missing")
    if _REVIEW_FIELD in doc:
        if not isinstance(doc[_REVIEW_FIELD], bool):
            add(_REVIEW_FIELD, "unexpected_value")
    elif required:
        add(_REVIEW_FIELD, "missing")


def _scan_archive(files: list[tuple[str, Path]]) -> tuple[list[dict[str, Any]], list[str],
                                                          list[Any]]:
    """(authorization findings, unreadable files, parsed documents)."""
    findings: list[dict[str, Any]] = []
    unreadable: list[str] = []
    documents: list[Any] = []
    for rel, path in files:
        name = rel.rsplit("/", 1)[-1]
        if name not in _SCANNED and not _MANIFEST_RE.match(name) and name != "run-binding.json":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            unreadable.append(rel)
            continue
        if name.endswith(".jsonl"):
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                # A malformed or non-object line is recorded and scanning
                # continues: a later record with an authorization anomaly must
                # still be found, so bad evidence can never hide a CRITICAL
                # (Codex P2 on #17, r45).
                try:
                    doc = json.loads(line)
                except ValueError:
                    unreadable.append(f"{rel}:{lineno}")
                    continue
                documents.append(doc)
                if isinstance(doc, dict):
                    _check_document(doc, rel=rel, line=lineno, required=False, findings=findings)
                else:
                    unreadable.append(f"{rel}:{lineno}")
            continue
        try:
            doc = json.loads(text)
        except ValueError:
            unreadable.append(rel)
            continue
        documents.append(doc)
        if name == DECISION_FILE:
            if isinstance(doc, dict):
                _check_document(doc, rel=rel, line=None, required=True, findings=findings)
            else:
                unreadable.append(rel)
    return findings, sorted(set(unreadable)), documents


# ------------------------------------------------------- manifests


def _runs_not_completed(manifests: list[dict[str, Any]], generated_at: str,
                        stall_hours: int) -> list[dict[str, Any]]:
    """The newest view (collected_at order) of every run, kept when it is
    not completed. Age is measured against --generated-at, never a clock."""
    latest: dict[int, dict[str, Any]] = {}
    for manifest in sorted(manifests, key=lambda m: _time(m["collected_at"])):
        for repo in manifest["repositories"]:
            for run in repo["runs"]:
                latest[run["run_id"]] = run
    now = _time(generated_at)
    out = []
    for run_id in sorted(latest):
        run = latest[run_id]
        if run["status"] == "completed":
            continue
        age = (now - _time(run["created_at"])).total_seconds() / 3600
        out.append({"run_id": run_id, "run_attempt": run["run_attempt"], "status": run["status"],
                    "age_hours": round(age, 2), "stalled": age >= stall_hours})
    return out


# --------------------------------------------------------- report


def _anomaly_counts(anomalies: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for anomaly in anomalies:
        kind = anomaly.get("kind")
        kind = kind if isinstance(kind, str) and _KIND_RE.match(kind) else "unrecognised"
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


MAX_QUEUE_MARKER = 500
# Closed-window digests kept in the issue marker (the newest; ~5 PRs each).
MAX_WINDOW_MARKER = 200
# Founder-queue lines rendered into the issue body (65,536-character limit);
# the full queue stays in status.json (Codex P2 r2, #20).
MAX_RENDERED_QUEUE = 50


def v5_summary(v5: dict[str, Any] | None, unavailable: str | None) -> dict[str, Any]:
    """The QA-1 proposed gate, summarized for the status view. Never
    authoritative: v4 above stays the gate of record until adoption."""
    if v5 is None:
        return {"available": False, "reason": unavailable or "not_computed"}
    c, g = v5["counts"], v5["gate"]["conditions"]
    return {
        "available": True, "status": v5["status"], "authoritative": v5["authoritative"],
        "verified": c["verified"], "threshold": v5["threshold"],
        "verified_green": c["verified_green"], "green_minimum": g["green_coverage"]["minimum"],
        "window_open": c["window_open"], "reference_unknown": c["reference_unknown"],
        # Outstanding work comes from the gate conditions, which count every
        # unanswered exception/sample including those whose reference is
        # unknown; the per-PR verification counts are mutually exclusive
        # (Codex P2, #24).
        "exceptions_open": g["exceptions_open"], "samples_pending": g["sampling"]["pending"],
        "escalation": g["escalation"]["status"], "sampling": g["sampling"]["status"],
        "confirmed_material_disagreements": g["confirmed_material_disagreements"],
        "met": v5["gate"]["met"],
        "founder_queue": [{"pr_number": q["pr_number"], "head_sha": q["head_sha"], "reasons": q["reasons"]}
                          for q in sorted(v5["founder_queue"], key=lambda q: q["pr_number"])],
        # Digest of each CLOSED sampling window (membership + samples). The
        # issue marker keeps the previous run's digests, so a later run that
        # re-rolls a closed window is detected and routed to the founder as an
        # exception (r50d, QA-1 refinement 6).
        "closed_windows": {str(w["index"]): _sha256(canonical_bytes(
            {"members": w["members"], "sampled": w["sampled"]}))[:16]
            for w in v5["windows"] if w["closed"]},
    }


def _queue_items(summary: dict[str, Any]) -> list[str]:
    return sorted(f"{q['pr_number']}:{q['head_sha'][:12]}:{'+'.join(q['reasons'])}"
                  for q in summary.get("founder_queue", []))


def build_status(*, readiness: dict[str, Any], ledger: dict[str, Any],
                 manifests: list[dict[str, Any]], archive_files: list[tuple[str, Path]],
                 repository: str, generated_at: str, stall_hours: int,
                 input_evidence: dict[str, Any],
                 v5: dict[str, Any] | None = None, v5_unavailable: str | None = None) -> dict[str, Any]:
    findings, unreadable, _ = _scan_archive(archive_files)
    ledger_auth = sum(1 for a in ledger["anomalies"] if a.get("kind") in _LEDGER_AUTH_KINDS)
    ledger_auth += sum(1 for a in readiness["anomalies"]
                       if a.get("kind") in _LEDGER_AUTH_KINDS and a not in ledger["anomalies"])
    n = readiness["adjudicated_qualifying_real_pr_count"]
    gate = readiness["gate"]
    cond = gate["escalation_condition"]
    acc = readiness["escalation_accuracy"]
    agreement = readiness["agreement"]
    latency = readiness["founder_latency"]
    infra = readiness["drift"]["infrastructure_failures"]
    pending = [{"pr_number": p["pr_number"], "head_sha": p["head_sha"]}
               for p in sorted(readiness["counted_prs"], key=lambda p: p["pr_number"])
               if p["qualification"]["status"] == "unadjudicated"]
    not_completed = _runs_not_completed(manifests, generated_at, stall_hours)
    anomaly_counts = _anomaly_counts(readiness["anomalies"])
    critical = bool(findings) or ledger_auth > 0
    critical_digest = _sha256(canonical_bytes({"findings": findings, "ledger": ledger_auth})) \
        if critical else None
    attention = (bool(anomaly_counts) or bool(pending) or bool(unreadable)
                 or any(r["stalled"] for r in not_completed) or cond["status"] == "fail"
                 or infra["failed_runs"] > 0 or bool(readiness["qualification"]["incomplete_prs"]))
    with_v5 = v5 is not None or v5_unavailable is not None
    summary = v5_summary(v5, v5_unavailable) if with_v5 else None
    if summary is not None and summary.get("founder_queue"):
        attention = True
    status = "CRITICAL" if critical else ("ATTENTION" if attention else "OK")
    reached = checkpoint_reached(n)
    kill_command = f"gh variable delete {SHADOW_KILL_SWITCH} -R {repository}"
    doc = {
        "format": MONITOR_FORMAT,
        "generated_at": generated_at,
        "repository": repository,
        "status": status,
        "checkpoint": {"reached": reached, "next": _next_checkpoint(n), "levels": list(CHECKPOINTS)},
        "qualification": {
            "real_pr_count": readiness["real_pr_count"],
            "qualifying": readiness["qualifying_real_pr_count"],
            "adjudicated_qualifying": n,
            "threshold": gate["threshold"],
            "real_prs_in_ledger": readiness["qualification"]["real_prs_in_ledger"],
            "exclusions": readiness["qualification"]["exclusions"],
        },
        "gate": {"met": gate["met"], "criterion": gate["criterion"],
                 "criterion_status": gate["criterion_status"]},
        "escalation": {
            "condition": cond["status"], "missed": cond["missed"], "unknown": cond["unknown"],
            "mandatory_total": cond["mandatory_total"], "matrix": acc["matrix"],
            "missed_prs": sorted(p["pr_number"] for p in acc["missed_escalation_prs"]),
            "unknown_prs": sorted(p["pr_number"] for p in acc["unknown_prs"]),
        },
        "agreement": {"rate": agreement["agreement_rate"], "compared": agreement["compared"],
                      "agree": agreement["agree"], "disagree": agreement["disagree"],
                      "direction": agreement["direction"]},
        "latency": {"n": latency["all_adjudicated"]["n"],
                    "median_seconds": latency["all_adjudicated"]["median_seconds"],
                    "p90_seconds": latency["all_adjudicated"]["p90_seconds"],
                    "escalated_only": latency["escalated_only"]},
        "infrastructure": {
            "failed_runs": infra["failed_runs"], "failure_modes": infra["failure_modes"],
            "api_budget_exceeded_runs": infra["api_budget_exceeded_runs"],
            "status": infra["status"],
            "policy_digest_drift": readiness["drift"]["policy_digest_drift"],
            "reviewer_version_drift": readiness["drift"]["reviewer_version_drift"],
        },
        "anomalies": {"total": sum(anomaly_counts.values()), "by_kind": anomaly_counts},
        "pending_adjudications": pending,
        "incomplete_prs": sorted(p["pr_number"] for p in readiness["qualification"]["incomplete_prs"]),
        "runs_not_completed": not_completed,
        "stalled_runs": sum(1 for r in not_completed if r["stalled"]),
        "synthetic_excluded": [{"pr_number": s["pr_number"],
                                "present_in_ledger": s["present_in_ledger"]}
                               for s in sorted(readiness["synthetic_excluded"],
                                               key=lambda s: s["pr_number"])],
        "authorization": {"findings": findings, "ledger_anomalies": ledger_auth,
                          "unreadable": unreadable, "files_scanned": len(archive_files)},
        "kill_switch": {"command": kill_command, "required": critical,
                        "monitor_variable": MONITOR_KILL_SWITCH},
        "inputs": input_evidence,
        "marker": {"v": MARKER_VERSION, "repository": repository,
                   "checkpoint": reached or 0, "status": status,
                   "anomaly_counts": anomaly_counts, "critical_digest": critical_digest},
        "note": "Qualification evidence only. The monitor authorizes nothing (HR-1).",
    }
    if summary is not None:
        doc["qualification_v5"] = summary
        items = _queue_items(summary)
        # The visible list is capped (issue-body size); the digest and total
        # cover the whole queue, so an item beyond the cap still notifies
        # (Codex P2, #20).
        doc["marker"]["founder_queue"] = items[:MAX_QUEUE_MARKER]
        doc["marker"]["founder_queue_total"] = len(items)
        doc["marker"]["founder_queue_digest"] = _sha256(canonical_bytes(items))
        closed = summary.get("closed_windows", {})
        keep = sorted(closed, key=int)[-MAX_WINDOW_MARKER:]
        doc["marker"]["closed_windows"] = {k: closed[k] for k in keep}
    return doc


def _fmt_rate(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _fmt_seconds(value: Any) -> str:
    return "n/a" if value is None else f"{value:g}s"


def render_marker(marker: dict[str, Any]) -> str:
    compact = json.dumps(marker, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return MARKER_PREFIX + compact + MARKER_SUFFIX


def render_markdown(status: dict[str, Any]) -> str:
    q = status["qualification"]
    cp = status["checkpoint"]
    esc = status["escalation"]
    lines = [f"## {ISSUE_TITLE}", "",
             f"Repository: `{status['repository']}` · generated {status['generated_at']} · "
             f"status **{status['status']}**", ""]
    if status["status"] == "CRITICAL":
        lines += ["> [!CAUTION]",
                  "> **AUTHORIZATION ANOMALY.** An archived decision, telemetry or attempt "
                  "record carries an authorization field outside its expected value.",
                  f"> Run now: `{status['kill_switch']['command']}`",
                  "> Then preserve the evidence (this run's artifact) and report.", ""]
    reached = f"CP-{cp['reached']}" if cp["reached"] else "none"
    nxt = f"CP-{cp['next']}" if cp["next"] else "none (all checkpoints reached)"
    lines += ["### Qualification",
              f"- Adjudicated qualifying real PRs: **{q['adjudicated_qualifying']}/{q['threshold']}**",
              f"- Checkpoint reached: {reached} · next: {nxt}",
              f"- Real PRs: {q['real_pr_count']} · qualifying: {q['qualifying']} · "
              f"in ledger: {q['real_prs_in_ledger']}",
              "- Exclusions: " + ", ".join(f"{k} {v}" for k, v in sorted(q["exclusions"].items())),
              f"- Gate met: {status['gate']['met']} (qualification evidence only; authorizes nothing)",
              "", "### Escalation",
              f"- Escalation condition: **{esc['condition']}** · missed {esc['missed']} · "
              f"unknown {esc['unknown']} · mandatory_total {esc['mandatory_total']}",
              "- 2×2: " + ", ".join(f"{k} {v}" for k, v in sorted(esc["matrix"].items()))]
    if esc["missed_prs"]:
        lines.append("- Missed escalations: " + ", ".join(f"#{n}" for n in esc["missed_prs"]))
    if esc["unknown_prs"]:
        lines.append("- Unknown: " + ", ".join(f"#{n}" for n in esc["unknown_prs"]))
    agr, lat = status["agreement"], status["latency"]
    lines += ["", "### Agreement and founder latency",
              f"- Agreement rate: {_fmt_rate(agr['rate'])} ({agr['agree']}/{agr['compared']})",
              f"- Founder latency (n={lat['n']}): median {_fmt_seconds(lat['median_seconds'])}, "
              f"p90 {_fmt_seconds(lat['p90_seconds'])}"]
    infra = status["infrastructure"]
    modes = ", ".join(f"`{k}` {v}" for k, v in sorted(infra["failure_modes"].items())) or "none"
    lines += ["", "### Infrastructure",
              f"- Failed runs: {infra['failed_runs']} ({infra['status']}) · API budget exceeded: "
              f"{infra['api_budget_exceeded_runs']}",
              f"- Failure modes: {modes}",
              f"- Policy digest drift: {infra['policy_digest_drift']} · reviewer version drift: "
              f"{infra['reviewer_version_drift']}"]
    anomalies = status["anomalies"]
    lines += ["", f"### Anomalies ({anomalies['total']})"]
    lines += [f"- `{k}`: {v}" for k, v in anomalies["by_kind"].items()] or ["- none"]
    lines += ["", f"### Pending founder adjudications ({len(status['pending_adjudications'])})"]
    lines += [f"- #{p['pr_number']} head `{p['head_sha'][:12]}`"
              for p in status["pending_adjudications"]] or ["- none"]
    v5 = status.get("qualification_v5")
    if v5 is not None:
        lines += ["", "### QA-1 proposed gate (v5): NOT authoritative"]
        if not v5["available"]:
            lines.append(f"- Unavailable this run ({v5['reason']}); the v4 status above is unaffected.")
        else:
            lines += [
                f"- Verified qualifying real PRs: **{v5['verified']}/{v5['threshold']}** · verified green "
                f"{v5['verified_green']}/{v5['green_minimum']} · in an open sampling window "
                f"{v5['window_open']} · reference unknown {v5['reference_unknown']}",
                f"- Escalation {v5['escalation']} · sampling {v5['sampling']} · open exceptions "
                f"{v5['exceptions_open']} · confirmed material disagreements "
                f"{v5['confirmed_material_disagreements']} · met {v5['met']}",
                "- Proposed only: the gate of record is the v4 gate above until the founder adopts QA-1.",
                "", f"#### Founder queue ({len(v5['founder_queue'])})"]
            lines += [f"- #{q['pr_number']} head `{q['head_sha'][:12]}`: {', '.join(q['reasons'])}. "
                      f"Apply exactly one `faraday/founder-<green|yellow|red|block>` label while the "
                      f"head is `{q['head_sha'][:12]}`."
                      for q in v5["founder_queue"][:MAX_RENDERED_QUEUE]] or [
                          "- none: no exception or sample needs the founder"]
            if len(v5["founder_queue"]) > MAX_RENDERED_QUEUE:
                lines.append(f"- … and {len(v5['founder_queue']) - MAX_RENDERED_QUEUE} more "
                             "(full queue in status.json)")
    lines += ["", "### Stalled / incomplete"]
    lines.append("- Incomplete PRs: " + (", ".join(f"#{n}" for n in status["incomplete_prs"])
                                         or "none"))
    for run in status["runs_not_completed"]:
        flag = "stalled" if run["stalled"] else "running"
        lines.append(f"- Run {run['run_id']} attempt {run['run_attempt']}: {run['status']}, "
                     f"{run['age_hours']:g}h old ({flag})")
    if not status["runs_not_completed"]:
        lines.append("- Runs not completed: none")
    lines += ["", "### Synthetic exclusions"]
    lines += [f"- #{s['pr_number']} (in ledger: {s['present_in_ledger']})"
              for s in status["synthetic_excluded"]] or ["- none"]
    auth = status["authorization"]
    lines += ["", "### Authorization scan",
              f"- Files scanned: {auth['files_scanned']} · findings: {len(auth['findings'])} · "
              f"ledger authorization anomalies: {auth['ledger_anomalies']}"]
    for f in auth["findings"][:MAX_RENDERED_FINDINGS]:
        where = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
        lines.append(f"- `{f['field']}` {f['problem']} in `{where}`")
    if len(auth["findings"]) > MAX_RENDERED_FINDINGS:
        lines.append(f"- … and {len(auth['findings']) - MAX_RENDERED_FINDINGS} more "
                     "(full list in status.json)")
    if auth["unreadable"]:
        lines.append("- Unreadable (not verifiable): "
                     + ", ".join(f"`{p}`" for p in auth["unreadable"]))
    lines += ["", "### Kill switches",
              f"- Shadow (authorization or privacy failure): `{status['kill_switch']['command']}`",
              f"- This monitor: `gh variable delete {MONITOR_KILL_SWITCH} -R {status['repository']}`",
              "", status["note"], "", render_marker(status["marker"])]
    return "\n".join(lines) + "\n"


def _load_v5(path: Path, readiness_path: Path, ledger_raw: bytes) -> dict[str, Any]:
    v5 = _load_json(path, "qualification-v5 report")
    if persisted_local_diagnostics("qualification-v5", v5):
        raise MonitorError("qualification-v5 report fails its schema")
    ev = v5["evidence"]
    bare = lambda raw: _sha256(raw).removeprefix("sha256:")
    if ev.get("ledger") != bare(ledger_raw) or ev.get("readiness") != bare(readiness_path.read_bytes()):
        raise MonitorError("qualification-v5 report was not derived from this readiness and ledger")
    return v5


def monitor_report(*, readiness_path: Path, ledger_path: Path, manifest_paths: list[Path],
                   archive: Path, repository: str, generated_at: str,
                   stall_hours: int = DEFAULT_STALL_HOURS, v5_path: Path | None = None,
                   v5_unavailable: str | None = None) -> tuple[dict[str, Any], str]:
    if v5_path is not None and v5_unavailable is not None:
        raise MonitorError("give a qualification-v5 report or an unavailable reason, not both")
    if v5_unavailable is not None and not _KIND_RE.match(v5_unavailable):
        raise MonitorError("invalid qualification-v5 unavailable reason")
    if not _REPOSITORY_RE.match(repository):
        raise MonitorError("invalid --repository")
    require_timestamp(generated_at, "--generated-at")
    if stall_hours < 1:
        raise MonitorError("--stall-hours must be >= 1")
    readiness = _load_json(readiness_path, "readiness report")
    ledger = _load_json(ledger_path, "ledger")
    if persisted_local_diagnostics("qualification-readiness", readiness):
        raise MonitorError("readiness report fails its schema")
    if persisted_local_diagnostics("qualification-ledger", ledger):
        raise MonitorError("ledger fails its schema")
    ledger_raw = ledger_path.read_bytes()
    if readiness["evidence"]["ledger"]["sha256"] != _sha256(ledger_raw):
        raise MonitorError("readiness report was not derived from this ledger")
    files = _archive_files(archive, repository)
    if not manifest_paths:
        manifest_paths = [p for rel, p in files if "/" not in rel]
    manifests = []
    manifest_evidence = []
    ledger_inputs = {(i["kind"], i["sha256"]) for i in ledger["inputs"]}
    for path in sorted(manifest_paths):
        raw = path.read_bytes() if path.is_file() else b""
        doc = _load_json(path, "collection manifest")
        if persisted_local_diagnostics("qualification-collection-manifest", doc):
            raise MonitorError("a collection manifest fails its schema")
        if ("collection_manifest", _sha256(raw)) not in ledger_inputs:
            raise MonitorError("a collection manifest is not an input of this ledger")
        manifests.append(doc)
        manifest_evidence.append({"name": path.name, "sha256": _sha256(raw)})
    present = {rel: p for rel, p in files}
    for item in ledger["inputs"]:
        held = present.get(item["path"])
        if held is None or _sha256(held.read_bytes()) != item["sha256"]:
            raise MonitorError("a ledger input is missing from the archive or has changed")
    _, _, archived_docs = _scan_archive(files)
    _identity_guard(repository, [readiness, ledger, *manifests, *archived_docs])
    v5 = None
    if v5_path is not None:
        # The proposed v5 report can never stop the v4 status: a report that
        # fails any check is dropped (not rendered, not published; the
        # workflow deletes it) and reported as rejected.
        try:
            v5 = _load_v5(v5_path, readiness_path, ledger_raw)
            _identity_guard(repository, [v5])
            v5_summary(v5, None)  # every field the summary reads (Codex P2, #20)
        except (MonitorError, KeyError, TypeError, ValueError, AttributeError):
            v5, v5_unavailable = None, "v5_rejected"
    evidence = {"readiness": {"name": readiness_path.name,
                              "sha256": _sha256(readiness_path.read_bytes())},
                "ledger": {"name": ledger_path.name, "sha256": _sha256(ledger_raw)},
                "manifests": manifest_evidence, "archive_files": len(files)}
    status = build_status(readiness=readiness, ledger=ledger, manifests=manifests,
                          archive_files=files, repository=repository, generated_at=generated_at,
                          stall_hours=stall_hours, input_evidence=evidence, v5=v5,
                          v5_unavailable=v5_unavailable)
    if v5 is not None:
        evidence["qualification_v5"] = {"name": v5_path.name, "sha256": _sha256(v5_path.read_bytes())}
    _identity_guard(repository, [status])
    return status, render_markdown(status)


# ---------------------------------------------------------- notify


def parse_marker(body: str, repository: str) -> dict[str, Any] | None:
    """The last well-formed marker for `repository` in an issue body, or
    None. Anything malformed is ignored (treated as no prior report)."""
    found = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(MARKER_PREFIX) and line.endswith(MARKER_SUFFIX):
            found = line[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
    if found is None:
        return None
    try:
        marker = json.loads(found)
    except ValueError:
        return None
    if not isinstance(marker, dict) or marker.get("v") != MARKER_VERSION:
        return None
    if not isinstance(marker.get("repository"), str) or \
            repo_identity(marker["repository"]) != repo_identity(repository):
        return None
    cp = marker.get("checkpoint")
    if isinstance(cp, bool) or cp not in (0, *CHECKPOINTS):
        return None
    if marker.get("status") not in STATUSES:
        return None
    counts = marker.get("anomaly_counts")
    if not isinstance(counts, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
            for k, v in counts.items()):
        return None
    digest = marker.get("critical_digest")
    if digest is not None and not isinstance(digest, str):
        return None
    queue = marker.get("founder_queue", [])
    if not isinstance(queue, list) or not all(isinstance(q, str) for q in queue):
        return None
    total = marker.get("founder_queue_total", 0)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    qd = marker.get("founder_queue_digest")
    if qd is not None and not isinstance(qd, str):
        return None
    windows = marker.get("closed_windows", {})
    if not isinstance(windows, dict) or not all(
            isinstance(k, str) and k.isdigit() and isinstance(v, str) and len(v) == 16
            for k, v in windows.items()):
        return None
    return marker


def rerolled_windows(current: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    """Closed windows recorded by the previous run whose membership or samples
    differ now (or that vanished): sampling was re-rolled. Only windows the
    current marker still covers by index range are compared, so marker
    trimming is not mistaken for a re-roll."""
    prev = (previous or {}).get("closed_windows", {})
    cur = current.get("closed_windows", {})
    if not prev or "closed_windows" not in current:
        return []
    floor = min((int(k) for k in cur), default=None)
    out = []
    for k in sorted(prev, key=int):
        if floor is not None and int(k) < floor and len(cur) >= MAX_WINDOW_MARKER:
            continue
        if cur.get(k) != prev[k]:
            out.append(k)
    return out


def notification(current: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    """Reasons to post a comment (empty: update the body only)."""
    prev = previous or {"checkpoint": 0, "status": "OK", "anomaly_counts": {},
                        "critical_digest": None}
    reasons = []
    if current["checkpoint"] > prev["checkpoint"]:
        reasons.append("checkpoint_reached")
    if current["status"] == "CRITICAL" and (prev["status"] != "CRITICAL" or
                                            prev["critical_digest"] != current["critical_digest"]):
        reasons.append("critical")
    if any(v > prev["anomaly_counts"].get(k, 0) for k, v in current["anomaly_counts"].items()):
        reasons.append("new_anomaly")
    if set(current.get("founder_queue", [])) - set(prev.get("founder_queue", [])) or (
            current.get("founder_queue_total", 0) > MAX_QUEUE_MARKER
            and current.get("founder_queue_digest") != prev.get("founder_queue_digest")):
        reasons.append("founder_queue")
    if rerolled_windows(current, previous):
        reasons.append("sampling_rerolled")
    return reasons


def render_comment(status: dict[str, Any], previous: dict[str, Any] | None,
                   reasons: list[str]) -> str:
    marker = status["marker"]
    prev_counts = (previous or {}).get("anomaly_counts", {})
    lines = [f"**Faraday Review qualification monitor: {status['status']}**", ""]
    if "critical" in reasons:
        lines += ["**CRITICAL: authorization anomaly.**",
                  f"Run now: `{status['kill_switch']['command']}`", ""]
    if "checkpoint_reached" in reasons:
        q = status["qualification"]
        lines.append(f"- Checkpoint CP-{marker['checkpoint']} reached: "
                     f"{q['adjudicated_qualifying']}/{q['threshold']} adjudicated qualifying "
                     "real PRs.")
    if "new_anomaly" in reasons:
        grown = [f"`{k}` {prev_counts.get(k, 0)}→{v}" for k, v in marker["anomaly_counts"].items()
                 if v > prev_counts.get(k, 0)]
        lines.append("- New anomalies: " + ", ".join(grown))
    if "founder_queue" in reasons:
        new = sorted(set(marker.get("founder_queue", [])) - set((previous or {}).get("founder_queue", [])))
        lines.append("- QA-1 (proposed) founder queue, new: "
                     + ", ".join(f"#{q.split(':')[0]} ({q.split(':')[2]})" for q in new)
                     + ". Label each PR with exactly one `faraday/founder-<class>` at its current head.")
    if "sampling_rerolled" in reasons:
        changed = rerolled_windows(marker, previous)
        lines.append("- **QA-1 (proposed) EXCEPTION: closed sampling window(s) re-rolled**: "
                     + ", ".join(f"window {k}" for k in changed)
                     + ". A PR left the ledger or its recorded evidence changed; compare with the "
                       "previous status. Founder review required before relying on v5 sampling.")
    lines += ["", "The issue body holds the full status. Qualification evidence only; "
              "authorizes nothing."]
    return "\n".join(lines) + "\n"


def monitor_notify(*, status_path: Path, previous_body: str | None,
                   repository: str) -> tuple[list[str], str | None]:
    if not _REPOSITORY_RE.match(repository):
        raise MonitorError("invalid --repository")
    status = _load_json(status_path, "status document")
    if not isinstance(status, dict) or status.get("format") != MONITOR_FORMAT:
        raise MonitorError("not a qualification monitor status document")
    if not isinstance(status.get("repository"), str) or \
            repo_identity(status["repository"]) != repo_identity(repository):
        raise MonitorError("status document is for another repository; refusing")
    _identity_guard(repository, [status])
    if previous_body is None:
        return [], None   # a new issue: creating it is the notification
    previous = parse_marker(previous_body, repository)
    reasons = notification(status["marker"], previous)
    if not reasons:
        return [], None
    return reasons, render_comment(status, previous, reasons)
