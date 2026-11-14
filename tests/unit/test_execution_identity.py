"""Execution identity in the qualification consumer (agent-contracts v0.4.0,
review-telemetry-identity.md C1-C9). Test ids follow the r7 matrix.

Every scenario writes real artifact dirs and runs the real `aggregate` and
`readiness`. Nothing here is mocked.
"""

from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path

import pytest
from identity_scenarios import (
    BASELINE_SYNTHETIC,
    V03,
    build_v1_prior_tree,
    build_v030_baseline,
    gate_view,
)
from qualification_support import REPO, write_run

from faraday_review import REVIEWER_VERSION
from faraday_review.canonical import canonical_bytes
from faraday_review.telemetry.qualification import (
    QualificationError,
    aggregate,
    local_validation_errors,
    readiness,
)

TS = "2026-11-12T00:00:00Z"
EVIDENCE = {"name": "x.json", "sha256": "sha256:" + "0" * 64}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "qualification"
E1 = "4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f601"
E2 = "4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f602"
E3 = "4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f603"
SAME_SECOND = "2026-11-01T10:00:00Z"


def gha(run_id: int, attempt: int = 1) -> dict:
    return {"provider": "github_actions", "run_id": run_id, "run_attempt": attempt}


def _ledger(root: Path) -> dict:
    ledger = aggregate(root, generated_at=TS)
    assert local_validation_errors("qualification-ledger", ledger) == []
    return ledger


def _report(ledger: dict, synthetic: list | None = None, threshold: int = 20) -> dict:
    report = readiness(ledger, synthetic or [], generated_at=TS, ledger_evidence=EVIDENCE,
                       synthetic_evidence=EVIDENCE, threshold=threshold)
    assert local_validation_errors("qualification-readiness", report) == []
    return report


def _runs(ledger: dict) -> list[dict]:
    return [r for e in ledger["entries"] for r in e["runs"]]


def _only_run(ledger: dict) -> dict:
    runs = _runs(ledger)
    assert len(runs) == 1, runs
    return runs[0]


def _kinds(doc: dict) -> list[str]:
    return sorted({a["kind"] for a in doc["anomalies"]})


def _infra(report: dict) -> dict:
    return report["drift"]["infrastructure_failures"]


# ------------------------------------------------------- T01 / T02 (C2)


def test_t01_same_second_differing_only_in_duration_are_two_executions(tmp_path):
    write_run(tmp_path, "a", created_at=SAME_SECOND, duration_seconds=1.5,
              event_id=E1, execution=gha(101))
    write_run(tmp_path, "b", created_at=SAME_SECOND, duration_seconds=2.5,
              event_id=E2, execution=gha(102))
    run = _only_run(_ledger(tmp_path))
    assert run["distinct_payloads"] == 2
    assert run["executions_exact"] == 2
    assert run["execution_count"] == 2
    assert run["execution_count_bounds"] == {"min": 2, "max": 2}


@pytest.mark.parametrize("field,a,b", [
    ("duration_seconds", 1.5, 2.5),
    ("github_requests_used", 7, 8),
    ("retries", 0, 1),
    ("finding_counts", {"info": 0, "warning": 0, "blocking": 0},
     {"info": 1, "warning": 0, "blocking": 0}),
])
def test_t02_legacy_key_is_the_full_record_not_a_projection(tmp_path, field, a, b):
    """The Codex P2 at 8088813:229. v0.3.0 records that differ only in a
    field the old projection dropped are two events, not one."""
    write_run(tmp_path, "a", created_at=SAME_SECOND, contract_version=V03, **{field: a})
    write_run(tmp_path, "b", created_at=SAME_SECOND, contract_version=V03, **{field: b})
    run = _only_run(_ledger(tmp_path))
    assert run["distinct_payloads"] == 2
    assert {o["event_key_kind"] for o in run["occurrence_log"]} == {"legacy_content"}
    assert run["execution_count"] is None
    assert run["execution_count_bounds"] == {"min": 2, "max": 2}


# ------------------------------------------------------------ T03 (C8)


