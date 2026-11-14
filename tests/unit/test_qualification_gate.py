"""Readiness gate semantics (qualification-readiness/v3).

The gate counts only real PRs whose current head has an *eligible* run
(completed, fresh, identity-eligible, policy digest verified, a known
classification, no configuration / refusal / infrastructure blocking
reason) **and** a valid founder adjudication on that head. Every other real
PR is reported under exactly one exclusion. `real_pr_count` keeps its v2
definition and stays visible.

Every scenario writes real artifact dirs and runs the real `aggregate` and
`readiness`. Nothing is mocked.
"""

from __future__ import annotations

import ast
import itertools
import re
import shutil
from pathlib import Path

import pytest
from qualification_support import DIGEST_B, REPO, write_label, write_real_prs, write_run

from faraday_review.canonical import canonical_bytes
from faraday_review.telemetry import qualification as q
from faraday_review.telemetry.qualification import (
    GATE_THRESHOLD_FLOOR,
    aggregate,
    local_validation_errors,
    readiness,
)

TS = "2026-11-12T00:00:00Z"
EVIDENCE = {"name": "x.json", "sha256": "sha256:" + "0" * 64}
SYNTH_REPO = "faraday-build/sandbox"
GOVERNANCE = Path(__file__).resolve().parents[2] / "src" / "faraday_review" / "governance"
EXCLUSION_KINDS = ("incomplete", "unknown", "misconfigured", "refused",
                   "infrastructure_blocked", "policy_digest_unverified", "unadjudicated")


def _report(root: Path, synthetic: list | None = None, threshold: int = 20) -> dict:
    ledger = aggregate(root, generated_at=TS)
    assert local_validation_errors("qualification-ledger", ledger) == []
    report = readiness(ledger, synthetic or [], generated_at=TS, ledger_evidence=EVIDENCE,
                       synthetic_evidence=EVIDENCE, threshold=threshold)
    assert local_validation_errors("qualification-readiness", report) == []
    return report


def _status(report: dict) -> dict[int, str]:
    return {p["pr_number"]: p["qualification"]["status"] for p in report["counted_prs"]}


def _adjudicated_prs(root: Path, prs, *, label: str = "faraday/founder-yellow",
                     **run_kwargs) -> None:
    for pr in prs:
        write_run(root, f"run-{pr}", pr=pr, head=f"h{pr}",
                  created_at=f"2026-11-01T10:{pr % 60:02d}:00Z", **run_kwargs)
        write_label(root, f"label-{pr}", pr=pr, head=f"h{pr}", label=label)


def _assert_partition(report: dict) -> None:
    """Every real PR in the ledger is the gate count or exactly one exclusion."""
    qual = report["qualification"]
    ex = qual["exclusions"]
    assert set(ex) == set(EXCLUSION_KINDS)
    assert report["adjudicated_qualifying_real_pr_count"] + sum(ex.values()) \
        == qual["real_prs_in_ledger"]
    assert report["qualifying_real_pr_count"] \
        == report["adjudicated_qualifying_real_pr_count"] + ex["unadjudicated"]
    assert report["real_pr_count"] == qual["real_prs_in_ledger"] - ex["incomplete"]
    assert report["adjudicated_qualifying_real_pr_count"] <= report["qualifying_real_pr_count"] \
        <= report["real_pr_count"]


# ------------------------------------------------------- the reported probe


def test_probe_20_block_unpinned_unadjudicated_prs_do_not_meet_gate(tmp_path):
    """Agent C probe: 20 real PRs, completed + fresh, Block for
    policy_digest_unpinned, zero adjudications. v2 reported met=true."""
    for pr in range(1, 21):
        write_run(tmp_path, f"run-{pr}", pr=pr, head=f"h{pr}", cls="block",
                  reasons=("policy_digest_unpinned",),
                  created_at=f"2026-11-01T10:{pr:02d}:00Z")
    report = _report(tmp_path)
    assert report["real_pr_count"] == 20  # unchanged definition, still visible
    assert report["qualifying_real_pr_count"] == 0
    assert report["adjudicated_qualifying_real_pr_count"] == 0
    assert report["qualification"]["exclusions"]["misconfigured"] == 20
    assert report["gate"]["met"] is False
    assert report["gate"]["superseded_criterion"] == {
        "criterion": "real_pr_count >= threshold", "met": True, "authoritative": False}
    _assert_partition(report)


