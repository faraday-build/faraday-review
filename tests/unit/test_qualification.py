"""Phase 1 qualification: adjudicate / aggregate / readiness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from qualification_support import (
    DIGEST_A,
    DIGEST_B,
    FOUNDER,
    REPO,
    sha,
    write_label,
    write_real_prs,
    write_run,
)

from faraday_review.canonical import canonical_bytes, read_canonical
from faraday_review.cli import main
from faraday_review.telemetry.qualification import (
    GATE_THRESHOLD_FLOOR,
    QualificationError,
    aggregate,
    local_validation_errors,
    readiness,
)

TS = "2026-11-12T00:00:00Z"
EVIDENCE = {"name": "x.json", "sha256": "sha256:" + "0" * 64}
SYNTH_REPO = "faraday-build/sandbox"


def _ledger(root: Path) -> dict:
    ledger = aggregate(root, generated_at=TS)
    assert local_validation_errors("qualification-ledger", ledger) == []
    return ledger


def _report(ledger: dict, synthetic: list | None = None, threshold: int = 20) -> dict:
    report = readiness(ledger, synthetic or [], generated_at=TS, ledger_evidence=EVIDENCE,
                       synthetic_evidence=EVIDENCE, threshold=threshold)
    assert local_validation_errors("qualification-readiness", report) == []
    return report


def _adjudicate(tmp_path: Path, **overrides) -> tuple[int, list[dict]]:
    args = {
        "--repository": REPO, "--pr": "7", "--head-sha": sha("h7"),
        "--label": "faraday/founder-red", "--actor-node-id": FOUNDER,
        "--actor-login": "tomrivera-PSE", "--labeled-at": "2026-11-01T12:00:00Z",
        "--artifacts-dir": str(tmp_path),
    }
    args.update(overrides)
    code = main(["adjudicate", *[x for kv in args.items() for x in kv]])
    path = tmp_path / "adjudications.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return code, [json.loads(line) for line in lines]


# ------------------------------------------------------------- adjudicate


def test_adjudicate_accepts_founder_node_id(tmp_path):
    code, records = _adjudicate(tmp_path)
    assert code == 0
    assert records == [{
        "record_type": "founder_adjudication", "repository": REPO, "pr_number": 7,
        "head_sha": sha("h7"), "label": "faraday/founder-red",
        "labeled_at": "2026-11-01T12:00:00Z",
        "actor": {"node_id": FOUNDER, "login": "tomrivera-PSE"},
        "founder_node_id": FOUNDER, "status": "accepted", "founder_classification": "red",
    }]


def test_adjudicate_rejects_non_founder_node_id(tmp_path):
    code, records = _adjudicate(tmp_path, **{"--actor-node-id": "U_someoneElse",
                                             "--actor-login": "someone"})
    assert code == 0
    assert records[0]["status"] == "rejected"
    assert records[0]["reject_reason"] == "actor_not_founder"
    assert records[0]["actor"] == {"node_id": "U_someoneElse", "login": "someone"}
    assert "founder_classification" not in records[0]


def test_adjudicate_login_spoof_rejected(tmp_path):
    """Founder's login with the wrong node_id is rejected: login never matches."""
    code, records = _adjudicate(tmp_path, **{"--actor-node-id": "BOT_kgDOEeALbA",
                                             "--actor-login": "tomrivera-PSE"})
    assert code == 0
    assert records[0]["status"] == "rejected"
    assert records[0]["reject_reason"] == "actor_not_founder"


def test_adjudicate_unrecognized_label_rejected(tmp_path):
    code, records = _adjudicate(tmp_path, **{"--label": "faraday/founder-purple"})
    assert code == 0
    assert records[0]["status"] == "rejected"
    assert records[0]["reject_reason"] == "unrecognized_label"


def test_adjudicate_is_append_only(tmp_path):
    _adjudicate(tmp_path)
    code, records = _adjudicate(tmp_path, **{"--label": "faraday/founder-green"})
    assert code == 0
    assert [r["founder_classification"] for r in records] == ["red", "green"]


@pytest.mark.parametrize("override", [
    {"--head-sha": "not-a-sha"}, {"--labeled-at": "yesterday"},
    {"--labeled-at": "2026-11-01T12:00:00"}, {"--repository": "no-slash"},
])
def test_adjudicate_invalid_input_exits_2(tmp_path, override):
    code, records = _adjudicate(tmp_path, **override)
    assert code == 2
    assert records == []


