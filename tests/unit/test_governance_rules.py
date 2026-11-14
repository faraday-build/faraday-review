"""Governance-side rules: digest pin, contract window, evidence class."""

from pathlib import Path

import pytest

from faraday_review.canonical import bundle_digest
from faraday_review.governance.authorization import (
    ACCEPTED_EVIDENCE_CLASSES,
    contract_version_reasons,
    evidence_class_reasons,
)
from faraday_review.governance.digest import verify_pinned_digest
from faraday_review.governance.policy_loader import load_bundle

BUNDLE = Path(__file__).resolve().parents[1] / "fixtures" / "policy-bundle"


def test_absent_pin_blocks():
    _, reasons = verify_pinned_digest(BUNDLE, None, "absent")
    assert reasons == ["policy_digest_unpinned"]


def test_mismatch_blocks():
    _, reasons = verify_pinned_digest(BUNDLE, "sha256:" + "0" * 64, "cli_flag")
    assert reasons == ["policy_digest_mismatch"]


def test_correct_pin_passes():
    expected = bundle_digest(BUNDLE)
    actual, reasons = verify_pinned_digest(BUNDLE, expected, "environment")
    assert reasons == [] and actual == expected


def test_bundle_loads_and_validates():
    bundle, reasons = load_bundle(BUNDLE)
    assert reasons == []
    assert bundle.policy_version == "1.0.0"
    assert bundle.provider_requirements["deterministic_only"] is True


def test_missing_bundle_is_policy_missing(tmp_path: Path):
    bundle, reasons = load_bundle(tmp_path / "nope")
    assert bundle is None and reasons == ["policy_missing"]


def test_broken_bundle_fails_closed(tmp_path: Path):
    import shutil

    root = tmp_path / "bundle"
    shutil.copytree(BUNDLE, root)
    (root / "approval-policy.yaml").write_text("policy_version: [broken\n")
    bundle, reasons = load_bundle(root)
    assert bundle is None and reasons == ["policy_missing"]


def test_contract_minor_window():
    """Ruling 16: accept N and N-1; reject N-2 and other majors."""
    assert contract_version_reasons("v0.4.0") == ([], 4)
    assert contract_version_reasons("v0.3.9") == ([], 3)
    assert contract_version_reasons("v0.2.0")[0] == ["contract_version_unsupported"]
    assert contract_version_reasons("v0.5.0")[0] == ["contract_version_unsupported"]
    assert contract_version_reasons("v1.4.0")[0] == ["contract_version_unsupported"]
    assert contract_version_reasons(None)[0] == ["contract_version_unsupported"]
    assert contract_version_reasons("0.3.0")[0] == ["contract_version_unsupported"]


def test_evidence_class_allowlist_no_coercion():
    assert evidence_class_reasons("deterministic", True) == []
    assert evidence_class_reasons("mixed", False) == []
    # deterministic_only policy rejects mixed evidence
    assert evidence_class_reasons("mixed", True) == ["evidence_malformed"]
    # no case-insensitive or whitespace coercion, no default for absent
    assert evidence_class_reasons("Deterministic", True) == ["unknown_evidence_class"]
    assert evidence_class_reasons(" deterministic", True) == ["unknown_evidence_class"]
    assert evidence_class_reasons("model", True) == ["unknown_evidence_class"]
    assert evidence_class_reasons(None, True) == ["unknown_evidence_class"]


def test_allowlist_is_exactly_two_members():
    assert ACCEPTED_EVIDENCE_CLASSES == frozenset({"deterministic", "mixed"})