def test_probe_adjudicated_but_unpinned_still_does_not_meet_gate(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 21), cls="block", reasons=("policy_digest_unpinned",),
                     label="faraday/founder-green")
    report = _report(tmp_path)
    assert report["adjudicated_count"] == 20
    assert report["adjudicated_qualifying_real_pr_count"] == 0
    assert report["gate"]["met"] is False


# ------------------------------------------------------- the threshold


def test_gate_met_at_20_adjudicated_qualifying(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 21))
    report = _report(tmp_path)
    assert report["real_pr_count"] == 20
    assert report["qualifying_real_pr_count"] == 20
    assert report["adjudicated_qualifying_real_pr_count"] == 20
    assert report["gate"] == {
        "threshold": 20,
        "criterion": "adjudicated_qualifying_real_pr_count >= threshold",
        "criterion_status": "pending_founder_confirmation",
        "met": True,
        "superseded_criterion": {"criterion": "real_pr_count >= threshold", "met": True,
                                 "authoritative": False},
    }
    assert set(_status(report).values()) == {"adjudicated_qualifying"}
    _assert_partition(report)


def test_gate_not_met_at_19_adjudicated_qualifying(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 20))
    report = _report(tmp_path)
    assert report["adjudicated_qualifying_real_pr_count"] == 19
    assert report["gate"]["met"] is False


def test_19_adjudicated_plus_unadjudicated_qualifying_not_met(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 20))
    write_real_prs(tmp_path, 5, start=20)
    report = _report(tmp_path)
    assert report["real_pr_count"] == 24
    assert report["qualifying_real_pr_count"] == 24
    assert report["adjudicated_qualifying_real_pr_count"] == 19
    assert report["qualification"]["exclusions"]["unadjudicated"] == 5
    assert report["gate"]["met"] is False
    assert report["gate"]["superseded_criterion"]["met"] is True
    _assert_partition(report)


def test_raised_threshold_applies_to_the_new_count(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 26))
    assert _report(tmp_path, threshold=25)["gate"]["met"] is True
    assert _report(tmp_path, threshold=26)["gate"]["met"] is False


def test_floor_still_20(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 21))
    ledger = aggregate(tmp_path, generated_at=TS)
    for bad in (19, 0, -1, True, 20.0, "20"):
        with pytest.raises(q.QualificationError):
            readiness(ledger, [], generated_at=TS, ledger_evidence=EVIDENCE,
                      synthetic_evidence=EVIDENCE, threshold=bad)
    assert q.GATE_THRESHOLD_FLOOR == 20


def test_synthetic_prs_still_excluded(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 20))
    for pr in range(1, 11):
        write_run(tmp_path, f"s-run-{pr}", repo=SYNTH_REPO, pr=pr, head=f"s{pr}")
        write_label(tmp_path, f"s-label-{pr}", repo=SYNTH_REPO, pr=pr, head=f"s{pr}")
    synthetic = [{"repository": SYNTH_REPO.upper(), "pr_number": n, "reason": "seeded"}
                 for n in range(1, 11)]
    report = _report(tmp_path, synthetic)
    assert report["adjudicated_qualifying_real_pr_count"] == 19
    assert report["qualification"]["real_prs_in_ledger"] == 19
    assert report["gate"]["met"] is False
    assert all(p["repository"] == REPO for p in report["counted_prs"])
    _assert_partition(report)


# ------------------------------------------------------- exclusions


