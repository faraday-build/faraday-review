"""Autonomy prohibitions: nothing in Phase 1 can authorize anything."""

import json
from pathlib import Path

from faraday_review.canonical import read_canonical

GOLDEN = Path(__file__).resolve().parents[1] / "determinism" / "golden"
SRC = Path(__file__).resolve().parents[2] / "src" / "faraday_review"


def test_no_golden_decision_authorizes_anything():
    """Green cannot approve; Block cannot approve; nothing merges."""
    decisions = sorted(GOLDEN.glob("*/expected-decision.json"))
    assert len(decisions) >= 12
    for path in decisions:
        decision = read_canonical(path)
        assert decision["approval_authorized"] is False, path
        assert decision["merge_authorized"] is False, path


def test_green_does_not_require_founder_but_still_authorizes_nothing():
    decision = read_canonical(GOLDEN / "green-docs" / "expected-decision.json")
    assert decision["risk_class"] == "green"
    assert decision["founder_review_required"] is False
    assert decision["approval_authorized"] is False
    assert decision["merge_authorized"] is False


def test_runtime_never_calls_approve_or_merge_endpoints():
    """No module in the plane may hit PR review-approval or merge APIs."""
    forbidden = ("/pulls/{pr}/reviews", "pulls/%s/reviews", "/merge\"", "put(\"/repos")
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        assert "\"approve\"" not in text, path
        assert "event\": \"approve" not in text, path
        for needle in forbidden:
            assert needle.lower() not in text, (path, needle)


def test_policy_bundle_pins_authorization_false():
    bundle = Path(__file__).resolve().parents[1] / "fixtures" / "policy-bundle"
    schema = json.loads((bundle / "policy.schema.json").read_text(encoding="utf-8"))
    risk_class_policy = schema["$defs"]["risk_class_policy"]["properties"]
    assert risk_class_policy["approval_authorized"] == {"const": False}
    assert risk_class_policy["merge_authorized"] == {"const": False}
