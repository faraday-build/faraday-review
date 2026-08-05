"""Hostile and malformed inputs must always fail closed."""

import shutil
from pathlib import Path

import pytest

from faraday_review.canonical import bundle_digest, read_canonical, write_canonical
from faraday_review.governance.validator_client import evaluate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN = Path(__file__).resolve().parents[1] / "determinism" / "golden" / "green-docs"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("context.json", "evidence.json"):
        shutil.copy(GOLDEN / name, tmp_path / name)
    shutil.copytree(FIXTURES / "policy-bundle", tmp_path / "policy-bundle")
    return tmp_path


def _evaluate(root: Path, mutate_evidence=None, mutate_context=None):
    if mutate_evidence:
        doc = read_canonical(root / "evidence.json")
        mutate_evidence(doc)
        write_canonical(root / "evidence.json", doc)
    if mutate_context:
        doc = read_canonical(root / "context.json")
        mutate_context(doc)
        write_canonical(root / "context.json", doc)
    return evaluate(
        context_path=root / "context.json",
        evidence_path=root / "evidence.json",
        bundle_root=root / "policy-bundle",
        expected_digest=bundle_digest(root / "policy-bundle"),
        digest_source="cli_flag",
    ).decision


def _assert_blocked(decision, reason):
    assert decision["risk_class"] == "block"
    assert reason in decision["blocking_reasons"]
    assert decision["approval_authorized"] is False
    assert decision["merge_authorized"] is False
    assert decision["founder_review_required"] is True


def test_repository_mismatch_blocks(workspace):
    d = _evaluate(workspace, mutate_evidence=lambda e: e.update(repository="evil/other"))
    _assert_blocked(d, "evidence_malformed")


def test_short_sha_in_evidence_blocks(workspace):
    d = _evaluate(workspace, mutate_evidence=lambda e: e.update(head_sha=e["head_sha"][:12]))
    _assert_blocked(d, "evidence_malformed")


def test_mixed_case_sha_is_normalized_not_rejected(workspace):
    d = _evaluate(workspace, mutate_evidence=lambda e: e.update(head_sha=e["head_sha"].upper()))
    assert d["risk_class"] == "green"


def test_unknown_evidence_class_blocks_without_coercion(workspace):
    d = _evaluate(workspace, mutate_evidence=lambda e: e.update(evidence_class="DETERMINISTIC"))
    _assert_blocked(d, "unknown_evidence_class")


def test_blocking_finding_from_unknown_check_fails_closed(workspace):
    finding = {"rule_id": "x", "check": "novel_check", "severity": "blocking", "message": "?"}
    d = _evaluate(
        workspace,
        mutate_evidence=lambda e: e.update(deterministic_findings=[finding]),
    )
    _assert_blocked(d, "evidence_malformed")


def test_unparseable_evidence_blocks(workspace):
    (workspace / "evidence.json").write_text("{not json", encoding="utf-8")
    d = _evaluate(workspace)
    _assert_blocked(d, "evidence_malformed")


def test_deleted_bundle_blocks_with_policy_missing(workspace):
    shutil.rmtree(workspace / "policy-bundle")
    d = evaluate(
        context_path=workspace / "context.json",
        evidence_path=workspace / "evidence.json",
        bundle_root=workspace / "policy-bundle",
        expected_digest="sha256:" + "9" * 64,
        digest_source="cli_flag",
    ).decision
    assert "policy_missing" in d["blocking_reasons"]
    assert "policy_digest_mismatch" in d["blocking_reasons"]


def test_blocking_reasons_are_sorted_lists(workspace):
    d = _evaluate(
        workspace,
        mutate_evidence=lambda e: e.update(
            repository="evil/other", evidence_class="model", contract_version="v0.1.0"
        ),
    )
    reasons = d["blocking_reasons"]
    assert reasons == sorted(reasons)
    assert isinstance(reasons, list)