@pytest.mark.parametrize(("reasons", "kind"), [
    (("policy_digest_unpinned",), "misconfigured"),
    (("policy_digest_mismatch",), "misconfigured"),
    (("policy_missing",), "misconfigured"),
    (("policy_version_unsupported",), "misconfigured"),
    (("contract_version_unsupported",), "misconfigured"),
    (("evidence_malformed",), "refused"),
    (("evidence_stale",), "refused"),
    (("unknown_evidence_class",), "refused"),
    (("github_api_budget_exceeded",), "infrastructure_blocked"),
    (("a_reason_this_consumer_does_not_know",), "unknown"),
])
def test_each_blocking_reason_category_excludes(tmp_path, reasons, kind):
    _adjudicated_prs(tmp_path, [1], cls="block", reasons=reasons)
    report = _report(tmp_path)
    assert report["real_pr_count"] == 1
    assert report["qualifying_real_pr_count"] == 0
    assert report["qualification"]["exclusions"][kind] == 1
    assert _status(report) == {1: kind}
    assert report["counted_prs"][0]["qualification"]["exclusion_reasons"] == [kind]
    _assert_partition(report)


@pytest.mark.parametrize("reasons", [
    ("secret_detected",), ("required_check_failing",), ("required_check_missing",),
    ("pr_too_large_for_deterministic_classification",),
    ("required_check_failing", "secret_detected"),
])
def test_classification_blocks_qualify(tmp_path, reasons):
    """A Block the Validator reached by classifying the change is a real
    classification the founder can adjudicate; it qualifies."""
    _adjudicated_prs(tmp_path, [1], cls="block", reasons=reasons)
    report = _report(tmp_path)
    assert report["adjudicated_qualifying_real_pr_count"] == 1
    assert _status(report) == {1: "adjudicated_qualifying"}


def test_configuration_reason_wins_over_a_classification_reason(tmp_path):
    _adjudicated_prs(tmp_path, [1], cls="block",
                     reasons=("policy_digest_mismatch", "secret_detected"))
    report = _report(tmp_path)
    assert report["qualifying_real_pr_count"] == 0
    assert _status(report) == {1: "misconfigured"}


def test_unverified_digest_excludes(tmp_path):
    _adjudicated_prs(tmp_path, [1], with_decision=False)
    report = _report(tmp_path)
    assert report["qualifying_real_pr_count"] == 0
    assert _status(report) == {1: "policy_digest_unverified"}


def test_missing_class_is_unknown_never_counted(tmp_path):
    _adjudicated_prs(tmp_path, [1], cls=None)
    report = _report(tmp_path)
    assert report["counted_prs"][0]["classifier_class"] is None
    assert _status(report) == {1: "unknown"}
    assert report["qualifying_real_pr_count"] == 0


def test_blocking_reason_on_non_block_class_is_unknown(tmp_path):
    _adjudicated_prs(tmp_path, [1], cls="green", reasons=("secret_detected",))
    report = _report(tmp_path)
    assert _status(report) == {1: "unknown"}
    assert report["qualifying_real_pr_count"] == 0


def test_precedence_reports_one_status_and_every_reason(tmp_path):
    _adjudicated_prs(tmp_path, [1], cls="block", with_decision=False,
                     reasons=("evidence_malformed", "github_api_budget_exceeded",
                              "policy_digest_unpinned", "zz_unknown"))
    row = _report(tmp_path)["counted_prs"][0]["qualification"]
    assert row["status"] == "unknown"
    assert row["exclusion_reasons"] == ["infrastructure_blocked", "misconfigured",
                                        "policy_digest_unverified", "refused", "unknown"]


def test_failed_only_and_stale_only_prs_are_incomplete(tmp_path):
    _adjudicated_prs(tmp_path, [1])
    write_run(tmp_path, "failed-2", pr=2, head="h2", final_status="failed",
              failure_mode="runner_lost")
    write_label(tmp_path, "label-2", pr=2, head="h2")
    write_run(tmp_path, "stale-3", pr=3, head="h3", stale_state="stale_before_publication")
    write_label(tmp_path, "label-3", pr=3, head="h3")
    report = _report(tmp_path)
    assert report["real_pr_count"] == 1
    qual = report["qualification"]
    assert qual["exclusions"]["incomplete"] == 2
    assert qual["incomplete_prs"] == [{"repository": REPO, "pr_number": 2},
                                      {"repository": REPO, "pr_number": 3}]
    assert report["adjudicated_qualifying_real_pr_count"] == 1
    _assert_partition(report)


