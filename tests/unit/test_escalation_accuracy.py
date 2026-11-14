"""Founder ruling 2026-11-14, O1 + O2 (+gate), qualification-readiness/v4.

O1: agreement, escalation and founder latency are computed over the
adjudicated QUALIFYING PRs (the gate population), using the D10 scored
occurrence. The previous all-adjudicated / all-counted figures stay
visible under `context_all_adjudicated` / `context_all_real_prs` and never
feed the gate.

O2: escalation accuracy. Truth = the founder's adjudicated class requires
founder review under the same pinned policy; prediction = the scored
occurrence's escalation. The gate additionally requires zero missed
mandatory escalations, zero unknowns and a non-zero mandatory denominator.

Every scenario runs the real `aggregate` and `readiness`; nothing is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from qualification_support import DIGEST_A, DIGEST_B, write_label, write_run

from faraday_review.canonical import bundle_digest
from faraday_review.github.preflight import RELEASED_POLICY_DIGEST
from faraday_review.governance.escalation import (
    RELEASED_REVIEW_REQUIREMENTS,
    review_requirements_from_bundle,
)
from faraday_review.telemetry.qualification import (
    QualificationError,
    aggregate,
    local_validation_errors,
    readiness,
)

TS = "2026-11-12T00:00:00Z"
EV = {"name": "x.json", "sha256": "sha256:" + "0" * 64}
BUNDLE = Path(__file__).resolve().parents[1] / "fixtures" / "policy-bundle"
POLICY = {"green": False, "yellow": True, "red": True, "block": True}
REQ = {DIGEST_A: POLICY}


def _report(root: Path, *, requirements=REQ, released=None, threshold: int = 20) -> dict:
    ledger = aggregate(root, generated_at=TS)
    assert local_validation_errors("qualification-ledger", ledger) == []
    report = readiness(ledger, [], generated_at=TS, ledger_evidence=EV, synthetic_evidence=EV,
                       threshold=threshold, released_policy_digests=released,
                       review_requirements=requirements)
    assert local_validation_errors("qualification-readiness", report) == []
    return report


def _pr(root: Path, pr: int, *, cls: str = "yellow", escalated: bool | None = None,
        founder: str = "yellow", **kw) -> None:
    if escalated is None:
        escalated = POLICY[cls]
    write_run(root, f"run-{pr}", pr=pr, head=f"h{pr}", cls=cls, escalated=escalated,
              created_at=f"2026-11-01T10:{pr % 60:02d}:00Z", **kw)
    write_label(root, f"label-{pr}", pr=pr, head=f"h{pr}", label=f"faraday/founder-{founder}")


def _excluded_adjudicated(root: Path, pr: int, founder: str = "red") -> None:
    """Adjudicated, but the gate excludes it (misconfigured)."""
    write_run(root, f"run-{pr}", pr=pr, head=f"h{pr}", cls="block", escalated=True,
              reasons=("policy_digest_unpinned",), created_at=f"2026-11-01T10:{pr % 60:02d}:00Z")
    write_label(root, f"label-{pr}", pr=pr, head=f"h{pr}", label=f"faraday/founder-{founder}",
                labeled_at="2026-11-02T10:00:00Z")


# ------------------------------------------------------------------ O1


def test_o1_metrics_cover_only_the_gate_population(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr)
    _excluded_adjudicated(tmp_path, 21, founder="green")
    report = _report(tmp_path)
    assert report["adjudicated_qualifying_real_pr_count"] == 20
    assert report["agreement"]["compared"] == 20
    assert report["agreement"]["disagree"] == 0
    assert report["founder_latency"]["all_adjudicated"]["n"] == 20
    assert report["escalation"]["escalated_count"] == 20
    assert report["escalation"]["classifier_class_distribution"]["block"] == 0
    # Context: the pre-O1 figures, separately and never gating.
    ctx = report["context_all_adjudicated"]
    assert ctx["adjudicated_count"] == 21
    assert ctx["agreement"]["compared"] == 21
    assert ctx["agreement"]["disagree"] == 1
    assert ctx["founder_latency"]["all_adjudicated"]["n"] == 21
    assert report["context_all_real_prs"]["escalation"]["escalated_count"] == 21
    assert report["context_all_real_prs"]["escalation"]["classifier_class_distribution"]["block"] == 1


def test_o1_excluded_adjudicated_pr_never_makes_a_disagreement(tmp_path):
    _pr(tmp_path, 1)
    _excluded_adjudicated(tmp_path, 2, founder="green")
    report = _report(tmp_path)
    assert report["agreement"]["disagreements"] == []
    assert len(report["context_all_adjudicated"]["agreement"]["disagreements"]) == 1


def test_o1_uses_the_scored_occurrence_not_a_later_excluded_rerun(tmp_path):
    # Eligible green (not escalated), then an unpinned Block rerun of the
    # same head that represents the collapsed run (escalated).
    write_run(tmp_path, "pinned", pr=1, head="h1", cls="green", escalated=False,
              created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "unpinned", pr=1, head="h1", cls="block", escalated=True,
              reasons=("policy_digest_unpinned",), created_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "label", pr=1, head="h1", label="faraday/founder-red",
                labeled_at="2026-11-01T12:00:00Z")
    report = _report(tmp_path)
    assert report["escalation"]["escalated_count"] == 0
    assert report["escalation"]["classifier_class_distribution"]["green"] == 1
    acc = report["escalation_accuracy"]
    assert acc["matrix"]["missed_escalation"] == 1
    assert acc["missed_escalations"] == 1
    assert report["founder_latency"]["escalated_only"]["n"] == 0


# ------------------------------------------------------------------ O2


def test_o2_two_by_two_and_rates(tmp_path):
    _pr(tmp_path, 1, cls="yellow", founder="yellow")   # true escalation
    _pr(tmp_path, 2, cls="red", founder="block")       # true escalation
    _pr(tmp_path, 3, cls="yellow", founder="green")    # unnecessary
    _pr(tmp_path, 4, cls="green", founder="red")       # missed
    _pr(tmp_path, 5, cls="green", founder="green")     # true non-escalation
    _pr(tmp_path, 6, cls="green", founder="green")     # true non-escalation
    _pr(tmp_path, 7, cls="green", founder="green")     # true non-escalation
    acc = _report(tmp_path)["escalation_accuracy"]
    assert acc["population"] == "adjudicated_qualifying"
    assert acc["matrix"] == {"true_escalation": 2, "unnecessary_escalation": 1,
                             "missed_escalation": 1, "true_non_escalation": 3}
    assert acc["mandatory_escalations"] == 3
    assert acc["unknown"] == 0
    assert acc["escalation_recall"] == round(2 / 3, 6)
    assert acc["escalation_precision"] == round(2 / 3, 6)
    assert acc["unnecessary_escalation_rate"] == round(1 / 4, 6)
    assert acc["missed_escalations"] == 1
    assert [(p["pr_number"], p["classifier_class"], p["founder_classification"])
            for p in acc["missed_escalation_prs"]] == [(4, "green", "red")]


def test_o2_prediction_is_the_recorded_escalation_not_derived_from_class(tmp_path):
    # A yellow occurrence that (abnormally) did not escalate is a miss.
    _pr(tmp_path, 1, cls="yellow", escalated=False, founder="yellow")
    acc = _report(tmp_path)["escalation_accuracy"]
    assert acc["matrix"]["missed_escalation"] == 1
    assert acc["matrix"]["true_escalation"] == 0


def test_o2_truth_is_the_founder_class_not_the_classifier_class(tmp_path):
    _pr(tmp_path, 1, cls="green", founder="yellow")
    _pr(tmp_path, 2, cls="yellow", founder="green")
    acc = _report(tmp_path)["escalation_accuracy"]
    assert acc["matrix"] == {"true_escalation": 0, "unnecessary_escalation": 1,
                             "missed_escalation": 1, "true_non_escalation": 0}


def test_o2_rates_are_null_never_zero_when_denominator_is_zero(tmp_path):
    (tmp_path / "empty").mkdir()
    acc = _report(tmp_path / "empty")["escalation_accuracy"]
    assert acc["escalation_recall"] is None
    assert acc["escalation_precision"] is None
    assert acc["unnecessary_escalation_rate"] is None
    _pr(tmp_path / "g", 1, cls="green", founder="green")
    acc = _report(tmp_path / "g")["escalation_accuracy"]
    assert acc["escalation_recall"] is None          # no mandatory escalation
    assert acc["escalation_precision"] is None       # nothing escalated
    assert acc["unnecessary_escalation_rate"] == 0.0  # 0 of 1 non-mandatory


def test_o2_unknown_policy_mapping_is_unknown(tmp_path):
    _pr(tmp_path, 1)
    _pr(tmp_path, 2, digest=DIGEST_B)
    acc = _report(tmp_path)["escalation_accuracy"]
    assert acc["unknown"] == 1
    assert [p["pr_number"] for p in acc["unknown_prs"]] == [2]
    assert acc["matrix"]["true_escalation"] == 1


def test_o2_too_large_kept_separate(tmp_path):
    _pr(tmp_path, 1, cls="block", founder="block",
        reasons=("pr_too_large_for_deterministic_classification",))
    report = _report(tmp_path)
    assert report["escalation"]["pr_too_large_escalations"] == 1
    assert report["escalation"]["api_budget_exceeded_escalations"] == 0
    assert report["escalation_accuracy"]["matrix"]["true_escalation"] == 1


def test_o2_malformed_requirements_rejected(tmp_path):
    _pr(tmp_path, 1)
    with pytest.raises(QualificationError):
        _report(tmp_path, requirements={DIGEST_A: {"green": False}})
    with pytest.raises(QualificationError):
        _report(tmp_path, requirements={DIGEST_A: {**POLICY, "green": "no"}})


# ------------------------------------------------------------- O2 gate


def _gate(report: dict) -> dict:
    return report["gate"]


def test_gate_met_with_twenty_and_no_missed_escalation(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr, cls="green" if pr % 2 else "yellow",
            founder="green" if pr % 2 else "yellow")
    gate = _gate(_report(tmp_path))
    assert gate["met"] is True
    assert gate["escalation_condition"] == {"status": "pass", "missed": 0,
                                            "mandatory_total": 10, "unknown": 0}
    assert gate["criterion"] == ("adjudicated_qualifying_real_pr_count >= threshold "
                                 "AND escalation_condition.status == pass")


def test_gate_fails_on_a_single_missed_escalation(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr)
    _pr(tmp_path, 21, cls="green", founder="red")
    gate = _gate(_report(tmp_path))
    assert gate["met"] is False
    assert gate["escalation_condition"]["status"] == "fail"
    assert gate["escalation_condition"]["missed"] == 1
    assert gate["superseded_criterion"]["met"] is True


def test_gate_fails_on_zero_mandatory_denominator(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr, cls="green", founder="green")
    gate = _gate(_report(tmp_path))
    assert gate["met"] is False
    assert gate["escalation_condition"] == {"status": "unknown", "missed": 0,
                                            "mandatory_total": 0, "unknown": 0}


def test_gate_fails_on_any_unknown(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr)
    _pr(tmp_path, 21, digest=DIGEST_B)
    report = _report(tmp_path, released=None)
    assert report["adjudicated_qualifying_real_pr_count"] == 21
    gate = _gate(report)
    assert gate["met"] is False
    assert gate["escalation_condition"]["status"] == "unknown"
    assert gate["escalation_condition"]["unknown"] == 1


def test_gate_fails_without_any_policy_mapping(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr)
    gate = _gate(_report(tmp_path, requirements={}))
    assert gate["met"] is False
    assert gate["escalation_condition"]["unknown"] == 20


def test_gate_count_still_required(tmp_path):
    for pr in range(1, 20):
        _pr(tmp_path, pr)
    gate = _gate(_report(tmp_path))
    assert gate["escalation_condition"]["status"] == "pass"
    assert gate["met"] is False


def test_excluded_missed_escalation_does_not_affect_gate(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr)
    _excluded_adjudicated(tmp_path, 21, founder="red")  # excluded: not in O2 population
    assert _gate(_report(tmp_path))["met"] is True


# ------------------------------------------------ pinned policy mapping


def test_released_requirements_match_the_released_bundle():
    assert bundle_digest(BUNDLE) == RELEASED_POLICY_DIGEST
    assert set(RELEASED_REVIEW_REQUIREMENTS) == {RELEASED_POLICY_DIGEST}
    digest, mapping = review_requirements_from_bundle(BUNDLE)
    assert digest == RELEASED_POLICY_DIGEST
    assert mapping == RELEASED_REVIEW_REQUIREMENTS[RELEASED_POLICY_DIGEST] == POLICY


def test_unusable_bundle_gives_no_mapping(tmp_path):
    assert review_requirements_from_bundle(tmp_path / "missing") is None


def test_default_requirements_are_the_released_ones(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr, digest=RELEASED_POLICY_DIGEST)
    ledger = aggregate(tmp_path, generated_at=TS)
    report = readiness(ledger, [], generated_at=TS, ledger_evidence=EV, synthetic_evidence=EV,
                       released_policy_digests=[RELEASED_POLICY_DIGEST])
    assert report["escalation_accuracy"]["matrix"]["true_escalation"] == 20
    assert report["gate"]["met"] is True


def test_format_is_v4(tmp_path):
    assert _report(tmp_path)["format"] == "faraday-review/qualification-readiness/v4"


# ------------------------------------- Codex P1 (#11): absent prediction


def _strip_escalation(run_dir: Path) -> None:
    path = run_dir / "telemetry.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        row.pop("founder_review_required", None)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def test_absent_escalation_in_telemetry_is_unknown_not_false(tmp_path):
    for pr in range(1, 21):
        _pr(tmp_path, pr, cls="green", founder="green")
        _strip_escalation(tmp_path / f"run-{pr}")
    _pr(tmp_path, 21)  # one correctly escalated mandatory PR
    ledger = aggregate(tmp_path, generated_at=TS)
    occ = next(e for e in ledger["entries"] if e["pr_number"] == 1)["runs"][0]["occurrence_log"][0]
    assert "escalated" not in occ
    report = _report(tmp_path)
    acc = report["escalation_accuracy"]
    assert acc["unknown"] == 20
    assert {p["reason"] for p in acc["unknown_prs"]} == {"prediction_unknown"}
    assert report["gate"]["escalation_condition"]["status"] == "unknown"
    assert report["gate"]["met"] is False
