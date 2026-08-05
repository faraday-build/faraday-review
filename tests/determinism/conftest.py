import pytest

from faraday_review.governance import validator_client

# The one golden case exercising an injected infrastructure failure
# (Correction 2: distinct from pr_too_large, which arrives as evidence).
INFRASTRUCTURE_BY_CASE = {
    "block-api-budget-exceeded": "github_api_budget_exceeded",
}

# evaluated_at is the single volatile field; goldens are generated with a
# pinned value and the comparator strips it, so live evaluations here use
# the real clock on purpose — the harness must prove the strip works.


@pytest.fixture
def evaluate():
    def _evaluate(root):
        expected = (root / "expected-policy-digest.txt").read_text(encoding="utf-8").strip()
        result = validator_client.evaluate(
            context_path=root / "context.json",
            evidence_path=root / "evidence.json",
            bundle_root=root / "policy-bundle",
            expected_digest=expected or None,
            digest_source="cli_flag" if expected else "absent",
            infrastructure_failure=INFRASTRUCTURE_BY_CASE.get(root.name),
        )
        return result.decision

    return _evaluate