def test_mixed_exclusions_exact_partition(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 13))                                  # 12 counted
    write_real_prs(tmp_path, 3, start=13)                                     # 3 unadjudicated
    _adjudicated_prs(tmp_path, [16, 17], cls="block",
                     reasons=("policy_digest_unpinned",))                     # 2 misconfigured
    _adjudicated_prs(tmp_path, [18], cls="block", reasons=("contract_version_unsupported",))
    _adjudicated_prs(tmp_path, [19], cls="block", reasons=("evidence_malformed",))  # refused
    _adjudicated_prs(tmp_path, [20], cls="block", reasons=("github_api_budget_exceeded",))
    _adjudicated_prs(tmp_path, [21], with_decision=False)                     # unverified
    _adjudicated_prs(tmp_path, [22], cls=None)                                # unknown
    write_run(tmp_path, "failed-23", pr=23, head="h23", final_status="failed",
              failure_mode="runner_lost")                                     # incomplete
    _adjudicated_prs(tmp_path, [24], cls="block", reasons=("secret_detected",))  # counted
    report = _report(tmp_path)
    assert report["qualification"]["exclusions"] == {
        "incomplete": 1, "unknown": 1, "misconfigured": 3, "refused": 1,
        "infrastructure_blocked": 1, "policy_digest_unverified": 1, "unadjudicated": 3}
    assert report["real_pr_count"] == 23
    assert report["qualifying_real_pr_count"] == 16
    assert report["adjudicated_qualifying_real_pr_count"] == 13
    assert report["gate"]["met"] is False
    assert report["gate"]["superseded_criterion"]["met"] is True
    _assert_partition(report)


# ------------------------------------------------------- adjudication


def test_adjudication_on_a_non_eligible_run_never_counts(tmp_path):
    _adjudicated_prs(tmp_path, range(1, 20))
    _adjudicated_prs(tmp_path, [20], cls="block", reasons=("policy_digest_mismatch",))
    report = _report(tmp_path)
    assert report["adjudicated_count"] == 20
    assert report["adjudicated_qualifying_real_pr_count"] == 19
    assert report["gate"]["met"] is False
    assert {"kind": "adjudicated_pr_not_qualifying", "repository": REPO, "pr_number": 20,
            "status": "misconfigured"} in report["anomalies"]


def test_adjudication_on_superseded_head_does_not_qualify_current_head(tmp_path):
    write_run(tmp_path, "old", pr=1, head="h1", created_at="2026-11-01T10:00:00Z")
    write_label(tmp_path, "label-old", pr=1, head="h1", labeled_at="2026-11-01T10:30:00Z")
    write_run(tmp_path, "new", pr=1, head="h1b", created_at="2026-11-01T11:00:00Z")
    report = _report(tmp_path)
    assert report["qualifying_real_pr_count"] == 1
    assert report["adjudicated_qualifying_real_pr_count"] == 0
    assert _status(report) == {1: "unadjudicated"}


def test_non_founder_adjudication_is_not_valid(tmp_path):
    write_run(tmp_path, "r1", pr=1, head="h1")
    write_label(tmp_path, "label-x", pr=1, head="h1", node_id="U_other", login="tomrivera-PSE")
    report = _report(tmp_path)
    assert report["adjudicated_qualifying_real_pr_count"] == 0
    assert _status(report) == {1: "unadjudicated"}


