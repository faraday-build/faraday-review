# Operating Model — Phase 1

## Roles

- **Governance** (`faraday-governance`) defines the policy bundle;
  releases are immutable tags; the digest is the contract.
- **Validator** (this repo) evaluates deterministically and emits
  RiskAssessment / MergeAuthorization / FounderEscalation. It authorizes
  nothing in Phase 1.
- **GitHub** enforces required checks and rulesets.
- **Founder** adjudicates every escalation and any decision they choose,
  via labels; matched on `node_id` `U_kgDODk3msA`.

## The founder loop

1. PR opens → shadow run posts the governed summary (risk class, reasons).
2. Founder reviews and merges (or not) exactly as before — the plane
   changes nothing about who can merge.
3. Founder optionally applies `faraday/founder-<class>` to record what
   the classification SHOULD have been for that head SHA.
4. Telemetry records `founder_classification`, `agreement`, latency.

## What telemetry answers (Phase 2 gate)

After ≥20 qualifying real PRs with founder adjudication (gate criterion in
`PHASE_2_INPUTS.md`, pending founder confirmation), `faraday-review
throughput` + the log must answer:

- which classifications the deterministic plane got wrong (per
  adjudication), and in which direction
- escalation volume and the founder's real throughput cost (median/p90)
- recurring Yellow causes that could be narrowed deterministically
- defect classes that provably require inference — the actual Phase 2 case

Those answers populate `PHASE_2_INPUTS.md`; the model phase is scoped
from that document, not from the Phase 1 handoff.

## Qualification telemetry and retention

Shadow runs execute on ephemeral runners, so no single runner holds the
cross-PR record. The durable path is:

1. Each shadow run uploads its artifacts dir (`telemetry.jsonl`,
   `decision.json`, and `adjudications.jsonl` when a label event ran
   `faraday-review adjudicate`) as a GitHub Actions artifact.
2. **Run artifacts are retained 90 days.** They are raw inputs. The
   ledger is a summary, not a re-ingestible sample store, so the raw
   inputs must outlive the artifacts.
3. Within the 90-day window the operator downloads each artifact and
   **archives its files by PR under `docs/qualification/artifacts/<artifact
   name>/`** (small JSONL/JSON files; never deleted, like the ledger).
4. The operator runs `faraday-review aggregate --input-dir
   docs/qualification/artifacts --prior-ledger <last committed ledger>`
   then `faraday-review readiness` (commands in `PHASE_2_INPUTS.md`). Every
   aggregation re-folds the **whole archive**, so samples from earlier
   retention windows are kept. `--prior-ledger` fails closed (exit 2) if
   any input the previous ledger recorded (matched by kind + sha256) is
   missing, so a later window can add samples but never silently drop
   them. The ledger + readiness report are committed by PR under
   `docs/qualification/`, signed, through the required checks.
   The ledger and readiness report record the sha256 of every input file,
   so a committed report is auditable against the archived artifacts.
5. Archived artifacts, committed ledgers and readiness reports are
   **never deleted during rollback** (see `ROLLBACK.md`); rollback removes observation, not the
   calibration record.

Adjudications are matched on the founder `node_id` (`U_kgDODk3msA`),
never login, at both `adjudicate` and `aggregate` time; rejected label
events are kept and counted but never applied. Synthetic/test PRs are
excluded only by listing them in `docs/qualification/synthetic-prs.json`
(`{repository, pr_number, reason}`), reviewed by PR like any other change;
repository names match case-insensitively and a case-variant duplicate
entry is rejected. Only completed occurrences whose freshness gates passed
(`stale_state: fresh`) count or select a PR's current head; stale-but-
completed runs and failed occurrences are kept in the ledger
(`occurrence_log`) as evidence and never counted. Infrastructure-health
counts are execution-level and kept separate from the qualification count.
Executions are identified by the v0.4.0 `execution` field and deduplicated
across runs. Pre-v0.4.0 records give bounded, not exact, tallies (see
`PHASE_2_INPUTS.md`). Ledgers are `qualification-ledger/v2`.
`--prior-ledger` also accepts a committed v1 ledger, because continuity is
checked on inputs (kind + sha256). v2 additionally records each
`run-binding.json` as a `run_binding` input, so a later window cannot change
or drop a binding that an earlier ledger recorded; a v1 prior ledger has no
such inputs and requires none.
Readiness reports are `qualification-readiness/v3`. The gate counts
adjudicated qualifying real PRs (`PHASE_2_INPUTS.md`, "Gate criterion"); its
threshold has a hard floor of 20, and `readiness` exits 2 on any lower value.

## Hard rules

- No bypassing protections; stop gates are diagnosed, never routed around.
- Every commit signed; every change lands by PR through required checks.
- `founder-pm` is never modified by this workstream.
- A Green classification is information, not authorization.
