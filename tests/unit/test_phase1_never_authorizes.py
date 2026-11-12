"""Phase 1 invariants the reusable shadow workflow relies on.

- An empty FARADAY_POLICY_DIGEST (how an unset caller variable reaches the
  workflow) is treated as absent and Blocks.
- No golden outcome, of any risk class, authorizes approval or merge.
"""

from pathlib import Path

import pytest

from faraday_review.canonical import read_canonical
from faraday_review.cli import _digest_source
from faraday_review.governance.policy_loader import load_bundle

GOLDEN = Path(__file__).resolve().parents[1] / "determinism" / "golden"
BUNDLE = Path(__file__).resolve().parents[1] / "fixtures" / "policy-bundle"


@pytest.mark.parametrize("value", ["", None])
def test_empty_or_missing_env_digest_is_absent(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FARADAY_POLICY_DIGEST", raising=False)
    else:
        monkeypatch.setenv("FARADAY_POLICY_DIGEST", value)
    assert _digest_source(None) == (None, "absent")
    assert _digest_source("") == (None, "absent")


def test_unpinned_golden_blocks():
    decision = read_canonical(GOLDEN / "block-digest-unpinned" / "expected-decision.json")
    assert decision["risk_class"] == "block"
    assert decision["blocking_reasons"] == ["policy_digest_unpinned"]
    assert decision["expected_policy_digest_source"] == "absent"


@pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir() if p.is_dir()))
def test_no_golden_outcome_authorizes(case):
    decision = read_canonical(GOLDEN / case / "expected-decision.json")
    assert decision["approval_authorized"] is False
    assert decision["merge_authorized"] is False


def test_phase1_policy_authorizes_no_class():
    bundle, reasons = load_bundle(BUNDLE)
    assert bundle is not None and reasons == []
    classes = bundle.approval_policy["risk_classes"]
    assert set(classes) >= {"green", "yellow", "red", "block"}
    for name, rules in classes.items():
        assert rules["approval_authorized"] is False, name
        assert rules["merge_authorized"] is False, name
