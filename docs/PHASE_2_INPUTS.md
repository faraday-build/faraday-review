# Phase 2 Inputs

**Status: initialized empty.** Written last, populated only from shadow
telemetry and founder adjudications. Phase 2 scoping does not begin until
Phase 1 has run against at least 20 qualifying real PRs with founder
adjudication (criterion below, confirmed by the founder on 2026-11-14).

## Qualification status

This document remains the governing Phase 1 qualification record.

| Item | Value |
|---|---|
| Adjudicated qualifying real PRs (gate count) | **0** |
| Gate (≥ 20 adjudicated qualifying real PRs, hard floor; criterion founder-confirmed 2026-11-14) | **NOT met** |
| Qualifying real PRs | 0 |
| Real PRs counted (v2 definition, not the gate count) | 0 |
| Founder adjudications applied | 0 |
| Synthetic PRs excluded | 0 (`docs/qualification/synthetic-prs.json` is empty) |

No shadow telemetry has been aggregated yet; nothing above is estimated.

How the number is computed (no network, deterministic, explicit timestamp):

```bash
# 1. archive every enrolled repo's shadow-run artifacts (read-only API)
faraday-review collect --collected-at 2026-11-12T00:00:00Z
# 2a. FIRST ledger only (no docs/qualification/ledger.json committed yet):
#     fold the whole archive into one ledger, without --prior-ledger
faraday-review aggregate --input-dir docs/qualification/artifacts \
  --output docs/qualification/ledger.json --generated-at 2026-11-12T00:00:00Z
# 2b. every LATER update: the last committed ledger is the prior ledger, so
#     an input it recorded can never silently disappear (fails closed, exit 2)
faraday-review aggregate --input-dir docs/qualification/artifacts \
  --prior-ledger docs/qualification/ledger.json \
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
fields (`real_pr_count`, `escalation.*`) use only the counted run.

**Execution identity** (agent-contracts v0.4.0; normative rules in
`src/faraday_review/schemas/review-telemetry-identity.md`). Each shadow
run emits a fresh `event_id` and its GitHub `execution` (`run_id`,
`run_attempt`). Copies of an event are one occurrence: the key is the
`event_id`, or for a v0.3.0 record a content key over the full record. An
`event_id` claimed by different content is `event_id_conflict`. That
event is ineligible: it never counts, selects a head, verifies a digest or
enters an infrastructure tally. Two events claiming one execution are
`multiple_events_per_execution`, and the execution counts once. v0.3.0
records have no execution identity and none is synthesized:
`execution_count` is `null` with `execution_count_bounds`, and
`infrastructure_failures.status` is `bounded` with per-tally `bounds`.
The figure given is the lower bound (distinct events). Identity never
changes `real_pr_count`: it is still one counted run per real PR on its
current head. `review_id` (review subject),
`fingerprint` (input digest) and `retries` (in-run API retries) keep their
meaning; none of them is an execution identity. `gate.met` is
`adjudicated_qualifying_real_pr_count >= threshold` (see "Gate criterion"
below); the threshold cannot be configured below 20. Founder labels are recorded with
`faraday-review adjudicate` and attached only to the exact head SHA they
were applied to. The report also carries agreement (confusion matrix,
over/under-escalation), escalation volume, founder latency median/p90,
policy-digest / reviewer-version drift, stale-state and infrastructure
failure counts, anomalies, and the sha256 of every input file. Schemas:
`src/faraday_review/telemetry/schemas/`.

When the operator commits a ledger + readiness report, update the table
above from `readiness.json` in the same PR.

## Gate criterion (qualification-readiness/v4)

> **Confirmed by the founder on 2026-11-14** (GHO-AUTONOMOUS-PRODUCTION-006
> qualification policy ruling: D1–D10 and J1 accepted as documented). The
> founder's Phase 2 requirement is "at least 20 qualifying real PRs with
> founder adjudication". Up to v2 this repository implemented only "≥ 20 real
> PRs (hard floor)", which a probe met with 20 unadjudicated PRs that were all
> Blocked for `policy_digest_unpinned`. v3 implements the stricter reading
> below. It can only make the gate harder to meet; the floor stays 20 and can
> only be raised. `gate.criterion_status` is `founder_confirmed`.
>
> The same ruling approved three follow-ups, implemented in v4: metrics
> over eligible PRs with all-PR context reported separately (O1),
> escalation-accuracy reporting with a zero-missed-mandatory-escalation gate
> condition where an unknown denominator never passes (O2 = A + gate), and a
> check that committed reports expose no private repository identity (O3).
> Rollback continuity stays a manual check until the first real ledger
> qualifies (O4 = B). The 20-real-PR floor is unchanged.

```
gate.met = adjudicated_qualifying_real_pr_count >= threshold   (threshold >= 20)
           AND gate.escalation_condition.status == pass