def test_eligible_occurrence_qualifies_despite_misconfigured_rerun_same_head(tmp_path):
    """Same digest, same fingerprint: one collapsed run whose representative
    is the newer unpinned rerun. The earlier pinned occurrence is a real
    classification of this exact head, so the PR qualifies."""
    write_run(tmp_path, "pinned", pr=1, head="h1", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "unpinned", pr=1, head="h1", cls="block",
              reasons=("policy_digest_unpinned",), created_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "label", pr=1, head="h1", labeled_at="2026-11-01T12:00:00Z")
    report = _report(tmp_path)
    assert report["counted_prs"][0]["classifier_class"] == "block"  # v2 representative
    assert _status(report) == {1: "adjudicated_qualifying"}


def test_eligible_run_under_other_digest_on_same_head_qualifies(tmp_path):
    write_run(tmp_path, "a", pr=1, head="h1", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "b", pr=1, head="h1", digest=DIGEST_B, cls="block",
              reasons=("policy_digest_mismatch",), created_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "label", pr=1, head="h1", labeled_at="2026-11-01T12:00:00Z")
    assert _status(_report(tmp_path)) == {1: "adjudicated_qualifying"}


def test_failed_and_stale_occurrences_never_qualify_a_misconfigured_run(tmp_path):
    write_run(tmp_path, "failed", pr=1, head="h1", final_status="failed",
              failure_mode="runner_lost", created_at="2026-11-01T09:00:00Z")
    write_run(tmp_path, "stale", pr=1, head="h1", stale_state="stale_before_validation",
              created_at="2026-11-01T09:30:00Z")
    write_run(tmp_path, "unpinned", pr=1, head="h1", cls="block",
              reasons=("policy_digest_unpinned",), created_at="2026-11-01T10:00:00Z")
    write_label(tmp_path, "label", pr=1, head="h1", labeled_at="2026-11-01T12:00:00Z")
    assert _status(_report(tmp_path)) == {1: "misconfigured"}


def test_conflicted_event_never_qualifies(tmp_path):
    eid = "4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f6aa"
    write_run(tmp_path, "unpinned", pr=1, head="h1", cls="block",
              reasons=("policy_digest_unpinned",), created_at="2026-11-01T11:00:00Z")
    write_run(tmp_path, "c1", pr=1, head="h1", event_id=eid, created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "c2", pr=1, head="h1", event_id=eid, created_at="2026-11-01T10:00:00Z",
              duration_seconds=9.0)
    write_label(tmp_path, "label", pr=1, head="h1", labeled_at="2026-11-01T12:00:00Z")
    report = _report(tmp_path)
    assert any(a["kind"] == "event_id_conflict" for a in report["anomalies"])
    assert _status(report) == {1: "misconfigured"}


# ------------------------------------------------------- determinism


def _mixed_tree(root: Path, order: tuple[int, ...]) -> None:
    builders = [
        lambda: _adjudicated_prs(root, range(1, 6)),
        lambda: write_real_prs(root, 2, start=6),
        lambda: _adjudicated_prs(root, [8], cls="block", reasons=("policy_digest_unpinned",)),
        lambda: _adjudicated_prs(root, [9], with_decision=False),
        lambda: write_run(root, "failed-10", pr=10, head="h10", final_status="failed",
                          failure_mode="runner_lost"),
    ]
    for i in order:
        builders[i]()


def test_report_independent_of_input_order(tmp_path):
    outputs = set()
    for n, order in enumerate(itertools.islice(itertools.permutations(range(5)), 0, 120, 23)):
        root = tmp_path / f"o{n}"
        _mixed_tree(root, order)
        outputs.add(canonical_bytes(_report(root)))
    assert len(outputs) == 1


