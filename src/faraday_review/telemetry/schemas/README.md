# Repo-local qualification schemas

NOT vendored from agent-contracts. These describe faraday-review's own
Phase 1 qualification outputs (`faraday-review adjudicate | aggregate |
readiness`) and inputs (`docs/qualification/synthetic-prs.json`). Owned and
versioned by this repo; the vendored contracts live in
`src/faraday_review/schemas/` and are never edited here.

Authorization fields are deliberately absent: the Validator's class is
carried as `classifier_class`, `founder_review_required` as `escalated`,
and the bundle digest as `policy_bundle_digest`.
