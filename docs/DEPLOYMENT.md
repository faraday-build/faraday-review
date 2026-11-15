# Deployment — Shadow Mode

## Prerequisites

- governance release with the `review-policy/` bundle: **v1.1.0**
- `FARADAY_POLICY_DIGEST` org/repo Actions variable set to the released
  digest: `sha256:32eeb3e07271fff267908bc428d77477caa403d9e0f8b0a0244d8c26aec73e12`
- agent-contracts **v0.4.0** (vendored; no runtime fetch)
- gitleaks available on the runner (installed by the workflow)

## Reusable workflow location

The reusable workflow lives in **this repository** at
`.github/workflows/shadow.yml` (`on: workflow_call`). This supersedes the
earlier plan (DISCOVERY.md §10) of a `reusable-faraday-review.yml` plus
`workflow-templates/` in `tomrivera-ops/.github`: keeping it here means one
commit SHA pins both the workflow and the reviewer code, the file is covered
by this repo's ruleset `20418235` (required checks, no bypass actors), and it
stays out of the repository that hosts the legacy `cli-approve-merge.yml`.
The repository is public, so callers in any organization can use it.

## Enroll a repository

1. Add `.github/workflows/faraday-review.yml` to the caller:

   ```yaml
   name: faraday-review
   on:
     pull_request:
       types: [opened, synchronize, reopened, ready_for_review]
   permissions:
     contents: read
     pull-requests: write
     checks: write
     actions: read
   concurrency:
     group: faraday-review-${{ github.event.pull_request.number }}
     cancel-in-progress: true
   jobs:
     faraday-review:
       uses: faraday-build/faraday-review/.github/workflows/shadow.yml@<REVIEWER_SHA>
       with:
         reviewer_sha: <REVIEWER_SHA>          # must equal the @ref above
         governance_sha: 1b98ab9b4aa13a0d481ef47972de62c3782e9f2a  # faraday-governance v1.1.0
         policy_digest: ${{ vars.FARADAY_POLICY_DIGEST }}
         required_checks: |
           <required check context>
   ```

   Rules: `<REVIEWER_SHA>` is a full commit SHA on faraday-review `main`
   (the workflow refuses anything else); never `pull_request_target`; never
   `secrets:` or `secrets: inherit` (none are declared); keep the check
   **non-required** during Phase 1 qualification.
2. Set the `FARADAY_POLICY_DIGEST` variable (org-level preferred). An unset
   or empty variable makes every run Block `policy_digest_unpinned`.
3. Create the four adjudication labels:
   `faraday/founder-green|yellow|red|block`.
4. Run the read-only preflight and resolve every gap it lists:

   ```bash
   GITHUB_TOKEN=<read-only token> faraday-review preflight --repo <owner>/<repo>
   ```

   It exits 0 only when all of these hold:
   - `FARADAY_POLICY_DIGEST` resolves for the repository (repository
     variable, or an organization variable visible to it) and equals the
     digest above;
   - the four labels exist;
   - the caller pins `shadow.yml` by a full SHA on faraday-review `main`,
     with the same `reviewer_sha`.

   It also reports `FARADAY_REVIEW_SHADOW_ENABLED`. A value the token
   cannot read is reported as `unknown`, which never passes. The command
   only reads.
