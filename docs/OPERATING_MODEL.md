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
2. **Run artifacts are retained 90 days.** They are raw inputs. The
   ledger is a summary, not a re-ingestible sample store, so the raw
   inputs must outlive the artifacts.
3. Within the 90-day window the operator runs **`faraday-review collect
   --collected-at <now>`** (read-only API; token from `$GITHUB_TOKEN`; the
   token needs `actions: read` on the callers). For each repository in
   `docs/qualification/enrollment.json` it lists the caller workflow's runs
   and their artifacts, downloads each unexpired
   `faraday-review-pr<N>-<head>-a<attempt>` artifact, and verifies its
   digest (or size). It extracts **only** `telemetry.jsonl`, `decision.json`,
   `adjudications.jsonl` and `run-binding.json` to
   `docs/qualification/artifacts/<owner>/<repo>/<run id>/<artifact name>/`.
   `pr-context.json`, `gitleaks-report.json` and the review evidence never
   leave the zip. An existing file is never overwritten with different
   bytes: collect fails closed and writes nothing. Each invocation writes
   one immutable `collection-manifest-<stamp>.json` at the archive root. It
   records, per run, the id, attempt, status and conclusion, and per
   artifact the id, digest, expiry and archived files with sha256. The files
   are small JSONL/JSON and are never deleted, like the ledger.
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
   Aggregate reads the manifests as inputs (kind `collection_manifest`,
   held by `--prior-ledger` continuity) and reports evidence that is
   expected but absent:
   - `run_without_telemetry`: a completed, non-skipped run attempt with no
     archived telemetry (failed, cancelled, timed out, or no artifact);
   - `artifact_expired_unarchived`: the attempt's artifact expired before
     it was archived;
   - `enrolled_repo_without_runs`;
   - `collection_file_missing`: a manifest names a file the archive no
     longer holds;
   - `invalid_collection_manifest`.

   CI job `qualification-reproducible` re-runs aggregate (and readiness,
   when committed) over the committed archive and byte-compares them with
   the committed `ledger.json` / `readiness.json`. It uses the base
   branch's ledger as `--prior-ledger`, and passes vacuously while no
   ledger is committed. It is not a required check. Making it one is a
   separate ruleset decision.
5. Archived artifacts, committed ledgers and readiness reports are
   **never deleted during rollback** (see `ROLLBACK.md`); rollback removes observation, not the
   calibration record.

### Private caller repositories (founder decision P8)

This repository is public. A private caller's archive would publish its
repository name, PR numbers, SHAs, risk classes and blocking reasons, so
`collect` never writes private metadata here:

- A repository counts as public only if `enrollment.json` declares it
  public **and** the live API reports `visibility: public`. Anything else,
  including a mismatch, is private.
- If an enrolled repository is private, collect refuses before any run or
  artifact request, unless `--private-archive-dir` names a directory
  **outside** this repository. Private files and their manifest then go
  only there. The public archive and manifest never mention the private
  repository.
- The committed `enrollment.json` lists public repositories only, and a
  test enforces that. Private entries belong in an enrollment file kept
  outside this repository (`--enrollment <path>`).
- Where the private archive lives (for example a private
  `faraday-build/faraday-review-qualification` repository, with only counts
  and hashes published here) is **a founder decision and is not created
  by this code**. Until it is made, enroll public repositories only.

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
The real-PR gate threshold has a hard floor of 20; `readiness` exits 2 on
any lower value.

### Failed runs leave telemetry

A shadow run that ends early still writes exactly one v0.4.0 record with
`final_status: failed`, a fresh `event_id`, the platform `execution` and a
`failure_mode`, then exits 2 as before. The contract fixes only the
`failure_mode` pattern; the producer emits:

| `failure_mode` | Cause |
|---|---|
| `github_api_budget_exceeded` | API budget exhausted (counts in `api_budget_exceeded_runs`) |
| `github_api_failure`, `configuration_invalid`, `publication_failure`, … | the `reason` of the typed `FaradayReviewError` |
| `gitleaks_error` | gitleaks exited outside {0, 2}, was missing, or timed out |
| `github_token_missing` | no token in the configured env var |
| `internal_error` | any other exception |
| `workflow_step_failed`, `workflow_step_cancelled` | the review step died without writing telemetry; recorded by the `Record failure telemetry` workflow step (`faraday-review record-failure`, no network, a no-op when telemetry exists) |

Authorization fields are copied verbatim from `decision.json` when the
Validator ran. Otherwise they are the fail-closed posture from
`governance.authorization.failed_run_posture()` (approval and merge
`false`, `founder_review_required: true`, no `risk_class`). Head and base
come from the decision, then the collected context, then the event payload
(`--head-sha`/`--base-sha`, used only for this record). With none of them
no schema-valid record exists, and the run writes nothing but still exits 2.
`stale_state` is omitted when no freshness gate ran. Failures before the
reviewer is installed (binding, checkout, install) cannot run reviewer code
and leave only `run-binding.json`.

## Hard rules

- No bypassing protections; stop gates are diagnosed, never routed around.
- Every commit signed; every change lands by PR through required checks.
- `founder-pm` is never modified by this workstream.
- A Green classification is information, not authorization.
