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

After ≥20 real PRs, `faraday-review throughput` + the log must answer:

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
2. **Run artifacts are retained 90 days.** They are raw inputs, not the
   record of truth; their expiry loses nothing once step 3 has run.
3. The operator downloads the artifacts into one directory and runs
   `faraday-review aggregate` then `faraday-review readiness` (commands in
   `PHASE_2_INPUTS.md`). The resulting **ledger + readiness report are the
   durable record**: committed by PR under `docs/qualification/`, signed,
   through the required checks — well inside the 90-day window.
4. The ledger and readiness report record the sha256 of every input file,
   so a committed report is auditable against the artifacts it came from.
5. Committed ledgers and readiness reports are **never deleted during
   rollback** (see `ROLLBACK.md`); rollback removes observation, not the
   calibration record.

Adjudications are matched on the founder `node_id` (`U_kgDODk3msA`),
never login, at both `adjudicate` and `aggregate` time; rejected label
events are kept and counted but never applied. Synthetic/test PRs are
excluded only by listing them in `docs/qualification/synthetic-prs.json`
(`{repository, pr_number, reason}`), reviewed by PR like any other change;
repository names match case-insensitively and a case-variant duplicate
entry is rejected. Only completed runs whose freshness gates passed
(`stale_state: fresh`) count or select a PR's current head; stale-but-
completed runs are kept in the ledger as known-stale evidence and never
counted.
The real-PR gate threshold has a hard floor of 20; `readiness` exits 2 on
any lower value.

## Hard rules

- No bypassing protections; stop gates are diagnosed, never routed around.
- Every commit signed; every change lands by PR through required checks.
- `founder-pm` is never modified by this workstream.
- A Green classification is information, not authorization.