5. Add the repository to `docs/qualification/enrollment.json` (public
   repositories only; see OPERATING_MODEL.md, "Private caller
   repositories").
6. Open a test PR; confirm one governed summary comment and the
   `faraday-review / governed-summary` check appear, and that nothing is
   approved or merged by the plane. Record the test PR as synthetic so it
   never counts toward the 20-real-PR gate.

## What the reusable workflow enforces

- Event must be `pull_request`; inputs must be full 40-hex SHAs.
- The caller run's `referenced_workflows` must show `shadow.yml` pinned at
  exactly `reviewer_sha`, and `reviewer_sha` must be reachable from
  faraday-review `main` — verified before any reviewer code is checked out.
- Reviewer code and the policy bundle are checked out at their pinned SHAs;
  no checkout persists credentials.
- gitleaks is downloaded at a pinned version and checksum-verified.
- Fork PRs are evaluated with `--skip-publish` (read-only token, no
  elevation).
- `decision.json`, `merge-authorization.json`, `telemetry.jsonl` and
  `run-binding.json` are uploaded as artifact
  `faraday-review-pr<N>-<head sha>-a<attempt>`, retained 90 days, for the
  qualification ledger.
- Required checks are evaluated only once final (S6 F-1). The job starts on
  the same event as the caller's required checks, so `shadow-run` polls
  them (10 s doubling to 60 s): a failure ends the wait at once; a context
  with no run after `required_checks_missing_grace` (default 300 s, max
  900 s) is `required_check_missing`; a context still running at
  `required_checks_pending_timeout` (default 900 s, max 1800 s) ends the run
  **failed** (`required_checks_pending_timeout`): one failed telemetry
  record, no decision, never a Block. A head that moves while waiting makes
  the run stale; a poll that would starve the rest of the run's API budget
  fails as `github_api_budget_exceeded`. The job timeout (40 min) covers
  the longest permitted wait.
- Every attempt (completed, failed or cancelled) also publishes one neutral
  `faraday-review / attempt-record` check run on the head, `external_id`
  `faraday-review-attempt:<run id>:<attempt>`, carrying that attempt's
  `telemetry.jsonl`, `decision.json` and `run-binding.json` verbatim with
  their sha256 (S6 F-2). GitHub deletes earlier-attempt artifacts on re-run;
  check runs survive. `collect` reads a record back only for an attempt
  whose artifact telemetry is gone, verifies it (GitHub Actions app, head,
  run id and attempt, repository, PR, schema, every sha256) and archives it
  under `<run id>/attempt-record-a<attempt>/` with provenance in the
  manifest; a record that fails any check is listed as rejected and
  aggregation reports `attempt_record_rejected`. Fork PRs skip it.

## Qualification monitor (scheduled, public callers only)

`.github/workflows/qualification-monitor.yml` is a second reusable workflow:
a read-only daily status view of the caller's own E1 qualification record.
It runs `collect` → `aggregate` → `readiness` (fresh, no prior ledger) over
the caller's unexpired shadow-run artifacts, then `faraday-review
monitor-report`, writes the Markdown to the job summary, and keeps **one**
issue in the caller repository, *Faraday Review qualification status*
(label `faraday-review-qualification`, author `github-actions[bot]`), whose
body is replaced in place. A comment is added only when the checkpoint level
rises (CP-5/10/15/20), a new anomaly appears (a kind's count grows), or a
CRITICAL authorization anomaly appears; the last reported state is kept in a
hidden marker in the body. It never approves, merges or labels a pull
request, writes no repository contents, and touches no variable or secret.

Caller (pin both SHAs to the same faraday-review main commit):

```yaml
name: faraday-review-qualification-monitor
on:
  schedule:
    - cron: "0 7 * * *"   # daily, after the 06:00 UTC Drift Gate
  workflow_dispatch:
permissions:
  contents: read
  actions: read
  checks: read
  pull-requests: read
  issues: write
jobs:
  monitor:
    if: vars.FARADAY_REVIEW_MONITOR_ENABLED == 'true'
    uses: faraday-build/faraday-review/.github/workflows/qualification-monitor.yml@<40-hex SHA>
    with:
      reviewer_sha: <same 40-hex SHA>
```

- **Fail-closed pre-check.** Before any run or artifact request the
  workflow verifies the caller's reviewer binding (as `shadow.yml` does),
  that the live API reports the caller public, and that the reviewer's
  committed `docs/qualification/enrollment.json` at the pinned SHA enrolls
  it as public. Only that one committed entry, and the caller's own entries
  of the committed `synthetic-prs.json`, are used. `monitor-report` refuses
  (exit 2, nothing written) if any input names another repository.
- **Permissions.** Job `monitor`: `contents`, `actions`, `checks`,
  `pull-requests` read. Job `status-issue`: `contents: read`, `issues:
  write`, nothing else (the only `issues: write` the hardening guard
  admits). It creates the label definition once, if absent.
- **Cadence and cost.** Recommended daily at 07:00 UTC plus
  `workflow_dispatch`: two short jobs per day. API use is bounded by
  `max_requests` (default 2000, max 5000) for `collect`; the issue job makes
  at most six requests.
- **Failure notification.** CRITICAL exits non-zero, so GitHub's failed-run
  notification fires, and the issue gets a comment carrying the kill-switch
  command. Any other failure (binding, visibility, enrollment, collection,
  refusal) fails the run before the issue is touched.
- **Kill switches.** Monitor: `gh variable delete
  FARADAY_REVIEW_MONITOR_ENABLED -R <caller>`. On CRITICAL (an archived
  decision, telemetry or attempt record with `approval_authorized` or
  `merge_authorized` not false, or a risk/authorization field outside its
  expected values): `gh variable delete FARADAY_REVIEW_SHADOW_ENABLED -R
  <caller>` at once, preserve the run artifact, report.
- **Rollback.** Delete the caller workflow (or unset the variable), or
  revert this workflow by PR. The status issue may be closed by hand;
  nothing else is written. Archived artifacts and ledgers are never deleted.
- **Privacy boundary.** Public callers only. A private caller must not use
  this workflow until founder decision P8 (private archive location); its
  record would land in a public CI log and job summary.

## Shadow pilot constraints (Phase 1.14)

One low-risk repository only: docs- or test-focused, no production
secrets, no customer data, no money movement, no deployment authority.
Approval and merge stay disabled regardless of risk class.

## Secrets and variables

| Name | Kind | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | ambient Actions token | collection + publication (permissions in GITHUB_PERMISSIONS.md) |
| `FARADAY_POLICY_DIGEST` | Actions variable | pinned policy digest; absence ⇒ every run Blocks `policy_digest_unpinned` |
| `FARADAY_REVIEW_MONITOR_ENABLED` | Actions variable (caller) | `true` enables the qualification monitor caller; delete it to stop the monitor |

No other secrets. The plane never handles deploy keys, cloud credentials,
or model API keys (no model in Phase 1).
