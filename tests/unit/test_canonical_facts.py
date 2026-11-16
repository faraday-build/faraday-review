"""GHO-PRODUCTION-1 single-source-of-truth correction: v4 readiness emits the
canonical per-PR facts (additive). These tests drive the REAL aggregate ->
readiness pipeline from telemetry runs."""

from __future__ import annotations

from qualification_support import DIGEST_A, DIGEST_B, REPO, sha, write_run

from faraday_review.telemetry.qualification import (
    aggregate,
    canonical_pr_facts,
    local_validation_errors,
    readiness,
)

TS = "2026-11-12T00:00:00Z"
EVIDENCE = {"name": "x.json", "sha256": "sha256:" + "0" * 64}


def _report(root, released=None):
    ledger = aggregate(root, generated_at=TS)
    assert local_validation_errors("qualification-ledger", ledger) == []
    report = readiness(ledger, [], generated_at=TS, ledger_evidence=EVIDENCE, synthetic_evidence=EVIDENCE,
                       released_policy_digests=released)
    assert local_validation_errors("qualification-readiness", report) == []
    return ledger, report


def _facts(report, pr=1):
    return next(c["facts"] for c in report["counted_prs"] if c["pr_number"] == pr)


def test_readiness_emits_schema_valid_facts_without_changing_v4_fields(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1", created_at="2026-11-01T10:00:00Z", cls="red")
    _, report = _report(tmp_path)
    (c,) = report["counted_prs"]
    assert set(c) == {"repository", "pr_number", "head_sha", "review_id", "classifier_class", "adjudicated",
                      "qualification", "facts"}
    assert c["facts"] == {
        "facts_version": 1,
        "first_countable": {"created_at": "2026-11-01T10:00:00Z", "head_sha": sha("h1"), "classifier_class": "red"},
        "latest_qualifying": {"created_at": "2026-11-01T10:00:00Z", "event_key": c["facts"]["latest_qualifying"]["event_key"],
                              "classifier_class": "red", "escalated": True},
        "head_transitions": [["2026-11-01T10:00:00Z", sha("h1")]],
    }


def test_stale_completion_is_not_when_the_pr_became_countable(tmp_path):
    """The reported P2 (Codex 4581842398): an early completed STALE occurrence
    must not backdate arrival; the first countable occurrence is the fresh one."""
    write_run(tmp_path, "r1-stale", pr=1, head="h1", created_at="2026-11-01T08:00:00Z",
              stale_state="stale_before_publication")
    write_run(tmp_path, "r2-fresh", pr=1, head="h1", created_at="2026-11-03T08:00:00Z")
    _, report = _report(tmp_path)
    assert _facts(report)["first_countable"]["created_at"] == "2026-11-03T08:00:00Z"


def test_failed_run_is_not_when_the_pr_became_countable(tmp_path):
    write_run(tmp_path, "r1-failed", pr=1, head="h1", created_at="2026-11-01T08:00:00Z",
              final_status="failed", failure_mode="runner_lost")
    write_run(tmp_path, "r2-ok", pr=1, head="h1", created_at="2026-11-03T08:00:00Z")
    _, report = _report(tmp_path)
    assert _facts(report)["first_countable"]["created_at"] == "2026-11-03T08:00:00Z"


def test_stale_rerun_is_never_a_head_transition(tmp_path):
    write_run(tmp_path, "a1", pr=1, head="hA", created_at="2026-11-01T08:00:00Z")
    write_run(tmp_path, "b1", pr=1, head="hB", created_at="2026-11-02T08:00:00Z")
    write_run(tmp_path, "a-rerun", pr=1, head="hA", created_at="2026-11-03T08:00:00Z",
              stale_state="stale_before_publication")
    _, report = _report(tmp_path)
    assert _facts(report)["head_transitions"] == [["2026-11-01T08:00:00Z", sha("hA")],
                                                  ["2026-11-02T08:00:00Z", sha("hB")]]


def test_head_that_becomes_current_again_appears_twice(tmp_path):
    write_run(tmp_path, "a1", pr=1, head="hA", created_at="2026-11-01T08:00:00Z")
    write_run(tmp_path, "b1", pr=1, head="hB", created_at="2026-11-02T08:00:00Z")
    write_run(tmp_path, "a2", pr=1, head="hA", created_at="2026-11-03T08:00:00Z")
    ledger, report = _report(tmp_path)
    tr = _facts(report)["head_transitions"]
    assert [h for _, h in tr] == [sha("hA"), sha("hB"), sha("hA")]


def test_latest_qualifying_excludes_unreleased_digests_and_uses_chronology(tmp_path):
    """Latest qualifying = latest countable, non-excluded occurrence on the
    current head; an unreleased-digest run never supplies class/escalation."""
    write_run(tmp_path, "r1", pr=1, head="h1", created_at="2026-11-01T08:00:00Z", cls="red", digest=DIGEST_A)
    write_run(tmp_path, "r2", pr=1, head="h1", created_at="2026-11-02T08:00:00Z", cls="green",
              escalated=False, digest=DIGEST_B)
    _, report = _report(tmp_path, released=[DIGEST_A])
    lq = _facts(report)["latest_qualifying"]
    assert lq["classifier_class"] == "red" and lq["escalated"] is True


def test_facts_are_deterministic_and_order_independent():
    occ = {"eligible": True, "final_status": "completed", "stale_state": "fresh", "classifier_class": "red",
           "escalated": True, "blocking_reasons": []}
    run = lambda *occs: {"occurrence_log": list(occs), "policy_digest_verified": True,  # noqa: E731
                         "policy_bundle_digest": DIGEST_A}
    a = {"head_sha": sha("a"), "current": False,
         "runs": [run(dict(occ, created_at="2026-11-01T10:00:00+02:00", event_key="x"))]}
    b = {"head_sha": sha("b"), "current": True,
         "runs": [run(dict(occ, created_at="2026-11-01T09:30:00Z", event_key="y"))]}
    f1 = canonical_pr_facts([a, b], [(b, b["runs"][0])], [DIGEST_A])
    f2 = canonical_pr_facts([b, a], [(b, b["runs"][0])], [DIGEST_A])
    assert f1 == f2
    # 10:00+02:00 (08:00Z) precedes 09:30Z chronologically.
    assert f1["first_countable"]["head_sha"] == sha("a")
    assert [h for _, h in f1["head_transitions"]] == [sha("a"), sha("b")]


def test_consecutive_runs_on_the_same_head_are_one_transition(tmp_path):
    write_run(tmp_path, "a1", pr=1, head="hA", created_at="2026-11-01T08:00:00Z")
    write_run(tmp_path, "a2", pr=1, head="hA", created_at="2026-11-01T09:00:00Z")
    write_run(tmp_path, "b1", pr=1, head="hB", created_at="2026-11-02T08:00:00Z")
    _, report = _report(tmp_path)
    assert _facts(report)["head_transitions"] == [["2026-11-01T08:00:00Z", sha("hA")],
                                                  ["2026-11-02T08:00:00Z", sha("hB")]]