```

- `real_pr_count` keeps its v2 definition (above) and stays in the report.
  The v2 criterion is reported as `gate.superseded_criterion`
  (`real_pr_count >= threshold`, `authoritative: false`), so nothing is
  hidden.
- `qualifying_real_pr_count`: real PRs whose current head has at least one
  **eligible occurrence**. Candidates are the completed, fresh,
  identity-eligible occurrences (`occurrence_log`) of the runs counted for
  `real_pr_count` on the current head. An occurrence is eligible when its
  run's policy digest is verified, its classifier class is known, and none
  of its blocking reasons is outside the `classification` category in the
  table below. Eligibility is judged per occurrence, because a collapsed
  run's representative can hide an earlier eligible occurrence of the same
  head (or the reverse).
- `adjudicated_qualifying_real_pr_count`: qualifying real PRs with a valid
  founder adjudication: an accepted record whose founder `node_id` is
  re-checked at `aggregate` time, attached to the exact head SHA of an
  eligible occurrence. An adjudication on a superseded head, on a
  non-eligible run, or by anyone else never counts. An adjudicated PR that
  does not qualify is reported as anomaly `adjudicated_pr_not_qualifying`.
- Synthetic PRs are excluded exactly as before.

Every real PR in the ledger is either in the gate count or in exactly one
`qualification.exclusions` kind, so
`adjudicated_qualifying_real_pr_count + Σ exclusions = qualification.real_prs_in_ledger`:

| Exclusion | Meaning |
|---|---|
| `incomplete` | No completed fresh run on a current head: failed-only, stale-only or identity-ineligible. Listed in `qualification.incomplete_prs`. |
| `unknown` | A state this consumer cannot interpret: no classifier class, a blocking reason not in the table below, or a blocking reason on a non-Block class (the Validator never emits that). Never counted. |
| `misconfigured` | Blocked by a configuration reason (table). |
| `refused` | Blocked by a refusal reason (table). |
| `infrastructure_blocked` | Blocked by an infrastructure reason (table). |
| `policy_digest_unverified` | The run's `decision.json` is absent or does not reproduce the fingerprint, so the policy digest it ran under is unproven. |
| `unadjudicated` | Qualifying, but no valid founder adjudication on an eligible head. |

A PR with no eligible occurrence is reported under the first kind it has
in the order `unknown`, `misconfigured`, `refused`, `infrastructure_blocked`,
`policy_digest_unverified` (`qualification.exclusion_precedence`); every
kind found is listed in its `counted_prs[].qualification.exclusion_reasons`.
The precedence affects only reporting, never the gate.

Blocking reasons. This is the Validator's complete vocabulary
(`src/faraday_review/governance/`); a test fails if the two ever differ.
The table is kept in code and here, not in the report, because outputs
never carry authorization field names.

| Reason | Category | Why |
|---|---|---|
| `secret_detected` | classification | The Validator classified the change; a real Block the founder can adjudicate. |
| `required_check_failing` | classification | Classified from the PR's observed required checks. |
| `required_check_missing` | classification | Classified from the PR's observed required checks. |
| `pr_too_large_for_deterministic_classification` | classification | A deterministic escalation outcome. These PRs are the Phase 2 inference case, so their adjudication is wanted calibration data. |
| `policy_digest_unpinned` | misconfigured | `FARADAY_POLICY_DIGEST` was not set; the run was not evaluated against a pinned policy. |
| `policy_digest_mismatch` | misconfigured | The bundle differs from the pinned digest; the run was not evaluated against the pinned policy. |
| `policy_missing` | misconfigured | No usable policy bundle; nothing was evaluated against policy. |
| `policy_version_unsupported` | misconfigured | The bundle's policy version is outside what this reviewer supports. |
| `contract_version_unsupported` | misconfigured | The evidence contract version is unsupported (Ruling 16), so the evidence was not evaluated. |
| `evidence_malformed` | refused | The Validator failed closed on evidence it could not read or bind to the context, instead of classifying the change. |
| `evidence_stale` | refused | The evidence is for a different head than the context, so the class is not a classification of the counted head. |
| `unknown_evidence_class` | refused | The evidence class is outside the accepted set; the Validator failed closed. |
| `github_api_budget_exceeded` | infrastructure_blocked | An infrastructure fault stopped evaluation (kept separate from `pr_too_large_*`). |

### Metric population (O1)

`agreement`, `escalation` and `founder_latency` are computed over the
adjudicated qualifying PRs (the gate population), each scored on its D10
occurrence: the latest eligible occurrence on the adjudicated entry.
`escalation.pr_too_large_escalations` and
`escalation.api_budget_exceeded_escalations` stay separate counts (the
latter is 0 by construction in this population, because a budget Block is
`infrastructure_blocked`). The previous figures stay visible as context and
never feed the gate:

- `context_all_adjudicated`: `adjudicated_count`, `agreement` and
  `founder_latency` over every adjudicated counted PR, qualifying or not
  (the v3 definitions).
- `context_all_real_prs.escalation`: escalation volume over every counted
  real PR's representative run (the v3 definition).

### Escalation accuracy and the escalation condition (O2)

`escalation_accuracy` covers the same population. Per PR:

- **truth**: the founder's adjudicated class requires founder review under
  the policy digest the scored occurrence was evaluated under. The mapping
  comes from the governance-pinned table
  `governance/escalation.py:RELEASED_REVIEW_REQUIREMENTS` (today, for the
  released digest: green no; yellow, red and block yes). A test derives the
  table from the released bundle and fails if they differ. A digest with no
  mapping makes the PR `unknown`.
- **prediction**: the scored occurrence's recorded escalation (`escalated`
  in the ledger's `occurrence_log`, copied from telemetry). A ledger written
  before v4 lacks it, which also makes the PR `unknown`.

| | prediction: escalated | prediction: not escalated |
|---|---|---|
| **truth: review required** | `true_escalation` | `missed_escalation` |
| **truth: not required** | `unnecessary_escalation` | `true_non_escalation` |

Reported: the four cells (`matrix`), `mandatory_escalations`
(true + missed), `missed_escalations` with `missed_escalation_prs`,
`unknown` with `unknown_prs`, `escalation_recall` (true / mandatory),
`escalation_precision` (true / escalated) and `unnecessary_escalation_rate`
(unnecessary / not required). A rate is `null`, never 0, when its
denominator is 0. Precision and the unnecessary rate have no threshold.

`gate.escalation_condition` = `{status, missed, mandatory_total, unknown}`:

- `fail` when `missed > 0`: a PR the founder had to see would have bypassed
  review;
- `unknown` when `unknown > 0` or `mandatory_total == 0`: an unknown or zero
  denominator never passes;
- `pass` otherwise.

`gate.met` requires `pass`. The condition can only make the gate stricter.

### Public repository identities (O3)

Committed qualification reports never name a repository that is not
enrolled as public:

- `tools/guards/public_identities.py` (run by `tests/guards`, so by the
  `guard-self-tests` check) fails when `ledger.json`, `readiness.json` or any
  `collection-manifest-*.json` under `docs/qualification/` names a
  repository that `docs/qualification/enrollment.json` does not declare
  `public`. A missing or unreadable file fails closed.
- `aggregate` and `readiness` refuse (exit 2) to write such a report when
  `--output` is inside a git checkout, using `--enrollment` (default: the
  checkout's `docs/qualification/enrollment.json`). Outside a checkout (the
  private side) nothing is checked.
- Neither prints the identity it refuses. Identities are read from the
  `repository`, `caller_repo` and `repository_spellings` fields, and from
  archive provenance paths (`path`, `source`, `sources`): `collect` lays the
  archive out as `<owner>/<repo>/<run>/...`, so a path names its repository
  by its first two components. A path whose repository cannot be
  established (other than a root `collection-manifest-*.json`) fails closed.
- An occurrence whose telemetry omits the escalation field has no recorded
  `escalated` in the ledger; its O2 prediction is `unknown`, never false.

Known limits, unchanged by v3: "policy digest verified" means the digest is
consistent with the run's own `decision.json` and, with no
`policy_digest_unpinned` / `policy_digest_mismatch` reason, that the run was
evaluated against the pin it was given; readiness does not compare it with a
release digest (drift is reported in `drift.policy_bundle_digests`).
Since v4, `agreement`, `escalation` and `founder_latency` cover the gate
population (O1, above).

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