def test_report_independent_of_archive_location(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _mixed_tree(a, (0, 1, 2, 3, 4))
    for child in sorted(a.iterdir()):
        shutil.copytree(child, b / "nested" / f"z-{child.name}")
    ra, rb = _report(a), _report(b)
    for report in (ra, rb):
        report.pop("evidence")
        report.pop("anomalies")  # carry archive-relative source paths
    assert canonical_bytes(ra) == canonical_bytes(rb)


# ------------------------------------------------------- reason table


def _validator_reason_literals() -> set[str]:
    """Every reason string the Validator can emit, read from its source."""
    found: set[str] = set()
    for path in sorted(GOVERNANCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "append" \
                    and getattr(node.func.value, "id", None) == "reasons" and node.args \
                    and isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                for elt in node.value.elts:
                    if isinstance(elt, ast.List):
                        found.update(e.value for e in elt.elts if isinstance(e, ast.Constant)
                                     and isinstance(e.value, str))
            if isinstance(node, ast.Return) and isinstance(node.value, ast.List):
                found.update(e.value for e in node.value.elts if isinstance(e, ast.Constant)
                             and isinstance(e.value, str))
    from faraday_review.governance import authorization
    found.update(authorization._REASON_BY_RULE.values())
    found.update(authorization._REASON_BY_CHECK.values())
    found.update(authorization.INFRASTRUCTURE_REASONS)
    assigned = re.findall(r'blocking_reasons = \["([a-z_]+)"\]',
                          (GOVERNANCE / "authorization.py").read_text(encoding="utf-8"))
    found.update(assigned)
    return {r for r in found if re.fullmatch(r"[a-z][a-z0-9_]*", r)}


def test_every_validator_reason_is_classified():
    """A reason the Validator gains must be classified here deliberately;
    until then it is `unknown` at run time and never counts."""
    validator = _validator_reason_literals()
    assert validator >= {"policy_digest_unpinned", "policy_digest_mismatch",
                         "contract_version_unsupported", "github_api_budget_exceeded",
                         "secret_detected", "evidence_malformed", "policy_missing"}
    assert validator == set(q.BLOCKING_REASON_CATEGORIES)


def test_reason_table_categories_and_documentation(tmp_path):
    _adjudicated_prs(tmp_path, [1])
    qual = _report(tmp_path)["qualification"]
    assert qual["exclusion_precedence"] == ["unknown", "misconfigured", "refused",
                                            "infrastructure_blocked", "policy_digest_unverified"]
    assert set(q.BLOCKING_REASON_CATEGORIES.values()) == {
        "classification", "misconfigured", "refused", "infrastructure_blocked"}
    doc = (Path(__file__).resolve().parents[2] / "docs" / "PHASE_2_INPUTS.md").read_text()
    for reason, category in q.BLOCKING_REASON_CATEGORIES.items():
        assert f"| `{reason}` | {category} |" in doc, reason


def test_report_format_is_v3(tmp_path):
    _adjudicated_prs(tmp_path, [1])
    assert _report(tmp_path)["format"] == "faraday-review/qualification-readiness/v3"
    assert GATE_THRESHOLD_FLOOR == 20


def test_adjudication_must_sit_on_the_eligible_entry_itself(tmp_path):
    """`readiness` reads a committed ledger, so it must not borrow an
    adjudication from a sibling entry (another digest) of the same head.
    `aggregate` attaches per head, so this ledger is edited to differ."""
    write_run(tmp_path, "a", pr=1, head="h1", created_at="2026-11-01T10:00:00Z")
    write_run(tmp_path, "b", pr=1, head="h1", digest=DIGEST_B, cls="block",
              reasons=("policy_digest_mismatch",), created_at="2026-11-01T11:00:00Z")
    write_label(tmp_path, "label", pr=1, head="h1", labeled_at="2026-11-01T12:00:00Z")
    ledger = aggregate(tmp_path, generated_at=TS)
    eligible_entry = next(e for e in ledger["entries"] if e["policy_bundle_digest"] != DIGEST_B)
    eligible_entry["founder_adjudication"] = None
    assert local_validation_errors("qualification-ledger", ledger) == []
    report = readiness(ledger, [], generated_at=TS, ledger_evidence=EVIDENCE,
                       synthetic_evidence=EVIDENCE)
    assert _status(report) == {1: "unadjudicated"}
    assert report["adjudicated_qualifying_real_pr_count"] == 0