# -------------------------------------------------------------- aggregate


def test_aggregate_dedupes_identical_and_rerun_events(tmp_path):
    write_run(tmp_path, "artifact-a", pr=1, head="h1")
    write_run(tmp_path, "download-again/artifact-a", pr=1, head="h1")  # byte-identical
    write_run(tmp_path, "rerun", pr=1, head="h1", created_at="2026-11-01T10:05:00Z")
    ledger = _ledger(tmp_path)
    assert ledger["counts"]["events_read"] == 3
    assert ledger["counts"]["runs_after_dedup"] == 1
    assert ledger["counts"]["duplicates_collapsed"] == 2
    (entry,) = ledger["entries"]
    (run,) = entry["runs"]
    assert run["occurrences"] == 3
    assert run["distinct_payloads"] == 2
    assert run["created_at"] == "2026-11-01T10:00:00Z"
    assert run["last_created_at"] == "2026-11-01T10:05:00Z"
    assert run["policy_digest_verified"] is True
    assert run["policy_bundle_digest"] == DIGEST_A


def test_aggregate_marks_superseded_heads(tmp_path):
    write_run(tmp_path, "r1", pr=3, head="old", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "r2", pr=3, head="new", created_at="2026-11-01T11:00:00Z")
    ledger = _ledger(tmp_path)
    flags = {e["head_sha"]: e["superseded"] for e in ledger["entries"]}
    assert flags == {sha("old"): True, sha("new"): False}
    assert ledger["counts"]["superseded_entries"] == 1
    report = _report(ledger)
    assert report["real_pr_count"] == 1
    assert report["counted_prs"][0]["head_sha"] == sha("new")


def test_adjudication_attached_only_to_matching_head(tmp_path):
    write_run(tmp_path, "r1", pr=3, head="old", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "r2", pr=3, head="new", created_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "l1", pr=3, head="old", label="faraday/founder-green")
    write_label(tmp_path, "l2", pr=3, head="nowhere", label="faraday/founder-red")
    ledger = _ledger(tmp_path)
    by_head = {e["head_sha"]: e["founder_adjudication"] for e in ledger["entries"]}
    assert by_head[sha("old")]["founder_classification"] == "green"
    assert by_head[sha("new")] is None
    assert [u["head_sha"] for u in ledger["adjudications"]["unmatched"]] == [sha("nowhere")]
    # An adjudication of a superseded head does not count for the current head.
    assert _report(ledger)["adjudicated_count"] == 0