def test_t03_byte_identical_legacy_copies_are_bounded_not_synthesized(tmp_path):
    write_run(tmp_path, "a", contract_version=V03)
    write_run(tmp_path, "b", contract_version=V03)
    ledger = _ledger(tmp_path)
    run = _only_run(ledger)
    assert run["distinct_payloads"] == 1
    assert run["occurrence_log"][0]["copies"] == 2
    assert run["occurrence_log"][0]["execution"] is None
    assert run["occurrence_log"][0]["execution_key"] is None
    assert run["executions_exact"] == 0
    assert run["execution_count"] is None
    assert run["execution_count_bounds"] == {"min": 1, "max": 2}
    assert "legacy_execution_identity_unknown" in _kinds(ledger)


# ------------------------------------------------------------ T04 (C7)


def test_t04_two_events_one_execution_counts_once(tmp_path):
    write_run(tmp_path, "a", event_id=E1, execution=gha(101))
    write_run(tmp_path, "b", event_id=E2, execution=gha(101))
    ledger = _ledger(tmp_path)
    run = _only_run(ledger)
    assert run["distinct_payloads"] == 2
    assert run["executions_exact"] == 1
    assert run["execution_count"] == 1
    assert "multiple_events_per_execution" in _kinds(ledger)
    assert ledger["counts"]["identity"]["executions_exact"] == 1


# ------------------------------------------------------------ T05 (C6)


def test_t05_rerun_attempts_are_distinct_and_only_the_completed_one_selects(tmp_path):
    write_run(tmp_path, "a1", created_at="2026-11-01T10:00:00Z", final_status="failed",
              failure_mode="runner_lost", event_id=E1, execution=gha(101, 1))
    write_run(tmp_path, "a2", created_at="2026-11-01T10:05:00Z", event_id=E2,
              execution=gha(101, 2))
    ledger = _ledger(tmp_path)
    run = _only_run(ledger)
    assert run["executions_exact"] == 2
    assert run["last_eligible_created_at"] == "2026-11-01T10:05:00Z"
    report = _report(ledger)
    assert report["real_pr_count"] == 1
    assert _infra(report)["failed_runs"] == 1
    assert _infra(report)["status"] == "exact"
    assert _infra(report)["bounds"]["failed_runs"] == {"min": 1, "max": 1}


# ------------------------------------------------------------ T06 (C6)


def test_t06_in_execution_retries_are_not_attempts(tmp_path):
    write_run(tmp_path, "a", retries=3, event_id=E1, execution=gha(101))
    run = _only_run(_ledger(tmp_path))
    assert run["executions_exact"] == 1
    assert run["execution_count"] == 1


# ------------------------------------------------------------ T07 (C3)


def test_t07_one_event_archived_three_times(tmp_path):
    write_run(tmp_path, "a", event_id=E1, execution=gha(101))
    write_run(tmp_path, "archive/a", event_id=E1, execution=gha(101), with_decision=False)
    write_run(tmp_path, "docs/qualification/artifacts/x", event_id=E1, execution=gha(101))
    ledger = _ledger(tmp_path)
    run = _only_run(ledger)
    assert run["distinct_payloads"] == 1
    assert run["occurrence_log"][0]["copies"] == 3
    assert run["occurrence_log"][0]["event_key_kind"] == "event_id"
    assert run["policy_digest_verified"] is True
    assert run["execution_count"] == 1
    assert "multiple_events_per_execution" not in _kinds(ledger)


# ------------------------------------------------------------ T08 (C4)


