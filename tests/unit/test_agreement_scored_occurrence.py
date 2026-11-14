"""Agreement scores the classification the gate qualified, not a later
misconfigured / refused rerun that happens to represent the collapsed run
(GHO-005 A2 gap G2)."""

from __future__ import annotations

from qualification_support import DIGEST_B, write_label, write_run

from faraday_review.telemetry.qualification import aggregate, local_validation_errors, readiness

TS = "2026-11-12T00:00:00Z"
EV = {"name": "x.json", "sha256": "sha256:" + "0" * 64}


def _report(root):
    report = readiness(aggregate(root, generated_at=TS), [], generated_at=TS,
                       ledger_evidence=EV, synthetic_evidence=EV)
    assert local_validation_errors("qualification-readiness", report) == []
    return report


def test_unpinned_rerun_does_not_create_a_false_disagreement(tmp_path):
    write_run(tmp_path, "pinned", pr=1, head="h1", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "unpinned", pr=1, head="h1", cls="block",
              reasons=("policy_digest_unpinned",), created_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "label", pr=1, head="h1", labeled_at="2026-11-01T12:00:00Z")
    report = _report(tmp_path)
    assert report["counted_prs"][0]["qualification"]["status"] == "adjudicated_qualifying"
    assert report["counted_prs"][0]["classifier_class"] == "block"  # v2 surface unchanged
    assert report["agreement"]["agree"] == 1 and report["agreement"]["disagree"] == 0
    assert report["agreement"]["confusion_matrix"]["yellow"]["yellow"] == 1
    assert report["agreement"]["confusion_matrix"]["block"]["yellow"] == 0


def test_mismatched_digest_rerun_on_sibling_entry_is_not_scored(tmp_path):
    write_run(tmp_path, "a", pr=1, head="h1", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "b", pr=1, head="h1", digest=DIGEST_B, cls="block",
              reasons=("policy_digest_mismatch",), created_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "label", pr=1, head="h1", labeled_at="2026-11-01T12:00:00Z")
    report = _report(tmp_path)
    assert report["agreement"]["disagreements"] == []
    assert report["agreement"]["agree"] == 1


def test_genuine_disagreement_on_the_qualifying_occurrence_still_reported(tmp_path):
    write_run(tmp_path, "r", pr=1, head="h1", cls="red")
    write_label(tmp_path, "label", pr=1, head="h1", label="faraday/founder-green")
    report = _report(tmp_path)
    assert report["agreement"]["disagree"] == 1
    assert report["agreement"]["disagreements"][0]["classifier_class"] == "red"
