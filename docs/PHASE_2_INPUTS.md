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
head whose newest fresh run is newest; a run whose freshness gate saw the
head move (`stale_before_validation` / `stale_before_publication`) never
makes its head current and never counts, even though it completed. A PR
with no fresh run has no current head and does not count (anomaly
`no_fresh_run_for_pr`). Earlier heads stay in the ledger flagged
`superseded`. Repository identity is case-insensitive: `owner/repo` is
casefolded for every comparison (correlation, adjudication matching,
synthetic exclusion, counting); original spellings are kept in
`repository_spellings`. Identical events (same `review_id` + run
fingerprint) are counted once. `gate.met` is `real_pr_count >= 20`; the
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
