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

## Hard rules

- No bypassing protections; stop gates are diagnosed, never routed around.
- Every commit signed; every change lands by PR through required checks.
- `founder-pm` is never modified by this workstream.
- A Green classification is information, not authorization.