def test_rejected_adjudications_counted_never_applied(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1")
    write_label(tmp_path, "l1", pr=1, head="h1", node_id="BOT_x", login="tomrivera-PSE",
                label="faraday/founder-green")
    write_label(tmp_path, "l2", pr=1, head="h1", label="faraday/founder-nope")
    ledger = _ledger(tmp_path)
    assert ledger["adjudications"]["rejected_count"] == 2
    assert ledger["adjudications"]["accepted_count"] == 0
    assert ledger["entries"][0]["founder_adjudication"] is None


def test_tampered_accepted_record_demoted_at_aggregate(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1")
    forged = {"record_type": "founder_adjudication", "repository": REPO, "pr_number": 1,
              "head_sha": sha("h1"), "label": "faraday/founder-green",
              "labeled_at": "2026-11-01T11:00:00Z",
              "actor": {"node_id": "U_attacker", "login": "tomrivera-PSE"},
              "founder_node_id": "U_attacker", "status": "accepted",
              "founder_classification": "green"}
    (tmp_path / "forged").mkdir()
    (tmp_path / "forged" / "adjudications.jsonl").write_text(json.dumps(forged) + "\n")
    ledger = _ledger(tmp_path)
    assert ledger["entries"][0]["founder_adjudication"] is None
    assert ledger["adjudications"]["rejected"][0]["reject_reason"] == "actor_not_founder_at_aggregate"


def test_latest_founder_label_wins_and_relabel_flagged(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1", cls="red")
    write_label(tmp_path, "l1", pr=1, head="h1", label="faraday/founder-yellow",
                labeled_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "l2", pr=1, head="h1", label="faraday/founder-red",
                labeled_at="2026-11-01T12:00:00Z")
    ledger = _ledger(tmp_path)
    adj = ledger["entries"][0]["founder_adjudication"]
    assert adj["founder_classification"] == "red"
    assert adj["accepted_records"] == 2
    assert any(a["kind"] == "founder_relabeled_head" for a in ledger["anomalies"])


def test_malformed_and_invalid_lines_are_anomalies_not_runs(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "telemetry.jsonl").write_text('{not json\n{"review_id": "x"}\n')
    ledger = _ledger(tmp_path)
    kinds = sorted(a["kind"] for a in ledger["anomalies"])
    assert kinds == ["invalid_telemetry_event", "malformed_line"]
    assert ledger["counts"]["runs_after_dedup"] == 1


def test_missing_decision_leaves_digest_unverified(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1", with_decision=False)
    ledger = _ledger(tmp_path)
    run = ledger["entries"][0]["runs"][0]
    assert run["policy_digest_verified"] is False
    assert run["policy_bundle_digest"] is None
    assert _report(ledger)["drift"]["policy_digest_unverified_runs"] == 1


def test_ledger_records_input_hashes(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1")
    inputs = _ledger(tmp_path)["inputs"]
    assert [(i["path"], i["kind"]) for i in inputs] == [
        ("r1/decision.json", "decision"), ("r1/telemetry.jsonl", "telemetry")]
    assert all(i["sha256"].startswith("sha256:") for i in inputs)


# -------------------------------------------------------------- readiness


def test_threshold_floor_rejects_19(tmp_path):
    write_real_prs(tmp_path, 20)
    with pytest.raises(QualificationError):
        _report(_ledger(tmp_path), threshold=19)


def test_threshold_floor_cli_exit_2(tmp_path):
    write_real_prs(tmp_path / "in", 1)
    ledger, synth, out = tmp_path / "l.json", tmp_path / "s.json", tmp_path / "r.json"
    synth.write_text("[]")
    assert main(["aggregate", "--input-dir", str(tmp_path / "in"), "--output", str(ledger),
                 "--generated-at", TS]) == 0
    base = ["readiness", "--ledger", str(ledger), "--synthetic", str(synth),
            "--generated-at", TS, "--output", str(out)]
    assert main([*base, "--threshold", "19"]) == 2
    assert not out.exists()
    assert main([*base, "--threshold", "25"]) == 0
    assert read_canonical(out)["gate"] == {"threshold": 25, "criterion": "real_pr_count >= threshold",
                                           "met": False}


def test_gate_not_met_at_19_real(tmp_path):
    write_real_prs(tmp_path, 19)
    report = _report(_ledger(tmp_path))
    assert report["real_pr_count"] == 19
    assert report["gate"]["met"] is False
    assert report["gate"]["threshold"] == GATE_THRESHOLD_FLOOR


def test_gate_met_at_20_real(tmp_path):
    write_real_prs(tmp_path, 20)
    report = _report(_ledger(tmp_path))
    assert report["real_pr_count"] == 20
    assert report["gate"]["met"] is True


def test_synthetic_prs_excluded_and_never_meet_gate(tmp_path):
    write_real_prs(tmp_path, 19)
    write_real_prs(tmp_path, 25, repo=SYNTH_REPO)
    synthetic = [{"repository": SYNTH_REPO, "pr_number": n, "reason": "seeded test PR"}
                 for n in range(1, 26)]
    synthetic.append({"repository": SYNTH_REPO, "pr_number": 999, "reason": "never ran"})
    report = _report(_ledger(tmp_path), synthetic)
    assert report["real_pr_count"] == 19
    assert report["gate"]["met"] is False
    assert len(report["synthetic_excluded"]) == 26
    assert {s["present_in_ledger"] for s in report["synthetic_excluded"] if s["pr_number"] == 999} == {False}
    assert all(p["repository"] == REPO for p in report["counted_prs"])


def test_synthetic_only_ledger_never_meets_gate(tmp_path):
    write_real_prs(tmp_path, 30, repo=SYNTH_REPO)
    synthetic = [{"repository": SYNTH_REPO, "pr_number": n, "reason": "seeded"} for n in range(1, 31)]
    report = _report(_ledger(tmp_path), synthetic)
    assert report["real_pr_count"] == 0
    assert report["gate"]["met"] is False


def test_committed_synthetic_list_is_valid_and_empty():
    path = Path(__file__).resolve().parents[2] / "docs" / "qualification" / "synthetic-prs.json"
    data = json.loads(path.read_text())
    assert local_validation_errors("synthetic-prs", data) == []
    assert data == []


def test_agreement_confusion_and_latency(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1", cls="yellow", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "r2", pr=2, head="h2", cls="red", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "r3", pr=3, head="h3", cls="green", escalated=False,
              created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "r4", pr=4, head="h4", cls="block",
              reasons=("github_api_budget_exceeded",))
    write_label(tmp_path, "l1", pr=1, head="h1", label="faraday/founder-yellow",
                labeled_at="2026-11-01T10:10:00Z")
    write_label(tmp_path, "l2", pr=2, head="h2", label="faraday/founder-green",
                labeled_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "l3", pr=3, head="h3", label="faraday/founder-red",
                labeled_at="2026-11-01T10:30:00Z")
    report = _report(_ledger(tmp_path))
    ag = report["agreement"]
    assert (ag["compared"], ag["agree"], ag["disagree"]) == (3, 1, 2)
    assert ag["direction"] == {"over_escalation": 1, "under_escalation": 1}
    assert ag["confusion_matrix"]["red"]["green"] == 1
    assert ag["confusion_matrix"]["green"]["red"] == 1
    assert ag["confusion_matrix"]["yellow"]["yellow"] == 1
    assert report["adjudicated_count"] == 3
    assert report["founder_latency"]["all_adjudicated"] == {
        "n": 3, "median_seconds": 1800.0, "p90_seconds": 3600.0}
    assert report["founder_latency"]["escalated_only"]["n"] == 2
    assert report["escalation"]["escalated_count"] == 3
    assert report["escalation"]["api_budget_exceeded_escalations"] == 1
    assert report["drift"]["infrastructure_failures"]["api_budget_exceeded_runs"] == 1


def test_policy_digest_drift_flagged(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1", digest=DIGEST_A)
    write_run(tmp_path, "r2", pr=2, head="h2", digest=DIGEST_B)
    report = _report(_ledger(tmp_path))
    assert report["drift"]["policy_digest_drift"] is True
    assert report["drift"]["policy_bundle_digests"] == [DIGEST_A, DIGEST_B]
    assert any(a["kind"] == "policy_digest_drift" for a in report["anomalies"])


def test_reviewer_version_drift_and_stale_counts(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1", reviewer_version="v0.1.0")
    write_run(tmp_path, "r2", pr=2, head="h2", reviewer_version="v0.2.0",
              stale_state="stale_before_publication")
    drift = _report(_ledger(tmp_path))["drift"]
    assert drift["reviewer_version_drift"] is True
    assert drift["stale_state_counts"] == {"fresh": 1, "stale_before_publication": 1}


def test_same_head_two_digests_correlate_separately(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1", digest=DIGEST_A, created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "r2", pr=1, head="h1", digest=DIGEST_B, created_at="2026-11-01T10:05:00Z")
    ledger = _ledger(tmp_path)
    assert [e["policy_bundle_digest"] for e in ledger["entries"]] == [DIGEST_A, DIGEST_B]
    assert {e["superseded"] for e in ledger["entries"]} == {False}
    assert _report(ledger)["real_pr_count"] == 1


def test_outputs_carry_no_authorization_fields(tmp_path):
    write_real_prs(tmp_path, 3)
    write_label(tmp_path, "l1", pr=1, head="h1")
    ledger = _ledger(tmp_path)
    blob = canonical_bytes(ledger) + canonical_bytes(_report(ledger))
    for field in ("approval_authorized", "merge_authorized", "founder_review_required",
                  "risk_class", '"policy_digest"', "policy_version", "contract_version"):
        assert field.encode() not in blob, field


def test_cli_end_to_end_is_canonical(tmp_path):
    write_real_prs(tmp_path / "in", 2)
    ledger, out, synth = tmp_path / "l.json", tmp_path / "r.json", tmp_path / "s.json"
    synth.write_text("[]\n")
    assert main(["aggregate", "--input-dir", str(tmp_path / "in"), "--output", str(ledger),
                 "--generated-at", TS]) == 0
    assert main(["readiness", "--ledger", str(ledger), "--synthetic", str(synth),
                 "--generated-at", TS, "--output", str(out)]) == 0
    report = read_canonical(out)
    assert out.read_bytes() == canonical_bytes(report)
    assert report["evidence"]["ledger"]["name"] == "l.json"
    assert report["evidence"]["synthetic"]["name"] == "s.json"
    assert len(report["evidence"]["ledger_inputs"]) == 4


def test_generated_at_required_and_validated(tmp_path):
    with pytest.raises(QualificationError):
        aggregate(tmp_path, generated_at="2026-11-12 00:00")
