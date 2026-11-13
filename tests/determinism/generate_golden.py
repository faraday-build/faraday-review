"""Regenerate the golden fixtures.

Run from the repo root after a DELIBERATE policy or contract change:

    .venv/bin/python tests/determinism/generate_golden.py

expected-decision.json is produced by the validator itself with a pinned
evaluated_at, then reviewed by a human in the PR diff — the goldens encode
ratified behavior, so unexplained diffs here are defects, not updates.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from faraday_review.canonical import bundle_digest, write_canonical  # noqa: E402
from faraday_review.governance import validator_client  # noqa: E402

GOLDEN = REPO / "tests" / "determinism" / "golden"
BUNDLE_TEMPLATE = REPO / "tests" / "fixtures" / "policy-bundle"

BASE = "1bdf555a2c9de08a6f3b7c41d95e8f20a4b6c8d1"
HEAD = "f674025b3d0ea19c7e4f8d52ea6f9031b5c7d9e2"
OTHER_HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
REPOSITORY = "faraday-build/shadow-target"
PINNED_AT = "2026-08-04T00:00:00Z"
WRONG_DIGEST = "sha256:" + "0" * 64

CHECK_OK = [{"context": "ci", "state": "success"}]


def context(paths: list[str], checks=None, head=HEAD) -> dict:
    return {
        "contract_version": "v0.3.0",
        "repository": REPOSITORY,
        "pr_number": 7,
        "base_sha": BASE,
        "head_sha": head,
        "base_ref": "main",
        "head_ref": "change",
        "draft": False,
        "changed_file_count": len(paths),
        "diff_bytes": 1024 * len(paths),
        "changed_files": [{"path": p, "status": "modified"} for p in paths],
        "required_checks_observed": checks if checks is not None else CHECK_OK,
        "collected_at": PINNED_AT,
    }


def evidence(head=HEAD, findings=None, evidence_class="deterministic") -> dict:
    return {
        "contract_version": "v0.3.0",
        "repository": REPOSITORY,
        "pr_number": 7,
        "base_sha": BASE,
        "head_sha": head,
        "reviewer_version": "v0.1.0",
        "evidence_class": evidence_class,
        "deterministic_findings": findings or [],
        "provider_results": [],
        "required_providers": [],
        "collected_at": PINNED_AT,
    }


SECRET_FINDING = {
    "rule_id": "generic-api-key",
    "check": "secret_scan",
    "severity": "blocking",
    "path": "src/settings.py",
    "line": 9,
    "rule_class": "high-entropy-string",
    "message": "secret detected by rule 'generic-api-key' (value withheld by contract)",
}

CASES: dict[str, dict] = {
    "green-docs": {"context": context(["docs/RUNBOOK.md", "README.md"]), "evidence": evidence()},
    "green-tests": {"context": context(["tests/unit/test_core.py"]), "evidence": evidence()},
    "yellow-business-logic": {"context": context(["src/app/core.py"]), "evidence": evidence()},
    "yellow-dependency-bump": {"context": context(["package.json", "uv.lock"]), "evidence": evidence()},
    "red-governance-path": {"context": context(["review-policy/approval-policy.yaml"]), "evidence": evidence()},
    "red-workflow-path": {"context": context([".github/workflows/ci.yml"]), "evidence": evidence()},
    "block-failing-ci": {
        "context": context(["docs/RUNBOOK.md"], checks=[{"context": "ci", "state": "failure"}]),
        "evidence": evidence(),
    },
    "block-stale-sha": {"context": context(["docs/RUNBOOK.md"]), "evidence": evidence(head=OTHER_HEAD)},
    "block-digest-mismatch": {
        "context": context(["docs/RUNBOOK.md"]),
        "evidence": evidence(),
        "expected_digest": WRONG_DIGEST,
    },
    "block-digest-unpinned": {
        # No pin supplied (empty FARADAY_POLICY_DIGEST in the reusable
        # workflow): must Block policy_digest_unpinned, never pass.
        "context": context(["docs/RUNBOOK.md"]),
        "evidence": evidence(),
        "expected_digest": "",
    },
    "block-missing-required-check": {
        "context": context(["docs/RUNBOOK.md"], checks=[{"context": "ci", "state": "missing"}]),
        "evidence": evidence(),
    },
    "block-secret-detected": {
        "context": context(["src/settings.py"]),
        "evidence": evidence(findings=[SECRET_FINDING]),
    },
    "block-api-budget-exceeded": {
        "context": context(["docs/RUNBOOK.md"]),
        "evidence": evidence(),
        "infrastructure_failure": "github_api_budget_exceeded",
    },
}


def main() -> None:
    if GOLDEN.exists():
        shutil.rmtree(GOLDEN)
    correct_digest = bundle_digest(BUNDLE_TEMPLATE)
    for name, spec in CASES.items():
        root = GOLDEN / name
        root.mkdir(parents=True)
        shutil.copytree(BUNDLE_TEMPLATE, root / "policy-bundle")
        write_canonical(root / "context.json", spec["context"])
        write_canonical(root / "evidence.json", spec["evidence"])
        expected_digest = spec.get("expected_digest", correct_digest)
        (root / "expected-policy-digest.txt").write_text(expected_digest + "\n", encoding="utf-8")
        result = validator_client.evaluate(
            context_path=root / "context.json",
            evidence_path=root / "evidence.json",
            bundle_root=root / "policy-bundle",
            expected_digest=expected_digest or None,
            digest_source="cli_flag" if expected_digest else "absent",
            infrastructure_failure=spec.get("infrastructure_failure"),
            evaluated_at=PINNED_AT,
        )
        write_canonical(root / "expected-decision.json", result.decision)
        print(f"{name}: risk_class={result.decision['risk_class']} "
              f"reasons={result.decision['blocking_reasons']}")


if __name__ == "__main__":
    main()
