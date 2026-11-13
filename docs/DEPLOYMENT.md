# Deployment — Shadow Mode

## Prerequisites

- governance release with the `review-policy/` bundle: **v1.1.0**
- `FARADAY_POLICY_DIGEST` org/repo Actions variable set to the released
  digest: `sha256:32eeb3e07271fff267908bc428d77477caa403d9e0f8b0a0244d8c26aec73e12`
- agent-contracts **v0.3.0** (vendored; no runtime fetch)
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
4. Open a test PR; confirm one governed summary comment and the
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

## Shadow pilot constraints (Phase 1.14)

One low-risk repository only: docs- or test-focused, no production
secrets, no customer data, no money movement, no deployment authority.
Approval and merge stay disabled regardless of risk class.

## Secrets and variables

| Name | Kind | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | ambient Actions token | collection + publication (permissions in GITHUB_PERMISSIONS.md) |
| `FARADAY_POLICY_DIGEST` | Actions variable | pinned policy digest; absence ⇒ every run Blocks `policy_digest_unpinned` |

No other secrets. The plane never handles deploy keys, cloud credentials,
or model API keys (no model in Phase 1).
