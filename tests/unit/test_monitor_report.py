"""`faraday-review monitor-report` / `monitor-notify` (GHO-AUTONOMOUS-
PRODUCTION-010 M1): the scheduled qualification monitor's pure, read-only
status view.

Every scenario builds a real archive (`<owner>/<repo>/<run id>/<artifact>/`,
the layout `collect` writes), runs the real `aggregate` and `readiness`, and
then the CLI. Nothing in the gate is mocked or re-derived: the monitor must
report exactly what readiness computed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from qualification_support import DIGEST_A, REPO, sha, write_label, write_run

from faraday_review.canonical import write_canonical
from faraday_review.cli import main
from faraday_review.telemetry.qualification import aggregate, file_evidence, readiness

TS = "2026-11-20T07:00:00Z"
REQUIREMENTS = {DIGEST_A: {"green": False, "yellow": True, "red": True, "block": True}}
KILL = f"gh variable delete FARADAY_REVIEW_SHADOW_ENABLED -R {REPO}"
FOREIGN = "someone-else/hidden-repo"
SEEDS = (0, 1, 7, 19, 43, 101)


# ----------------------------------------------------------------- fixtures


class Archive:
    """An archive laid out exactly like `collect` output for one repository."""

    def __init__(self, tmp: Path, repo: str = REPO) -> None:
        self.tmp = tmp
        self.repo = repo
        self.root = tmp / "archive"
        self.root.mkdir(parents=True, exist_ok=True)
        # run_id -> manifest run (status, conclusion, created_at, attempt)
        self.runs: dict[int, dict] = {}

    def artifact_dir(self, run_id: int, pr: int, head: str, attempt: int = 1) -> str:
        return f"{run_id}/faraday-review-pr{pr}-{sha(head)}-a{attempt}"

    def run(self, run_id: int, pr: int, head: str | None = None, *, attempt: int = 1,
            created_at: str | None = None, **kwargs) -> Path:
        head = head or f"h{pr}"
        created = created_at or f"2026-11-16T10:{run_id % 60:02d}:00Z"
        self.runs[run_id] = {"status": "completed", "conclusion": "success",
                             "created_at": created, "run_attempt": attempt}
        return write_run(self.root / self.repo, self.artifact_dir(run_id, pr, head, attempt),
                         repo=self.repo, pr=pr, head=head, created_at=created,
                         event_id=f"00000000-0000-4000-8000-{run_id:012d}",
                         execution={"provider": "github_actions", "run_id": run_id,
                                    "run_attempt": attempt},
                         **kwargs)

    def label(self, run_id: int, pr: int, head: str | None = None, **kwargs) -> None:
        head = head or f"h{pr}"
        write_label(self.root / self.repo, self.artifact_dir(run_id, pr, head), repo=self.repo,
                    pr=pr, head=head, **kwargs)

    def bare_run(self, run_id: int, *, status: str = "completed", conclusion: str | None = "failure",
                 created_at: str = "2026-11-16T09:00:00Z", attempt: int = 1) -> None:
        """A run the manifest lists with no archived artifact."""
        self.runs[run_id] = {"status": status, "conclusion": conclusion,
                             "created_at": created_at, "run_attempt": attempt}

    def manifest(self, collected_at: str = "2026-11-20T06:59:00Z",
                 attempt_records: dict[int, list] | None = None) -> Path:
        base = self.root / self.repo
        runs = []
        for run_id in sorted(self.runs):
            meta = self.runs[run_id]
            artifacts = []
            run_dir = base / str(run_id)
            for art in sorted(run_dir.iterdir()) if run_dir.is_dir() else []:
                if not art.name.startswith("faraday-review-pr"):
                    continue
                files = []
                for f in sorted(art.iterdir()):
                    raw = f.read_bytes()
                    files.append({"name": f.name,
                                  "path": f.relative_to(self.root).as_posix(),
                                  "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                                  "size": len(raw)})
                pr_part, head_part, att = art.name[len("faraday-review-pr"):].split("-")
                artifacts.append({"artifact_id": run_id * 10 + int(att[1:]), "name": art.name,
                                  "pr_number": int(pr_part), "head_sha": head_part,
                                  "attempt": int(att[1:]), "size_in_bytes": 100, "digest": None,
                                  "expired": False, "expires_at": None, "archived": True,
                                  "files": files})
            run = {"run_id": run_id, "run_attempt": meta["run_attempt"],
                   "status": meta["status"], "conclusion": meta["conclusion"],
                   "created_at": meta["created_at"], "artifacts": artifacts}
            if attempt_records and run_id in attempt_records:
                run["attempt_records"] = attempt_records[run_id]
            runs.append(run)
        manifest = {"format": "faraday-review/qualification-collection/v1",
                    "collected_at": collected_at,
                    "repositories": [{"repository": self.repo, "visibility": "public",
                                      "workflow": ".github/workflows/faraday-review.yml",
                                      "runs": runs}]}
        stamp = collected_at.replace("-", "").replace(":", "")
        path = self.root / f"collection-manifest-{stamp}.json"
        write_canonical(path, manifest)
        return path

    def build(self, synthetic: list | None = None, *, manifest: bool = True) -> dict:
        manifests = [self.manifest()] if manifest else []
        out = self.tmp / "out"
        out.mkdir(exist_ok=True)
        ledger = aggregate(self.root, generated_at=TS)
        ledger_path = out / "ledger.json"
        write_canonical(ledger_path, ledger)
        synth_path = out / "synthetic-prs.json"
        write_canonical(synth_path, synthetic or [])
        report = readiness(ledger, synthetic or [], generated_at=TS,
                           ledger_evidence=file_evidence(ledger_path),
                           synthetic_evidence=file_evidence(synth_path),
                           released_policy_digests=[DIGEST_A],
                           review_requirements=REQUIREMENTS)
        readiness_path = out / "readiness.json"
        write_canonical(readiness_path, report)
        return {"ledger": ledger_path, "readiness": readiness_path, "manifests": manifests,
                "report": report}


def _argv(arc: Archive, built: dict, *, repository: str = REPO, extra: tuple = ()) -> list[str]:
    argv = ["monitor-report", "--readiness", str(built["readiness"]),
            "--ledger", str(built["ledger"]), "--archive-dir", str(arc.root),
            "--repository", repository, "--generated-at", TS,
            "--output", str(arc.tmp / "status.json"),
            "--markdown-output", str(arc.tmp / "status.md")]
    for m in built["manifests"]:
        argv += ["--manifest", str(m)]
    return argv + list(extra)


def _monitor(arc: Archive, built: dict, **kwargs) -> tuple[int, dict | None, str | None]:
    rc = main(_argv(arc, built, **kwargs))
    status_path, md_path = arc.tmp / "status.json", arc.tmp / "status.md"
    status = json.loads(status_path.read_text()) if status_path.exists() else None
    md = md_path.read_text() if md_path.exists() else None
    return rc, status, md


def _adjudicated(arc: Archive, n: int, start: int = 1) -> None:
    for pr in range(start, start + n):
        arc.run(100 + pr, pr)
        arc.label(100 + pr, pr, labeled_at=f"2026-11-16T12:{pr % 60:02d}:00Z")


# --------------------------------------------------------------- checkpoints


@pytest.mark.parametrize("n,reached,next_level", [
    (0, None, 5), (4, None, 5), (5, 5, 10), (9, 5, 10), (10, 10, 15), (19, 15, 20), (20, 20, None),
])
def test_checkpoint_thresholds(tmp_path, n, reached, next_level):
    arc = Archive(tmp_path)
    _adjudicated(arc, n)
    arc.run(900, 900)  # one more PR, real but unadjudicated, never counts
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    assert rc == 0
    report = built["report"]
    assert status["qualification"]["adjudicated_qualifying"] == n \
        == report["adjudicated_qualifying_real_pr_count"]
    assert status["qualification"]["threshold"] == report["gate"]["threshold"] == 20
    assert status["checkpoint"]["reached"] == reached
    assert status["checkpoint"]["next"] == next_level
    assert status["checkpoint"]["levels"] == [5, 10, 15, 20]
    assert f"**{n}/20**" in md
    label = f"CP-{reached}" if reached else "none"
    assert f"Checkpoint reached: {label}" in md


def test_checkpoint_helper_boundaries():
    from faraday_review.telemetry.monitor import checkpoint_reached
    assert [checkpoint_reached(n) for n in (0, 4, 5, 9, 10, 14, 15, 19, 20, 21, 99)] == \
        [None, None, 5, 5, 10, 10, 15, 15, 20, 20, 20]


# ------------------------------------------------------------ count sections


def test_counts_exclusions_pending_and_synthetic(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 2)                                   # PR 1, 2: adjudicated qualifying
    arc.run(110, 10)                                       # PR 10: qualifying, unadjudicated
    arc.run(111, 11, cls="block", reasons=("policy_digest_unpinned",))   # misconfigured
    arc.run(112, 12, final_status="failed", failure_mode="required_checks_pending_timeout",
            cls=None, with_decision=False)                                      # incomplete (failed only)
    arc.run(113, 13, head="old13", created_at="2026-11-16T08:00:00Z")    # label on superseded
    arc.label(113, 13, head="old13", labeled_at="2026-11-16T08:30:00Z")
    arc.run(114, 13, head="new13", created_at="2026-11-16T09:00:00Z")    # current head
    arc.run(115, 23)                                       # synthetic
    built = arc.build(synthetic=[{"repository": REPO, "pr_number": 23, "reason": "smoke"},
                                 {"repository": REPO, "pr_number": 99, "reason": "never ran"}])
    rc, status, md = _monitor(arc, built)
    report = built["report"]
    assert rc == 0
    q = status["qualification"]
    assert q["real_pr_count"] == report["real_pr_count"]
    assert q["qualifying"] == report["qualifying_real_pr_count"] == 4
    assert q["adjudicated_qualifying"] == 2
    assert q["exclusions"] == report["qualification"]["exclusions"]
    assert q["exclusions"]["misconfigured"] == 1 and q["exclusions"]["incomplete"] == 1
    assert q["exclusions"]["unadjudicated"] == 2
    # Pending = current head eligible, no valid founder adjudication; the
    # label on PR 13's superseded head does not count.
    assert status["pending_adjudications"] == [
        {"pr_number": 10, "head_sha": sha("h10")},
        {"pr_number": 13, "head_sha": sha("new13")},
    ]
    assert "#10" in md and sha("new13")[:12] in md
    assert status["incomplete_prs"] == [12]
    assert status["synthetic_excluded"] == [{"pr_number": 23, "present_in_ledger": True},
                                            {"pr_number": 99, "present_in_ledger": False}]
    assert "smoke" not in md  # free-text reasons are never echoed
    assert status["status"] == "ATTENTION"


def test_pending_list_derives_only_from_readiness_status(tmp_path, monkeypatch):
    """The monitor must not re-derive qualification: it reports whatever
    status readiness recorded."""
    arc = Archive(tmp_path)
    _adjudicated(arc, 1)
    arc.run(110, 10)
    built = arc.build()
    report = json.loads(built["readiness"].read_text())
    for pr in report["counted_prs"]:
        pr["qualification"]["status"] = "unadjudicated"
    report["adjudicated_qualifying_real_pr_count"] = 0
    write_canonical(built["readiness"], report)
    rc, status, _ = _monitor(arc, built)
    assert rc == 0
    assert [p["pr_number"] for p in status["pending_adjudications"]] == [1, 10]


def test_escalation_agreement_latency_copied_from_readiness(tmp_path):
    arc = Archive(tmp_path)
    for pr, (cls, esc, label) in enumerate([("yellow", True, "faraday/founder-yellow"),
                                            ("green", False, "faraday/founder-red"),
                                            ("green", False, "faraday/founder-green")], start=1):
        arc.run(100 + pr, pr, cls=cls, escalated=esc)
        arc.label(100 + pr, pr, label=label, labeled_at=f"2026-11-16T1{pr}:00:00Z")
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    report = built["report"]
    assert rc == 0
    esc = status["escalation"]
    cond = report["gate"]["escalation_condition"]
    acc = report["escalation_accuracy"]
    assert esc["condition"] == cond["status"] == "fail"
    assert esc["missed"] == cond["missed"] == 1
    assert esc["unknown"] == cond["unknown"]
    assert esc["mandatory_total"] == cond["mandatory_total"] == 2
    assert esc["matrix"] == acc["matrix"]
    assert esc["missed_prs"] == [2]
    agr = status["agreement"]
    assert agr["rate"] == report["agreement"]["agreement_rate"]
    assert agr["compared"] == 3
    lat = report["founder_latency"]["all_adjudicated"]
    assert status["latency"]["median_seconds"] == lat["median_seconds"] is not None
    assert status["latency"]["p90_seconds"] == lat["p90_seconds"]
    assert "Escalation condition: **fail**" in md
    assert status["gate"]["met"] is False
    assert status["status"] == "ATTENTION"


def test_agreement_and_latency_absent_render_na(tmp_path):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    assert rc == 0
    assert status["agreement"]["rate"] is None
    assert status["latency"]["median_seconds"] is None
    assert "Agreement rate: n/a" in md
    assert "median n/a" in md
    assert status["escalation"]["condition"] == "unknown"


def test_infrastructure_failures_reported(tmp_path):
    arc = Archive(tmp_path)
    arc.run(110, 10, final_status="failed", failure_mode="required_checks_pending_timeout", cls=None,
            with_decision=False)
    arc.run(111, 11, final_status="failed", failure_mode="github_api_budget_exceeded", cls=None,
            with_decision=False)
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    infra = built["report"]["drift"]["infrastructure_failures"]
    assert rc == 0
    assert status["infrastructure"]["failed_runs"] == infra["failed_runs"] == 2
    assert status["infrastructure"]["failure_modes"] == infra["failure_modes"]
    assert status["infrastructure"]["failure_modes"]["required_checks_pending_timeout"] == 1
    assert status["infrastructure"]["api_budget_exceeded_runs"] == infra["api_budget_exceeded_runs"]
    assert "required_checks_pending_timeout" in md
    assert status["status"] == "ATTENTION"


# ------------------------------------------------------------- anomalies


def test_anomalies_grouped_by_kind(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 1)
    arc.run(110, 10, with_decision=False)          # policy_digest_unverified
    arc.bare_run(200)                              # completed failure, no artifact
    arc.bare_run(201)
    arc.bare_run(202, status="completed", conclusion="success", attempt=1)
    built_manifest = arc.manifest(attempt_records={202: [{
        "attempt": 1, "provenance": "check_run",
        "external_id": "faraday-review-attempt:202:1", "status": "rejected",
        "accepted_check_run_ids": [], "rejected": [{"check_run_id": 7, "reason": "sha256_mismatch"}],
        "files": []}]})
    built = arc.build(manifest=False)
    built["manifests"] = [built_manifest]
    rc, status, md = _monitor(arc, built)
    assert rc == 0
    kinds = status["anomalies"]["by_kind"]
    report_kinds: dict[str, int] = {}
    for a in built["report"]["anomalies"]:
        report_kinds[a["kind"]] = report_kinds.get(a["kind"], 0) + 1
    assert kinds == report_kinds
    assert kinds["run_without_telemetry"] == 3
    assert kinds["attempt_record_rejected"] == 1
    assert kinds["policy_digest_unverified"] >= 1
    assert status["anomalies"]["total"] == sum(kinds.values())
    assert list(kinds) == sorted(kinds)
    for kind in ("run_without_telemetry", "attempt_record_rejected", "policy_digest_unverified"):
        assert f"`{kind}`" in md
    assert status["status"] == "ATTENTION"


def test_stalled_and_incomplete_runs(tmp_path):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    arc.bare_run(300, status="in_progress", conclusion=None, created_at="2026-11-19T20:00:00Z")
    arc.bare_run(301, status="queued", conclusion=None, created_at="2026-11-20T06:00:00Z")
    built = arc.build()
    rc, status, md = _monitor(arc, built, extra=("--stall-hours", "6"))
    assert rc == 0
    assert status["runs_not_completed"] == [
        {"run_id": 300, "run_attempt": 1, "status": "in_progress", "age_hours": 11.0,
         "stalled": True},
        {"run_id": 301, "run_attempt": 1, "status": "queued", "age_hours": 1.0, "stalled": False},
    ]
    assert status["stalled_runs"] == 1
    assert "300" in md and "stalled" in md


def test_clean_state_is_ok(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 5)
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    assert rc == 0
    assert status["anomalies"]["total"] == 0, built["report"]["anomalies"]
    assert status["status"] == "OK"
    assert status["authorization"]["findings"] == []
    assert "Run now" not in md
    # The standing kill-switch references are always present, informational.
    assert "FARADAY_REVIEW_MONITOR_ENABLED" in md


# --------------------------------------------------- authorization anomaly


def _rewrite_json(path: Path, **changes) -> None:
    doc = json.loads(path.read_text())
    doc.update(changes)
    write_canonical(path, doc)


def _rewrite_telemetry(path: Path, **changes) -> None:
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for doc in lines:
        doc.update(changes)
    path.write_text("".join(json.dumps(d, sort_keys=True, separators=(",", ":")) + "\n"
                            for d in lines))


@pytest.mark.parametrize("where,field,value,problem", [
    ("decision", "approval_authorized", True, "true"),
    ("decision", "merge_authorized", True, "true"),
    ("telemetry", "approval_authorized", True, "true"),
    ("telemetry", "merge_authorized", True, "true"),
    ("decision", "merge_authorized", "no", "unexpected_value"),
    ("decision", "risk_class", "purple", "unexpected_value"),
    ("telemetry", "risk_class", "purple", "unexpected_value"),
    ("decision", "founder_review_required", "yes", "unexpected_value"),
    ("attempt_record", "approval_authorized", True, "true"),
])
def test_authorization_anomaly_is_critical(tmp_path, where, field, value, problem):
    arc = Archive(tmp_path)
    _adjudicated(arc, 1)
    run_dir = arc.run(110, 10)
    if where == "decision":
        target = run_dir / "decision.json"
        _rewrite_json(target, **{field: value})
    elif where == "telemetry":
        target = run_dir / "telemetry.jsonl"
        _rewrite_telemetry(target, **{field: value})
    else:
        rec = arc.root / REPO / "110" / "attempt-record-a2"
        rec.mkdir(parents=True)
        target = rec / "telemetry.jsonl"
        target.write_text((run_dir / "telemetry.jsonl").read_text())
        _rewrite_telemetry(target, **{field: value})
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    assert rc == 3
    assert status["status"] == "CRITICAL"
    findings = status["authorization"]["findings"]
    rel = target.relative_to(arc.root).as_posix()
    assert {"path": rel, "field": field, "problem": problem} in \
        [{k: f[k] for k in ("path", "field", "problem")} for f in findings]
    assert KILL in md
    assert "Run now" in md
    assert status["kill_switch"]["command"] == KILL
    # Values are never echoed.
    assert "purple" not in md and "purple" not in json.dumps(status)


def test_decision_missing_authorization_field_is_critical(tmp_path):
    arc = Archive(tmp_path)
    run_dir = arc.run(110, 10)
    doc = json.loads((run_dir / "decision.json").read_text())
    del doc["merge_authorized"]
    write_canonical(run_dir / "decision.json", doc)
    built = arc.build()
    rc, status, _ = _monitor(arc, built)
    assert rc == 3 and status["status"] == "CRITICAL"
    assert any(f["field"] == "merge_authorized" and f["problem"] == "missing"
               for f in status["authorization"]["findings"])


def test_ledger_authorization_anomaly_alone_is_critical(tmp_path):
    """A ledger anomaly `authorization_flag_true_in_telemetry` is CRITICAL even
    when the scanned files look clean (defence in depth)."""
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    ledger = json.loads(built["ledger"].read_text())
    ledger["anomalies"].append({"kind": "authorization_flag_true_in_telemetry",
                                "review_id": "frr-x", "source": f"{REPO}/110/x/telemetry.jsonl"})
    write_canonical(built["ledger"], ledger)
    report = json.loads(built["readiness"].read_text())
    report["evidence"]["ledger"] = file_evidence(built["ledger"])
    write_canonical(built["readiness"], report)
    rc, status, md = _monitor(arc, built)
    assert rc == 3 and status["status"] == "CRITICAL"
    assert status["authorization"]["ledger_anomalies"] == 1
    assert KILL in md


# ------------------------------------------------------ identity refusal


def _refused(capsys, arc, rc) -> None:
    assert rc == 2
    assert not (arc.tmp / "status.json").exists()
    assert not (arc.tmp / "status.md").exists()
    err = capsys.readouterr().err
    assert FOREIGN not in err and FOREIGN.casefold() not in err.casefold()


def test_foreign_repository_in_archive_is_refused(tmp_path, capsys):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    other = Archive(tmp_path, repo=FOREIGN)
    other.run(500, 1)
    built = arc.build()
    _refused(capsys, arc, main(_argv(arc, built)))


def test_foreign_path_without_identity_content_is_refused(tmp_path, capsys):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    stray = arc.root / FOREIGN / "1" / "x"
    stray.mkdir(parents=True)
    (stray / "notes.txt").write_text("nothing to see\n")
    _refused(capsys, arc, main(_argv(arc, built)))


def test_repository_argument_mismatch_is_refused(tmp_path, capsys):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    _refused(capsys, arc, main(_argv(arc, built, repository=FOREIGN)))


def test_foreign_synthetic_entry_is_refused(tmp_path, capsys):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build(synthetic=[{"repository": FOREIGN, "pr_number": 3, "reason": "x"}])
    _refused(capsys, arc, main(_argv(arc, built)))


def test_foreign_identity_inside_archived_file_is_refused(tmp_path, capsys):
    arc = Archive(tmp_path)
    run_dir = arc.run(110, 10)
    binding = {"caller_repo": FOREIGN, "pr_number": "10"}
    (run_dir / "run-binding.json").write_text(json.dumps(binding))
    built = arc.build()
    _refused(capsys, arc, main(_argv(arc, built)))


def test_case_variant_repository_is_accepted(tmp_path):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    rc, status, _ = _monitor(arc, built, repository=REPO.upper())
    assert rc == 0
    assert status["repository"] == REPO.upper()


def test_invalid_repository_argument_is_refused(tmp_path, capsys):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    assert main(_argv(arc, built, repository="not a repo")) == 2


# ------------------------------------------------------- input integrity


def test_readiness_not_from_this_ledger_is_refused(tmp_path):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    ledger = json.loads(built["ledger"].read_text())
    ledger["generated_at"] = "2026-11-20T08:00:00Z"
    write_canonical(built["ledger"], ledger)
    assert main(_argv(arc, built)) == 2


def test_ledger_input_missing_from_archive_is_refused(tmp_path):
    arc = Archive(tmp_path)
    run_dir = arc.run(110, 10)
    built = arc.build()
    (run_dir / "decision.json").write_text("{}\n")
    assert main(_argv(arc, built)) == 2


def test_manifest_not_in_ledger_is_refused(tmp_path):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    extra = arc.manifest(collected_at="2026-11-20T06:59:30Z")
    built["manifests"].append(extra)
    assert main(_argv(arc, built)) == 2


def test_symlink_in_archive_is_refused(tmp_path):
    arc = Archive(tmp_path)
    run_dir = arc.run(110, 10)
    built = arc.build()
    (run_dir / "link.json").symlink_to(run_dir / "decision.json")
    assert main(_argv(arc, built)) == 2


def test_schema_invalid_readiness_is_refused(tmp_path):
    arc = Archive(tmp_path)
    arc.run(110, 10)
    built = arc.build()
    report = json.loads(built["readiness"].read_text())
    report["format"] = "something-else"
    write_canonical(built["readiness"], report)
    assert main(_argv(arc, built)) == 2


def test_unreadable_archived_file_is_attention_not_silent(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 5)
    rec = arc.root / REPO / "101" / "attempt-record-a2"
    rec.mkdir(parents=True)
    (rec / "decision.json").write_text("{not json")
    built = arc.build()
    rc, status, _ = _monitor(arc, built)
    assert rc == 0
    assert status["authorization"]["unreadable"] == [f"{REPO}/101/attempt-record-a2/decision.json"]
    assert status["status"] == "ATTENTION"


# ------------------------------------------------------------ determinism


def test_output_deterministic_across_hash_seeds(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 6)
    arc.run(110, 10)
    arc.bare_run(200)
    arc.run(111, 11, final_status="failed", failure_mode="required_checks_pending_timeout", cls=None,
            with_decision=False)
    built = arc.build(synthetic=[{"repository": REPO, "pr_number": 23, "reason": "smoke"}])
    outputs = set()
    for seed in SEEDS:
        out = tmp_path / f"seed-{seed}"
        out.mkdir()
        argv = _argv(arc, built)
        argv[argv.index("--output") + 1] = str(out / "status.json")
        argv[argv.index("--markdown-output") + 1] = str(out / "status.md")
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        proc = subprocess.run([sys.executable, "-c",
                               "import sys; from faraday_review.cli import main; "
                               "raise SystemExit(main(sys.argv[1:]))", *argv],
                              env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        outputs.add(((out / "status.json").read_bytes(), (out / "status.md").read_bytes()))
    assert len(outputs) == 1


# ------------------------------------------------------- notification


def _status_doc(tmp_path, n_adjudicated: int, *, bare_runs: int = 0, critical: bool = False):
    arc = Archive(tmp_path)
    _adjudicated(arc, n_adjudicated)
    for i in range(bare_runs):
        arc.bare_run(700 + i)
    if critical:
        run_dir = arc.run(110, 10)
        _rewrite_json(run_dir / "decision.json", approval_authorized=True)
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    assert rc == (3 if critical else 0)
    return arc.tmp / "status.json", md


def _notify(tmp_path, status_path: Path, previous: str | None) -> tuple[int, str | None]:
    prev = tmp_path / "previous.md"
    if previous is not None:
        prev.write_text(previous)
    comment = tmp_path / "comment.md"
    if comment.exists():
        comment.unlink()
    argv = ["monitor-notify", "--status", str(status_path), "--repository", REPO,
            "--comment-output", str(comment)]
    if previous is not None:
        argv += ["--previous-body", str(prev)]
    rc = main(argv)
    return rc, comment.read_text() if comment.exists() else None


def test_notify_comments_only_when_checkpoint_increases(tmp_path):
    s4, md4 = _status_doc(tmp_path / "a", 4)
    s5, md5 = _status_doc(tmp_path / "b", 5)
    s9, md9 = _status_doc(tmp_path / "c", 9)
    rc, comment = _notify(tmp_path, s5, md4)
    assert rc == 0 and comment is not None and "CP-5" in comment
    rc, comment = _notify(tmp_path, s5, md5)
    assert rc == 0 and comment is None            # unchanged: body update only
    rc, comment = _notify(tmp_path, s9, md5)
    assert rc == 0 and comment is None            # 9 is still CP-5


def test_notify_new_issue_has_no_comment(tmp_path):
    s5, _ = _status_doc(tmp_path / "a", 5)
    rc, comment = _notify(tmp_path, s5, None)
    assert rc == 0 and comment is None


def test_notify_new_anomaly_comments_once(tmp_path):
    base, md_base = _status_doc(tmp_path / "a", 1)
    worse, md_worse = _status_doc(tmp_path / "b", 1, bare_runs=1)
    rc, comment = _notify(tmp_path, worse, md_base)
    assert rc == 0 and comment is not None and "run_without_telemetry" in comment
    rc, comment = _notify(tmp_path, worse, md_worse)
    assert comment is None
    rc, comment = _notify(tmp_path, base, md_worse)   # anomalies went away: no alert
    assert comment is None


def test_notify_critical_comments_with_kill_switch_once(tmp_path):
    ok, md_ok = _status_doc(tmp_path / "a", 1)
    crit, md_crit = _status_doc(tmp_path / "b", 1, critical=True)
    rc, comment = _notify(tmp_path, crit, md_ok)
    assert rc == 0 and comment is not None
    assert "CRITICAL" in comment and KILL in comment
    rc, comment = _notify(tmp_path, crit, md_crit)
    assert comment is None


def test_notify_ignores_forged_or_foreign_marker(tmp_path):
    s5, md5 = _status_doc(tmp_path / "a", 5)
    forged = md5.replace('"checkpoint":5', '"checkpoint":"x"')
    rc, comment = _notify(tmp_path, s5, forged)
    assert rc == 0 and comment is not None        # treated as no prior report
    foreign = md5.replace(REPO, FOREIGN)
    rc, comment = _notify(tmp_path, s5, foreign)
    assert rc == 0 and comment is not None


def test_notify_refuses_status_of_another_repository(tmp_path):
    s5, md5 = _status_doc(tmp_path / "a", 5)
    rc = main(["monitor-notify", "--status", str(s5), "--repository", FOREIGN,
               "--comment-output", str(tmp_path / "c.md")])
    assert rc == 2
    assert not (tmp_path / "c.md").exists()


def test_marker_is_last_line_of_markdown(tmp_path):
    _, md = _status_doc(tmp_path / "a", 5)
    last = md.rstrip("\n").splitlines()[-1]
    assert last.startswith("<!-- faraday-review-qualification-monitor: ")
    assert last.endswith(" -->")


# ----------------------------------------- Codex P2 (r45): JSONL scan continues past bad lines


def _inject_before_violation(path: Path, bad_line: str) -> None:
    """Rewrite a telemetry JSONL so a bad line precedes a record whose
    merge_authorized is true; the original first record stays first."""
    first = [json.loads(line) for line in path.read_text().splitlines() if line.strip()][0]
    violation = dict(first, merge_authorized=True)
    path.write_text(json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n"
                    + bad_line + "\n"
                    + json.dumps(violation, sort_keys=True, separators=(",", ":")) + "\n")


@pytest.mark.parametrize("bad_line", ["{not json", "[1, 2, 3]", "42", "\"a string\"", "null"])
@pytest.mark.parametrize("where", ["telemetry", "attempt_record"])
def test_bad_jsonl_line_never_hides_a_later_authorization_violation(tmp_path, where, bad_line):
    arc = Archive(tmp_path)
    _adjudicated(arc, 1)
    run_dir = arc.run(110, 10)
    if where == "telemetry":
        target = run_dir / "telemetry.jsonl"
    else:
        rec = arc.root / REPO / "110" / "attempt-record-a2"
        rec.mkdir(parents=True)
        target = rec / "telemetry.jsonl"
        target.write_text((run_dir / "telemetry.jsonl").read_text())
    _inject_before_violation(target, bad_line)
    built = arc.build()
    rc, status, md = _monitor(arc, built)
    rel = target.relative_to(arc.root).as_posix()
    # The violation on line 3 is still found: CRITICAL, kill switch, exit 3.
    assert rc == 3
    assert status["status"] == "CRITICAL"
    assert {"path": rel, "line": 3, "field": "merge_authorized", "problem": "true"} in \
        [{k: f[k] for k in ("path", "line", "field", "problem")}
         for f in status["authorization"]["findings"]]
    assert status["kill_switch"]["command"] == KILL
    assert KILL in md and "Run now" in md
    # The bad line is still surfaced as unreadable evidence, not silently dropped.
    assert any(u == rel or u.startswith(rel + ":") for u in status["authorization"]["unreadable"])


def test_bad_jsonl_line_alone_is_attention_not_ok(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 1)
    run_dir = arc.run(110, 10)
    target = run_dir / "telemetry.jsonl"
    first = target.read_text().splitlines()[0]
    target.write_text(first + "\n{not json\n")
    built = arc.build()
    rc, status, _ = _monitor(arc, built)
    assert status["status"] == "ATTENTION" and rc == 0
    rel = target.relative_to(arc.root).as_posix()
    assert any(u == rel or u.startswith(rel + ":") for u in status["authorization"]["unreadable"])
