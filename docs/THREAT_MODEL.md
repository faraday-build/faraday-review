# Threat Model — Phase 1

Assets: merge authorization integrity, policy integrity, secret
non-disclosure, calibration-data integrity (Phase 2 depends on it).

| # | Threat | Control |
|---|---|---|
| 1 | PR author swaps head after review ("review one SHA, merge another") | Exact-SHA binding; head refetched before validation and again before publication; stale ⇒ Block `evidence_stale` |
| 2 | Policy tampering (silent rule relaxation) | Pinned `FARADAY_POLICY_DIGEST` from an immutable governance tag; absent/mismatch ⇒ Block; authorization fields `const: false` in bundle schema |
| 3 | Non-governance code fabricating authorization | `authorization_boundary.py` guard (required check): auth fields produced only in `governance/`; consumers relay, never derive |
| 4 | Bot/App approval laundering | `bot-review-not-authoritative` required check fails on any Bot APPROVED review; MergeAuthorization `authorized_by` enum excludes bots. Residual risk: `tomrivera-ops/.github/cli-approve-merge.yml` still exists despite ADR-0012 — flagged in DISCOVERY §9.1, retirement is a founder action |
| 5 | Secret value exfiltration via review artifacts | Value-bearing gitleaks fields dropped at parse; contract forbids values in findings; tests assert non-leakage across evidence, telemetry, and comment |
| 6 | Workflow privilege escalation (pull_request_target, unpinned actions, script injection) | `workflow_hardening.py` guard (required check): SHA pins, permission ceilings, no event interpolation in `run:`, no fail-open steps |
| 7 | Oversized-PR noise poisoning Phase 2 calibration | `pr_too_large_for_deterministic_classification` distinct from `github_api_budget_exceeded`; misconfigured budget fails at startup |
| 8 | Adjudication spoofing (login squatting) | Founder matched on immutable `node_id` (`U_kgDODk3msA`), never login; rejects logged `adjudication_rejected` |
| 9 | Determinism drift (env-dependent decisions) | Six-seed cross-process golden harness + same-process repeat; byte comparison of canonical form; volatile list pinned to exactly `evaluated_at` |
| 10 | Fail-open on infrastructure error | Every failure path resolves to approval=false, merge=false, founder_review_required=true; collection-phase budget death fails the run loudly (paged), never silently green |

Out of scope in Phase 1 (no model exists): prompt injection, provider
compromise, model-evidence forgery. These enter the model-phase threat
model via `PHASE_2_INPUTS.md`.
