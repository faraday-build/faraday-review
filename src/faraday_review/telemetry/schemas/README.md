# Repo-local qualification schemas

NOT vendored from agent-contracts. These describe faraday-review's own
Phase 1 qualification outputs (`faraday-review adjudicate | aggregate |
readiness`) and inputs (`docs/qualification/synthetic-prs.json`). Owned and
versioned by this repo; the vendored contracts live in
`src/faraday_review/schemas/` and are never edited here.

Authorization fields are deliberately absent: the Validator's class is
carried as `classifier_class`, `founder_review_required` as `escalated`,
and the bundle digest as `policy_bundle_digest`.

`qualification-ledger.schema.json` is format `qualification-ledger/v2`
(execution identity, agent-contracts v0.4.0).
`qualification-readiness.schema.json` is format `qualification-readiness/v3`
(gate on adjudicated qualifying real PRs; v2 criterion kept as
`gate.superseded_criterion`). Readiness reports are derived outputs, never
re-ingested, so no frozen v2 copy is kept.
`qualification-ledger-v1.schema.json` is a frozen copy of v1. It is used
only to validate a v1 ledger passed to `aggregate --prior-ledger`, and is
never produced.
