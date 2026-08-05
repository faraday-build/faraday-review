# Deployment — Shadow Mode

## Prerequisites

- governance release with the `review-policy/` bundle: **v1.1.0**
- `FARADAY_POLICY_DIGEST` org/repo Actions variable set to the released
  digest: `sha256:32eeb3e07271fff267908bc428d77477caa403d9e0f8b0a0244d8c26aec73e12`
- agent-contracts **v0.3.0** (vendored; no runtime fetch)
- gitleaks available on the runner (installed by the workflow)

## Enroll a repository

1. Copy `workflow-templates/faraday-review.yml` from `tomrivera-ops/.github`
   into `.github/workflows/faraday-review.yml` (it calls the reusable
   workflow pinned to a faraday-review release SHA).
2. Set the `FARADAY_POLICY_DIGEST` variable (org-level preferred).
3. Create the four adjudication labels:
   `faraday/founder-green|yellow|red|block`.
4. Open a test PR; confirm one governed summary comment and the
   `faraday-review / governed-summary` check appear, and that nothing is
   approved or merged by the plane.

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
