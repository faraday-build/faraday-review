# faraday-review

Deterministic PR enforcement plane for governed Faraday repositories —
**Phase 1: no model inference anywhere**.

```text
Governance defines policy.   (faraday-capital-systems/faraday-governance, pinned digest)
Validator authorizes.        (faraday-validator evaluate-pr — this package)
GitHub enforces.             (rulesets + required checks)
Founder handles exceptions.  (faraday/founder-* adjudication labels)
```

## What it does

- exact-SHA binding and freshness (full 40-hex, lowercase-normalized,
  short SHAs rejected, head refetched before validation and publication)
- pinned governance policy digest verification (`FARADAY_POLICY_DIGEST`,
  fail closed on absence or mismatch)
- protected-path classification (green/yellow/red; unmatched paths
  default yellow)
- required-check verification
- secret detection (gitleaks primary; findings never contain the value)
- distinct escalations for oversized PRs vs API budget exhaustion
- deterministic `faraday-validator evaluate-pr` (golden-file harness
  across six hash seeds)
- one idempotent governed summary per head SHA; **never approves, never
  merges** — approval and merge authorization are disabled in Phase 1 for
  every risk class

## Usage

```bash
faraday-validator evaluate-pr \
  --context pr-context.json \
  --evidence review-evidence.json \
  --policy-bundle path/to/review-policy \
  --expected-policy-digest "$FARADAY_POLICY_DIGEST" \
  --output decision.json

faraday-review shadow-run --repository owner/repo --pr 42 \
  --policy-bundle path/to/review-policy --repo-dir . \
  --required-check ci
```

## Docs

`docs/ARCHITECTURE.md`, `docs/THREAT_MODEL.md`, `docs/DEPLOYMENT.md`,
`docs/ROLLBACK.md`, `docs/GITHUB_PERMISSIONS.md`,
`docs/OPERATING_MODEL.md`, `docs/DISCOVERY.md`, `docs/PHASE_2_INPUTS.md`.