def test_t08_conflicting_event_id_is_ineligible_and_never_counts(tmp_path):
    write_run(tmp_path, "a", duration_seconds=1.5, event_id=E1, execution=gha(101))
    write_run(tmp_path, "b", duration_seconds=9.5, event_id=E1, execution=gha(101))
    ledger = _ledger(tmp_path)
    conflicts = [a for a in ledger["anomalies"] if a["kind"] == "event_id_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["event_id"] == E1
    assert conflicts[0]["sources"] == ["a/telemetry.jsonl", "b/telemetry.jsonl"]
    run = _only_run(ledger)
    assert [o["eligible"] for o in run["occurrence_log"]] == [False]
    assert run["last_eligible_created_at"] is None
    assert run["policy_digest_verified"] is False
    assert run["executions_exact"] == 0
    assert run["execution_count"] == 0
    assert run["occurrence_final_statuses"] == {}
    assert run["occurrence_stale_states"] == {}
    assert ledger["entries"][0]["current"] is False
    report = _report(ledger)
    assert report["real_pr_count"] == 0
    assert report["counted_prs"] == []


def test_t08b_conflict_across_run_groups_is_still_detected(tmp_path):
    """S07: the same event_id reused across repositories. Grouping by run
    must not hide a conflict."""
    write_run(tmp_path, "a", repo="faraday-build/one", event_id=E1, execution=gha(101))
    write_run(tmp_path, "b", repo="faraday-build/two", event_id=E1, execution=gha(101))
    write_run(tmp_path, "c", repo="faraday-build/three", event_id=E2, execution=gha(102))
    ledger = _ledger(tmp_path)
    assert [a["event_id"] for a in ledger["anomalies"] if a["kind"] == "event_id_conflict"] == [E1]
    assert _report(ledger)["real_pr_count"] == 1
    assert ledger["counts"]["identity"]["events"] == 1


def test_t08c_conflict_cannot_add_policy_evidence(tmp_path):
    """A conflicted copy's decision.json must not verify the run's digest."""
    write_run(tmp_path, "good", event_id=E2, execution=gha(102), with_decision=False)
    write_run(tmp_path, "a", event_id=E1, execution=gha(101), duration_seconds=1.0)
    write_run(tmp_path, "b", event_id=E1, execution=gha(101), duration_seconds=2.0)
    run = _only_run(_ledger(tmp_path))
    assert run["policy_digest_verified"] is False


def test_t08d_conflicted_failures_are_not_infrastructure_failures(tmp_path):
    write_run(tmp_path, "a", final_status="failed", failure_mode="runner_lost",
              duration_seconds=1.0, event_id=E1, execution=gha(101))
    write_run(tmp_path, "b", final_status="failed", failure_mode="runner_lost",
              duration_seconds=2.0, event_id=E1, execution=gha(101))
    infra = _infra(_report(_ledger(tmp_path)))
    assert infra["failed_runs"] == 0
    assert infra["failure_modes"] == {}
    assert infra["bounds"]["failed_runs"] == {"min": 0, "max": 0}
    assert _report(_ledger(tmp_path))["drift"]["stale_state_counts"] == {}


def test_t08e_conflicted_run_on_a_current_head_never_joins_the_count(tmp_path):
    """A clean run makes the head current. A conflicted run of another
    reviewer version on the same head is completed and fresh, but its
    events are ineligible, so it must not reach the counted set."""
    write_run(tmp_path, "clean", created_at="2026-11-01T10:00:00Z", event_id=E2,
              execution=gha(102))
    for name, dur in (("x", 1.0), ("y", 2.0)):
        write_run(tmp_path, name, created_at="2026-11-01T11:00:00Z", reviewer_version="v9.9.9",
                  duration_seconds=dur, event_id=E1, execution=gha(101))
    ledger = _ledger(tmp_path)
    assert sum(1 for e in ledger["entries"] if e["current"]) == 2  # same head, two digests
    report = _report(ledger)
    assert report["real_pr_count"] == 1
    assert report["counted_prs"][0]["review_id"] == _runs(ledger)[0]["review_id"]
    assert report["drift"]["reviewer_versions"] == [REVIEWER_VERSION]
    assert report["drift"]["reviewer_version_drift"] is False
    assert report["drift"]["policy_digest_unverified_runs"] == 0


def test_t08f_representative_is_never_a_conflicted_event(tmp_path):
    write_run(tmp_path, "clean", cls="yellow", created_at="2026-11-01T10:00:00Z", event_id=E2,
              execution=gha(102))
    for name, dur in (("x", 1.0), ("y", 2.0)):
        write_run(tmp_path, name, cls="red", created_at="2026-11-01T11:00:00Z",
                  duration_seconds=dur, event_id=E1, execution=gha(101))
    run = _only_run(_ledger(tmp_path))
    assert run["classifier_class"] == "yellow"
    assert run["created_at"] == "2026-11-01T10:00:00Z"
    assert run["last_created_at"] == "2026-11-01T10:00:00Z"
    assert run["occurrence_final_statuses"] == {"completed": 1}


def test_t04c_integral_float_run_fields_are_the_same_execution(tmp_path):
    """Codex P2 (#7, qualification.py:181). JSON Schema accepts 101.0 as an
    integer, so a re-serialized archive can carry it. It must not create a
    second execution identity, which would hide C7 and inflate the counts."""
    write_run(tmp_path, "a", event_id=E1, execution=gha(101, 1))
    write_run(tmp_path, "b", event_id=E2, execution={"provider": "github_actions",
                                                     "run_id": 101.0, "run_attempt": 1.0})
    ledger = _ledger(tmp_path)
    assert "multiple_events_per_execution" in _kinds(ledger)
    assert ledger["counts"]["identity"]["executions_exact"] == 1
    assert _only_run(ledger)["executions_exact"] == 1


def test_t04d_duplicate_execution_descriptor_is_location_independent(tmp_path):
    """Codex P2 (#7 full-diff review, qualification.py:443). With 101 and
    101.0 for one execution, the multiple_events_per_execution descriptor
    must not depend on which archive location is visited last (C9). It
    carries normalized ints; occurrence records keep the raw form."""
    float_exec = {"provider": "github_actions", "run_id": 101.0, "run_attempt": 1.0}
    descriptors = []
    for layout, (first, second) in enumerate((((E1, gha(101, 1)), (E2, float_exec)),
                                              ((E2, float_exec), (E1, gha(101, 1))))):
        root = tmp_path / f"layout{layout}"
        root.mkdir()
        write_run(root, "a", event_id=first[0], execution=first[1])
        write_run(root, "b", event_id=second[0], execution=second[1])
        ledger = _ledger(root)
        [anomaly] = [a for a in ledger["anomalies"]
                     if a["kind"] == "multiple_events_per_execution"]
        descriptors.append(canonical_bytes(anomaly))
        assert anomaly["execution"]["run_id"] == 101
        assert type(anomaly["execution"]["run_id"]) is int
        assert type(anomaly["execution"]["run_attempt"]) is int
        assert sorted(type(o["execution"]["run_id"]).__name__
                      for o in _only_run(ledger)["occurrence_log"]) == ["float", "int"]
    assert descriptors[0] == descriptors[1]


def test_t18d_integral_float_run_fields_match_the_binding(tmp_path):
    good = write_run(tmp_path, "good", pr=1, event_id=E1,
                     execution={"provider": "github_actions", "run_id": 101.0, "run_attempt": 1.0})
    _binding(good)
    assert "run_binding_mismatch" not in _kinds(_ledger(tmp_path))


def test_t04b_repository_is_part_of_execution_identity(tmp_path):
    """Equal run_id and run_attempt in two repositories are two executions."""
    write_run(tmp_path, "a", repo="faraday-build/one", event_id=E1, execution=gha(101))
    write_run(tmp_path, "b", repo="faraday-build/two", event_id=E2, execution=gha(101))
    ledger = _ledger(tmp_path)
    assert "multiple_events_per_execution" not in _kinds(ledger)
    assert ledger["counts"]["identity"]["executions_exact"] == 2


# ------------------------------------------------------- T09 / T10 (budget)


def test_t09_budget_exhaustion_in_two_identified_executions_is_exact(tmp_path):
    for name, eid, rid in (("a", E1, 101), ("b", E2, 102)):
        write_run(tmp_path, name, created_at=SAME_SECOND, cls="block",
                  reasons=("github_api_budget_exceeded",), event_id=eid, execution=gha(rid))
    infra = _infra(_report(_ledger(tmp_path)))
    assert infra["api_budget_exceeded_runs"] == 2
    assert infra["status"] == "exact"
    assert infra["bounds"]["api_budget_exceeded_runs"] == {"min": 2, "max": 2}


def test_t10_budget_exhaustion_on_identical_legacy_copies_is_bounded(tmp_path):
    for name in ("a", "b"):
        write_run(tmp_path, name, created_at=SAME_SECOND, cls="block",
                  reasons=("github_api_budget_exceeded",), contract_version=V03)
    infra = _infra(_report(_ledger(tmp_path)))
    assert infra["api_budget_exceeded_runs"] == 1
    assert infra["status"] == "bounded"
    assert infra["bounds"]["api_budget_exceeded_runs"] == {"min": 1, "max": 2}


def test_t10b_one_execution_with_two_events_is_one_failure(tmp_path):
    for name, eid in (("a", E1), ("b", E2)):
        write_run(tmp_path, name, final_status="failed", failure_mode="runner_lost",
                  event_id=eid, execution=gha(101))
    infra = _infra(_report(_ledger(tmp_path)))
    assert infra["failed_runs"] == 1
    assert infra["failure_modes"] == {"runner_lost": 1}


# ------------------------------------------- T11 / T12 / T13 / T14 (unchanged)


def test_t11_newer_head_with_only_failed_occurrences_does_not_become_current(tmp_path):
    write_run(tmp_path, "old", head="h1", created_at="2026-11-01T10:00:00Z", event_id=E1,
              execution=gha(101))
    write_run(tmp_path, "new", head="h2", created_at="2026-11-02T10:00:00Z",
              final_status="failed", failure_mode="runner_lost", event_id=E2,
              execution=gha(102))
    from qualification_support import sha
    ledger = _ledger(tmp_path)
    assert [e["head_sha"] for e in ledger["entries"] if e["current"]] == [sha("h1")]
    assert _report(ledger)["counted_prs"][0]["head_sha"] == sha("h1")


def test_t12_equal_second_heads_stay_ambiguous_whatever_the_run_ids(tmp_path):
    write_run(tmp_path, "a", head="h1", created_at=SAME_SECOND, event_id=E1, execution=gha(5))
    write_run(tmp_path, "b", head="h2", created_at=SAME_SECOND, event_id=E2, execution=gha(9))
    assert "current_head_ambiguous" in _kinds(_ledger(tmp_path))


def test_t13_verified_on_one_copy_absent_on_another_is_verified(tmp_path):
    write_run(tmp_path, "a", event_id=E1, execution=gha(101))
    write_run(tmp_path, "b", event_id=E1, execution=gha(101), with_decision=False)
    assert _only_run(_ledger(tmp_path))["policy_digest_verified"] is True


def test_t14_two_verified_digests_fail_closed(tmp_path):
    from qualification_support import DIGEST_B, write_run as w
    w(tmp_path, "a", event_id=E1, execution=gha(101))
    w(tmp_path, "b", event_id=E2, execution=gha(102), digest=DIGEST_B)
    ledger = _ledger(tmp_path)
    # Different digests are different fingerprints: two runs, each verified.
    assert all(r["policy_digest_verified"] for r in _runs(ledger))


# ------------------------------------------------------------ T15 (C9)


def _t15_tree(root: Path, order: tuple[int, ...]) -> None:
    makers = [
        lambda: write_run(root, "x1", created_at=SAME_SECOND, duration_seconds=1.0,
                          event_id=E1, execution=gha(101)),
        lambda: write_run(root, "x2", created_at=SAME_SECOND, duration_seconds=2.0,
                          event_id=E2, execution=gha(101)),
        lambda: write_run(root, "x3", created_at=SAME_SECOND, contract_version=V03),
        lambda: write_run(root, "x4", created_at=SAME_SECOND, contract_version=V03),
        lambda: write_run(root, "x5", created_at=SAME_SECOND, event_id=E3, execution=gha(103),
                          final_status="failed", failure_mode="runner_lost"),
        lambda: write_run(root, "x6", created_at=SAME_SECOND, event_id=E3, execution=gha(103),
                          duration_seconds=7.0),
    ]
    for i in order:
        makers[i]()


def test_t15_ledger_is_independent_of_creation_order_and_names(tmp_path):
    outputs = set()
    for n, order in enumerate(itertools.islice(itertools.permutations(range(6)), 0, 720, 97)):
        root = tmp_path / f"p{n}"
        _t15_tree(root, order)
        outputs.add(canonical_bytes(aggregate(root, generated_at=TS)))
    assert len(outputs) == 1
    # Renaming every directory changes only the source/input paths.
    a, b = tmp_path / "ra", tmp_path / "rb"
    _t15_tree(a, tuple(range(6)))
    _t15_tree(b, tuple(range(6)))
    for child in sorted(b.iterdir()):
        child.rename(b / f"zz-{child.name}")
    la, lb = aggregate(a, generated_at=TS), aggregate(b, generated_at=TS)
    assert la["counts"] == lb["counts"]
    assert [r["executions_exact"] for r in _runs(la)] == [r["executions_exact"] for r in _runs(lb)]


# ------------------------------------------------------------ T16 (C8)


def test_t16_mixed_legacy_and_identified_events_group_together(tmp_path):
    write_run(tmp_path, "a", event_id=E1, execution=gha(101), duration_seconds=1.0)
    write_run(tmp_path, "b", event_id=E2, execution=gha(102), duration_seconds=2.0)
    write_run(tmp_path, "c", contract_version=V03, duration_seconds=3.0)
    write_run(tmp_path, "d", contract_version=V03, duration_seconds=3.0)
    ledger = _ledger(tmp_path)
    run = _only_run(ledger)
    assert run["distinct_payloads"] == 3
    assert run["executions_exact"] == 2
    assert run["execution_count"] is None
    assert run["execution_count_bounds"] == {"min": 3, "max": 4}
    assert ledger["counts"]["identity"] == {
        "events": 3, "executions_exact": 2, "execution_count": None,
        "execution_count_bounds": {"min": 3, "max": 4}, "legacy_events": 1,
        "legacy_copies": 2, "event_id_conflicts": 0, "invalid_events": 0,
    }


# ------------------------------------------------------------ T17 (C1)


def test_t17_identity_on_a_v030_record_is_invalid(tmp_path):
    write_run(tmp_path, "a", contract_version=V03, event_id=E1)
    ledger = _ledger(tmp_path)
    assert "invalid_telemetry_event" in _kinds(ledger)
    assert ledger["entries"] == []
    assert ledger["counts"]["identity"]["invalid_events"] == 1


# ------------------------------------------------------------ T18 (4.6)


def _binding(run_dir: Path, **overrides) -> None:
    from qualification_support import sha
    binding = {"caller_repo": REPO, "pr_number": "1", "head_sha": sha("h1"),
               "base_sha": sha("base"), "reviewer_sha": "r" * 40, "governance_sha": "g" * 40,
               "run_id": "101", "run_attempt": "1"}
    binding.update(overrides)
    (run_dir / "run-binding.json").write_text(json.dumps(binding, sort_keys=True))


def test_t18_run_binding_mismatch_is_reported_and_changes_no_count(tmp_path):
    good = write_run(tmp_path, "good", pr=1, event_id=E1, execution=gha(101))
    _binding(good)
    baseline = _ledger(tmp_path)
    assert "run_binding_mismatch" not in _kinds(baseline)
    _binding(good, run_attempt="2")
    ledger = _ledger(tmp_path)
    mismatches = [a for a in ledger["anomalies"] if a["kind"] == "run_binding_mismatch"]
    assert [m["fields"] for m in mismatches] == [["run_attempt"]]
    assert ledger["counts"]["identity"] == baseline["counts"]["identity"]
    assert gate_view(_report(ledger)) == gate_view(_report(baseline))


def test_t18b_run_binding_never_upgrades_a_legacy_record(tmp_path):
    legacy = write_run(tmp_path, "legacy", pr=1, contract_version=V03)
    _binding(legacy)
    run = _only_run(_ledger(tmp_path))
    assert run["occurrence_log"][0]["execution"] is None
    assert run["execution_count"] is None


def test_t18c_repository_spelling_in_binding_is_casefolded(tmp_path):
    good = write_run(tmp_path, "good", pr=1, event_id=E1, execution=gha(101))
    _binding(good, caller_repo=REPO.upper())
    assert "run_binding_mismatch" not in _kinds(_ledger(tmp_path))


# ------------------------------------------------------------ T19 / T21


def test_t19_floor_holds_and_many_executions_of_one_pr_count_once(tmp_path):
    for i in range(25):
        write_run(tmp_path, f"x{i}", created_at=f"2026-11-01T10:{i:02d}:00Z",
                  event_id=f"4f1c2a3b-5d6e-4f70-8a91-{i:012d}", execution=gha(1000 + i))
    ledger = _ledger(tmp_path)
    assert _only_run(ledger)["executions_exact"] == 25
    assert _report(ledger)["real_pr_count"] == 1
    with pytest.raises(QualificationError):
        _report(ledger, threshold=19)


def test_t21_synthetic_pr_with_many_executions_is_excluded(tmp_path):
    for i in range(5):
        write_run(tmp_path, f"s{i}", repo="faraday-build/sandbox", created_at=f"2026-11-01T10:{i:02d}:00Z",
                  event_id=f"4f1c2a3b-5d6e-4f70-8a91-{i:012d}", execution=gha(2000 + i))
    report = _report(_ledger(tmp_path), [{"repository": "faraday-build/sandbox", "pr_number": 1,
                                          "reason": "sandbox"}])
    assert report["real_pr_count"] == 0
    assert report["synthetic_excluded"][0]["present_in_ledger"] is True


# ------------------------------------------------------------ T20 / T26


def test_t26_prior_v1_ledger_is_accepted_and_missing_inputs_still_fail(tmp_path):
    prior = json.loads((FIXTURES / "v1-prior-ledger.json").read_text(encoding="utf-8"))
    assert prior["format"] == "faraday-review/qualification-ledger/v1"
    root = tmp_path / "in"
    build_v1_prior_tree(root)
    write_run(root, "p3", pr=3, head="h3", created_at="2026-11-02T10:00:00Z", event_id=E1,
              execution=gha(301))
    ledger = aggregate(root, generated_at=TS, prior_ledger=prior)
    assert ledger["format"] == "faraday-review/qualification-ledger/v2"
    shutil.rmtree(root / "p2")
    with pytest.raises(QualificationError, match="missing"):
        aggregate(root, generated_at=TS, prior_ledger=prior)


def test_t26b_prior_v2_ledger_is_accepted(tmp_path):
    write_run(tmp_path, "a", event_id=E1, execution=gha(101))
    prior = _ledger(tmp_path)
    write_run(tmp_path, "b", pr=2, head="h2", event_id=E2, execution=gha(102))
    assert len(aggregate(tmp_path, generated_at=TS, prior_ledger=prior)["entries"]) == 2


def test_t26c_unknown_prior_format_fails_closed(tmp_path):
    write_run(tmp_path, "a", event_id=E1, execution=gha(101))
    prior = _ledger(tmp_path)
    prior["format"] = "faraday-review/qualification-ledger/v9"
    with pytest.raises(QualificationError):
        aggregate(tmp_path, generated_at=TS, prior_ledger=prior)


# ------------------------------------------------------------ T29


def test_t29_identity_changes_no_gate_figure_on_v030_inputs(tmp_path):
    expected = json.loads((FIXTURES / "v1-baseline-gate.json").read_text(encoding="utf-8"))
    build_v030_baseline(tmp_path)
    ledger = _ledger(tmp_path)
    report = _report(ledger, BASELINE_SYNTHETIC)
    assert gate_view(report) == expected["gate_view"]
    for key in ("events_read", "runs_after_dedup", "duplicates_collapsed", "entries",
                "superseded_entries"):
        assert ledger["counts"][key] == expected["ledger_counts"][key], key
    infra, base = _infra(report), expected["infrastructure_failures"]
    assert infra["failed_runs"] == base["failed_runs"]
    assert infra["failure_modes"] == base["failure_modes"]
    assert infra["api_budget_exceeded_runs"] == base["api_budget_exceeded_runs"]
    assert infra["status"] == "bounded"


# ------------------------------------------------------------ T24 (C1)


def _vectors() -> dict:
    path = (Path(__file__).resolve().parents[2] / "src" / "faraday_review" / "schemas"
            / "conformance" / "review-telemetry-identity.vectors.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_t24_every_record_vector_is_ingested_or_rejected_as_the_contract_says(tmp_path):
    records = _vectors()["records"]
    assert len(records) == 35
    for vec in records:
        root = tmp_path / vec["id"]
        (root / "r").mkdir(parents=True)
        line = json.dumps(vec["record"], sort_keys=True, separators=(",", ":"))
        (root / "r" / "telemetry.jsonl").write_text(line + "\n", encoding="utf-8")
        ledger = aggregate(root, generated_at=TS)
        if vec["valid"]:
            assert ledger["counts"]["events_read"] == 1, vec["id"]
            assert ledger["counts"]["identity"]["invalid_events"] == 0, vec["id"]
        else:
            assert ledger["counts"]["events_read"] == 0, vec["id"]
            assert ledger["counts"]["identity"]["invalid_events"] == 1, vec["id"]
            assert "invalid_telemetry_event" in _kinds(ledger), vec["id"]
