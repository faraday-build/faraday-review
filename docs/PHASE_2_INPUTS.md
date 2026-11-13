# Phase 2 Inputs

**Status: initialized empty.** Written last, populated only from shadow
telemetry and founder adjudications. Phase 2 scoping does not begin until
Phase 1 has run against at least 20 real PRs.

## Qualification status

This document remains the governing Phase 1 qualification record.

| Item | Value |
|---|---|
| Real PRs counted | **0** |
| Gate (≥ 20 real PRs, hard floor) | **NOT met** |
| Founder adjudications applied | 0 |
| Synthetic PRs excluded | 0 (`docs/qualification/synthetic-prs.json` is empty) |

No shadow telemetry has been aggregated yet; nothing above is estimated.

How the number is computed (no network, deterministic, explicit timestamp):

```bash
# 1. download every shadow-run artifact into one directory, e.g. qual-in/
#    (gh run download … — one sub-directory per run is fine)
# 2. fold them into one ledger
faraday-review aggregate --input-dir qual-in/ \
  --output docs/qualification/ledger.json --generated-at 2026-11-12T00:00:00Z
# 3. derive the gate report
faraday-review readiness --ledger docs/qualification/ledger.json \
  --synthetic docs/qualification/synthetic-prs.json \
  --generated-at 2026-11-12T00:00:00Z \
  --output docs/qualification/readiness.json
```

`real_pr_count` = distinct (repository, PR) with at least one completed
**fresh** shadow run (`stale_state: fresh`) on its **current** head,
excluding every PR listed in `synthetic-prs.json`. The current head is the
head whose newest **completed and fresh** occurrence is newest; a run whose
freshness gate saw the head move (`stale_before_validation` /
`stale_before_publication`) never makes its head current and never counts,
even though it completed, and a failed occurrence never makes its head
current either. A PR with no completed-and-fresh occurrence has no current
head and does not count (anomaly `no_fresh_run_for_pr` when every
occurrence was stale, `no_eligible_run_for_pr` when fresh occurrences exist
but none completed). Earlier heads stay in the ledger flagged
`superseded`. Repository identity is case-insensitive: `owner/repo` is
casefolded for every comparison (correlation, adjudication matching,
synthetic exclusion, counting); original spellings are kept in
`repository_spellings`. Identical events (same `review_id` + run
fingerprint) are counted once for qualification. Each distinct execution
occurrence (a failed attempt, a retry, a budget-exhausted run) is kept in
the run's `occurrence_log`; copies of the same event, with or without
`decision.json`, are one occurrence. Any verified copy proves the run's
policy digest (the fingerprint binds it), so a partial copy never erases
it; a `decision.json` that fails to verify beside a verified copy is
flagged `policy_digest_evidence_conflict`, and two different verified
digests are flagged `policy_digest_conflict` and fail closed to unverified.
Infrastructure health (`drift.stale_state_counts`,
`drift.infrastructure_failures`) counts occurrences, so a successful retry
never erases an earlier failure or budget exhaustion; the qualification
fields (`real_pr_count`, `escalation.*`) use only the counted run. `gate.met` is `real_pr_count >= 20`; the
threshold cannot be configured below 20. Founder labels are recorded with
`faraday-review adjudicate` and attached only to the exact head SHA they
were applied to. The report also carries agreement (confusion matrix,
over/under-escalation), escalation volume, founder latency median/p90,
policy-digest / reviewer-version drift, stale-state and infrastructure
failure counts, anomalies, and the sha256 of every input file. Schemas:
`src/faraday_review/telemetry/schemas/`.

When the operator commits a ledger + readiness report, update the table
above from `readiness.json` in the same PR.

## Miss patterns (deterministic classification vs founder adjudication)

_None recorded yet._

## Escalation volume and founder throughput cost

_None recorded yet._

## Recurring Yellow causes (candidates for deterministic narrowing)

_None recorded yet._

## Defect classes that provably require inference

_None recorded yet._

## Escalation-type separation check

`pr_too_large_for_deterministic_classification` vs
`github_api_budget_exceeded` counts — must be reported separately;
budget exhaustions are infrastructure faults and must trend to zero.

_None recorded yet._
