"""Shared input trees for the execution-identity tests.

`build_v030_baseline` is the T29 scenario. It writes v0.3.0 records only and
covers re-runs, superseded heads, digest drift, failed and budget-exhausted
occurrences, a partial copy, a synthetic PR and founder labels.
`tests/fixtures/qualification/v1-baseline-gate.json` holds what the
qualification-ledger/v1 consumer (faraday-review 8088813, unchanged at the
v0.4.0 re-vendor commit e9fb136) produced for it. The v2 consumer must
reproduce the gate, the counted PRs and the agreement block exactly.
"""

from __future__ import annotations

from pathlib import Path

from qualification_support import DIGEST_B, write_label, write_run

V03 = "v0.3.0"
BASELINE_SYNTHETIC = [{"repository": "faraday-build/pilot", "pr_number": 21, "reason": "seeded"}]


def build_v030_baseline(root: Path) -> None:
    for pr in range(1, 22):
        write_run(root, f"run-{pr}", pr=pr, head=f"h{pr}",
                  created_at=f"2026-11-01T10:{pr % 60:02d}:00Z", contract_version=V03)
    write_run(root, "rerun-3", pr=3, head="h3", created_at="2026-11-01T12:00:00Z",
              contract_version=V03)
    write_run(root, "push-4", pr=4, head="h4b", created_at="2026-11-02T09:00:00Z",
              contract_version=V03)
    write_run(root, "drift-5", pr=5, head="h5", digest=DIGEST_B,
              created_at="2026-11-02T09:00:00Z", contract_version=V03)
    write_run(root, "retry-6", pr=6, head="h6", created_at="2026-11-01T09:00:00Z",
              final_status="failed", failure_mode="runner_lost", contract_version=V03)
    write_run(root, "budget-8", pr=8, head="h8", created_at="2026-11-01T09:00:00Z",
              cls="block", reasons=("github_api_budget_exceeded",), contract_version=V03)
    write_run(root, "partial-7", pr=7, head="h7", created_at="2026-11-01T10:07:00Z",
              with_decision=False, contract_version=V03)
    write_run(root, "failed-head-9", pr=9, head="h9b", created_at="2026-11-03T09:00:00Z",
              final_status="failed", failure_mode="runner_lost", contract_version=V03)
    # Same content archived twice: one event, two copies.
    write_run(root, "copy-a/10", pr=10, head="h10", created_at="2026-11-01T10:10:00Z",
              contract_version=V03)
    write_label(root, "label-1", pr=1, head="h1", label="faraday/founder-green")
    write_label(root, "label-2", pr=2, head="h2", label="faraday/founder-red",
                labeled_at="2026-11-01T13:00:00Z")
    write_label(root, "label-x", pr=2, head="h2", node_id="U_other",
                login="tomrivera-PSE", label="faraday/founder-green")


_V2_COUNTED_FIELDS = ("repository", "pr_number", "head_sha", "review_id", "classifier_class",
                      "adjudicated")


def gate_view(report: dict) -> dict:
    """The parts of a readiness report that identity must never change,
    projected onto the v1/v2 shape: readiness/v3 adds per-PR qualification
    and a new gate criterion, and reports the v2 criterion as
    `gate.superseded_criterion`, which must equal the frozen baseline gate."""
    gate = report["gate"]
    return {
        "real_pr_count": report["real_pr_count"],
        "counted_prs": [{k: p[k] for k in _V2_COUNTED_FIELDS} for p in report["counted_prs"]],
        "synthetic_excluded": report["synthetic_excluded"],
        "adjudicated_count": report["adjudicated_count"],
        "agreement": report["agreement"],
        "escalation": report["escalation"],
        "gate": {"threshold": gate["threshold"],
                 "criterion": gate["superseded_criterion"]["criterion"],
                 "met": gate["superseded_criterion"]["met"]},
    }


def build_v1_prior_tree(root: Path) -> None:
    """T20/T26: inputs of `tests/fixtures/qualification/v1-prior-ledger.json`,
    a ledger written by the qualification-ledger/v1 consumer (e9fb136)."""
    write_run(root, "p1", pr=1, head="h1", created_at="2026-11-01T10:01:00Z", contract_version=V03)
    write_run(root, "p2", pr=2, head="h2", created_at="2026-11-01T10:02:00Z", contract_version=V03)
    write_label(root, "label-1", pr=1, head="h1", label="faraday/founder-green")
