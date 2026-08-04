# Discovery — Faraday Review Phase 1

Read-only inspection performed 2026-08-04 via the GitHub API. Nothing was
cloned or modified in any inspected repository. Reduced Phase 1 scope: Codex
review shape and Adversary integration are deferred to Phase 2.

Repos inspected:

- `tomrivera-ops/agent-contracts` (private)
- `tomrivera-ops/faraday-validator` (public)
- `tomrivera-ops/.github` (public)
- `faraday-capital-systems/faraday-governance` (public)
- `faraday-capital-systems/faraday-architecture-registry` (private)

---

## 1. Existing schemas and JSON Schema draft

`tomrivera-ops/agent-contracts` is the canonical contract source
("Canonical JSON Schemas defining the contracts all Faraday ecosystem repos
validate against"). All 19 schemas declare exactly
`"$schema": "https://json-schema.org/draft/2020-12/schema"` — zero
exceptions. `$id` convention: `https://faraday-lab.dev/schemas/<name>.schema.json`.

### additionalProperties

Every object-typed schema sets `additionalProperties: false` at top level.
The four schemas without the key (`risk-class`, `agent-role`,
`human-principal`, `work-session-state`) are `"type": "string"` enum
taxonomies where it does not apply. The closed-contracts convention holds
repo-wide, and Phase 1 additions must follow it.

### Contract existence matrix

| Contract | Status | Location |
|---|---|---|
| ReviewEvidence | EXISTS | `schemas/review-evidence.schema.json` |
| ApprovalDecision | EXISTS | `schemas/approval-decision.schema.json` |
| RiskClass | EXISTS | `schemas/risk-class.schema.json` (string enum: `docs`, `standard`, `elevated`, `critical`) |
| SeverityClass | EXISTS | `schemas/severity-class.schema.json` |
| AgentIdentity | EXISTS | `schemas/agent-identity.schema.json` |
| GovernanceEvent | EXISTS | `schemas/governance-event.schema.json` |
| ToolPermission | EXISTS (deferred to Phase 2 anyway) | `schemas/tool-permission.schema.json` |
| PullRequestContext | MISSING — Phase 1.5 adds | — |
| DeterministicFinding | MISSING — Phase 1.5 adds | — |
| EvidenceClass | MISSING — Phase 1.5 adds (enum complete at `deterministic`, `mixed`) | — |
| RiskAssessment | MISSING — Phase 1.5 adds | — |
| MergeAuthorization | MISSING — Phase 1.5 adds | — |
| FounderEscalation | MISSING — Phase 1.5 adds | — |
| ReviewTelemetryEvent | MISSING — Phase 1.5 adds | — |
| ReviewFinding | MISSING — deferred to Phase 2 | — |
| ReviewProviderResult | MISSING — deferred to Phase 2 | — |

Consequence: Phase 1.5 is an **additive minor release of agent-contracts**
(v0.3.0 under its SemVer convention), landing the seven Phase 1 contracts
with `reviewer_version`, `evidence_class`, `deterministic_findings`,
`provider_results`, `required_providers` as **optional** fields (Ruling 16).
`ReviewFinding`, `ReviewProviderResult` are not created in Phase 1.

### agent-contracts release convention

SemVer `vMAJOR.MINOR.PATCH` annotated tags + GitHub Releases with immutable
append-only assets: `agent-contracts-schema-bundle.tar.gz`,
`.manifest.json` (per-file sha256 + size, `contract_version` asserted equal
to tag, `source_commit` asserted equal to workflow SHA, fail-closed),
`.sha256`. Verification tooling exists (`scripts/verify-bundle.sh`).
Released: `v0.1.0`, `v0.2.0` (latest; tag object `1b6b56a0…` → commit
`d637b4d5…` = current main HEAD).

---

## 2. Governance release state (digest pin)

- `faraday-capital-systems/faraday-governance` **is tagged**: exactly one
  tag, `v1.0.0` → `ac753335b7aac4f9ba8b79067338dab38448cd98`, matching the
  pinned governance contract. Bundle hash method per `VERSIONING.md`:
  `git archive <tag> | sha256sum`; known-good v1.0.0 digest
  `b086ae5d06f8e0d3211769d7efdcc66f4445b2edffa9789c0896386f421fdc5d`,
  re-verified weekly by `governance-health-check.yml` (hard-fail on drift).
- **The six-file Phase 1 policy bundle does not exist.** None of
  `approval-policy.yaml`, `protected-paths.yaml`,
  `evidence-requirements.yaml`, `provider-requirements.yaml`,
  `policy.schema.json` are present. Phase 1.6 therefore requires a
  governance PR adding the bundle and a **new governance release tag**
  whose bundle digest (computed with `canonical.bundle_digest`) becomes
  `FARADAY_POLICY_DIGEST`. The v* tag ruleset (17866918) makes prior tags
  immutable; the new tag is additive and does not disturb v1.0.0.
- Governance releases publish no GitHub Release objects; the tag is the
  release. The Phase 1 pin verifies the **bundle-directory digest**, which
  is stable under CRLF checkouts and formatting-only differences by
  construction, unlike whole-tree `git archive` hashing.

---

## 3. faraday-validator: the named engine does not exist

`tomrivera-ops/faraday-validator` is a TypeScript Supabase **deployment**
smoke-checker (auth/RLS/schema checks against a live Supabase instance,
single profile `fso`, only subcommand `validate <profile>`). It has no
`evaluate-pr`, no PR-related code, no tags, no releases, no CI, and is
dormant since 2026-05-06. It shares a name and nothing else with the
deterministic PR validator this phase requires — the same divergent-domain
pattern already recorded for shield-ai's audit-event schema.

**Decision recorded:** the deterministic validator is implemented in
`faraday-build/faraday-review` (`src/faraday_review/governance/` +
`cli.py`), exposing the `faraday-validator` console entry point with the
`evaluate-pr` subcommand as specified in Phase 1.7.
`tomrivera-ops/faraday-validator` is not modified, not extended, and not
pinned. If the founder later wants the name reassigned or the TS repo
renamed, that is a separate ruling.

---

## 4. CI conventions and reusable workflow patterns

- **No reusable workflows exist anywhere** in `tomrivera-ops/.github`
  (no `workflow_call` consumers/producers) and **no `workflow-templates/`
  directory exists** (API 404). Phase 1.10 creates both from scratch.
- Existing org convention: actions pinned by **major version tag only**
  (`@v4`, `@v5`) — never SHA-pinned; **no `concurrency` groups anywhere**;
  `permissions:` blocks only in agent-contracts' two workflows
  (`ci.yml`: `contents: read`; `release.yml`: `contents: write`).
  faraday-governance and .github workflows run with default token
  permissions.
- This repo's `workflow_hardening.py` guard is intentionally **stricter
  than current org practice** (40-char SHA pins, mandatory permissions
  blocks, timeouts, concurrency). Workflows landed by Phase 1 satisfy the
  guard; retrofitting other repos is out of scope.
- agent-contracts CI is deterministic by policy ("No AI invocation
  anywhere") — same posture as this repo.

---

## 5. GitHub permission patterns

- agent-contracts: least-privilege (`contents: read` CI /
  `contents: write` release).
- faraday-governance `validate.yml` and health check: no permissions block
  (default token).
- `tomrivera-ops/.github/.github/workflows/cli-approve-merge.yml`: **no
  permissions block**, `workflow_dispatch` with free-text owner/repo/PR
  inputs, mints an org-wide GitHub App token (`FARADAY_APP_ID` /
  `FARADAY_APP_PRIVATE_KEY`) and **approves and merges arbitrary PRs**.
  See Risks.
- Phase 1 shadow workflow ceiling (enforced by the guard):
  `contents: read`, `pull-requests: write`, `checks: write`,
  `actions: read`.

---

## 6. Branch protection and rulesets

| Repo | Protection |
|---|---|
| agent-contracts | **None enforceable** — private on Free plan; rulesets and classic protection both 403. The canonical contract source is the least-protected repo in the estate. |
| faraday-validator | Classic: 1 review, enforce_admins, no required checks, no signatures. |
| .github | Classic: 1 review, dismiss stale, enforce_admins, no signatures. |
| faraday-governance | Rulesets `17871609` (main: signatures, PR, required check `validate` strict, no bypass) + `17866918` (v* tags immutable). |
| architecture-registry | None enforceable (private, Free plan) — 403. |
| **faraday-build/faraday-review (this repo)** | Made **public** by founder ruling 2026-08-04 (Free-plan private = no enforcement, verified 403 on both mechanisms). Ruleset `20418235`: PR required, required checks `authorization-boundary` / `workflow-hardening` / `guard-self-tests` (strict), no bypass actors, deletion + force-push blocked. |

## 7. Founder identity (adjudication matching)

- Founder immutable account `node_id`: **`U_kgDODk3msA`**
  (login `tomrivera-PSE`, id `239986352`, type `User`). The authed gh
  identity is the same account. Adjudication matches on this `node_id`,
  never on login.
- Known non-founder actors seen in history: `faraday-ci-bot[bot]`
  (`BOT_kgDOEeALbA`) and GitHub's `web-flow` merge committer
  (`MDQ6VXNlcjE5ODY0NDQ3`). Both must be rejected by adjudication and
  logged as `adjudication_rejected`.

## 8. Secret scanning availability

GitHub secret scanning (and push protection) is **disabled on all three
public repos** inspected and unreadable (null) on the two private ones.
Consequence for Correction 3: the GitHub secret-scanning-alerts path is
currently a no-op estate-wide; **gitleaks over the diff range is the
primary detector in practice**, not the fallback. Recommendation (not
executed — outside Phase 1 scope): enable secret scanning + push
protection on public repos; it is free.

## 9. Contradictions and risks

1. **P0 — `cli-approve-merge.yml` contradicts ADR-0012 and the authority
   invariant.** ADR-0012 (agent-contracts, Accepted 2026-07-11) revokes
   all bot/App approve+merge authority org-wide and ratifies this
   workflow's retirement, but it still exists, is still dispatchable, has
   no permissions block, and mints an App token able to approve+merge any
   PR in any `tomrivera-ops` repo (used by `faraday-ci-bot[bot]` as
   recently as 2026-07-04). While it exists, "Validator authorizes,
   GitHub enforces" is not the only merge path in the org. Phase 1's
   `bot-review-not-authoritative` required check detects bot APPROVEs on
   governed PRs but cannot prevent that workflow merging around review.
   **Retirement needs a founder-authorized change to
   `tomrivera-ops/.github`; flagged, not executed here.**
2. **agent-contracts is unprotectable while private on Free plan** — the
   Phase 1.5 contract release rides on an unprotected default branch.
   Same decision surface as this repo's visibility ruling.
3. **Two schema authorities with overlapping names.** agent-contracts and
   faraday-governance both publish `audit-event` (different `$id`
   spaces); shield-ai diverges further (already recorded). For Phase 1:
   review/decision contracts come from **agent-contracts**; policy bundle
   and its schema come from **faraday-governance**. One authority per
   contract name.
4. **agent-contracts v0.2.0 CHANGELOG self-describes as "unreleased"**
   inside the released tag. Cosmetic; correct in the v0.3.0 PR.
5. **No pinnable release exists** for faraday-validator or
   architecture-registry (zero tags). Nothing in Phase 1 pins either.
6. architecture-registry is an aspirational scaffold (declared
   directories not committed); nothing to integrate with in Phase 1.
7. Stale memory pin: faraday-governance main has advanced to
   `ba5e64cf…` (PR #13, ADR-0001 SSOT split) past the `7739146` recorded
   in operator memory. The ADR-0001 split is consistent with item 3's
   one-authority-per-name rule.

## 10. Inputs this discovery fixes for later phases

- Contract schemas: draft 2020-12, `additionalProperties: false`,
  `$id` under `https://faraday-lab.dev/schemas/`, additive minor release
  v0.3.0.
- Policy bundle: new directory + schema in faraday-governance, released
  as a new v* tag; `FARADAY_POLICY_DIGEST` = `canonical.bundle_digest`
  of the bundle directory at that tag.
- Validator: this repo, console script `faraday-validator`, subcommand
  `evaluate-pr`.
- Shadow workflow: new `reusable-faraday-review.yml` +
  `workflow-templates/faraday-review.yml` in `tomrivera-ops/.github`,
  meeting this repo's hardening guard even where org convention is
  looser.
- Founder adjudication matches `node_id == U_kgDODk3msA`.
- Secret detection: gitleaks primary; GitHub alerts path included but
  expected-absent until scanning is enabled.
