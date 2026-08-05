# faraday-review — agent rules

Phase 1 is a DETERMINISTIC enforcement plane. Non-negotiable:

- No model inference anywhere in this repo. No providers/, review/,
  prompts/, architecture/ packages, and no stub modules for them.
- Authorization fields (approval_authorized, merge_authorized,
  founder_review_required, risk_class, policy_digest, policy_version,
  contract_version) are produced ONLY inside src/faraday_review/governance/.
  The authorization_boundary guard enforces this; do not work around it —
  consumers copy from the Validator's decision document verbatim.
- Approval and merge stay disabled for every risk class. Green is
  information, not authorization. Do not add any code path that approves
  or merges a PR.
- The Validator is deterministic: no network, no RNG, no set iteration
  reaching output, no clock outside evaluated_at. Any change must keep
  the six-seed golden harness byte-identical or regenerate goldens in the
  same PR with the diff explained.
- Secret values never appear in findings, evidence, telemetry, artifacts,
  logs, or comments — rule id / path / line / rule class only.
- Contracts are vendored from agent-contracts v0.3.0; policy is pinned by
  FARADAY_POLICY_DIGEST from governance v1.1.0. Fail closed on both.
- Every commit signed; every change lands by PR through the required
  checks (authorization-boundary, workflow-hardening, guard-self-tests,
  tests, determinism). Never bypass ruleset 20418235.
- tests/determinism/golden/ encodes ratified behavior — regenerate only
  via tests/determinism/generate_golden.py and review the diff.
