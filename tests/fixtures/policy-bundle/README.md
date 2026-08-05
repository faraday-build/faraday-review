# Faraday Review — Policy Bundle

Definitions-only policy consumed by the Faraday Review deterministic
enforcement plane (`faraday-build/faraday-review`). No instance data, no
secrets, no engine code — the engine lives in the consumer.

## Files

| File | Purpose |
|---|---|
| `approval-policy.yaml` | Risk classes (green/yellow/red/block), authorization posture (all false in Phase 1), block conditions |
| `protected-paths.yaml` | Path patterns → class floors; unmatched paths default yellow |
| `evidence-requirements.yaml` | Evidence contract, accepted evidence classes, SHA binding and freshness, required deterministic checks |
| `provider-requirements.yaml` | Required model providers (empty in Phase 1; `deterministic_only: true`) |
| `policy.schema.json` | Draft 2020-12 schema validating the four YAMLs assembled into one object keyed by file basename |

## Digest and pin

Each governance release tags and publishes this bundle's digest, computed
over the **bundle directory** with `canonical.bundle_digest` from
`faraday_review.canonical`: a sorted manifest of
`{relative_path: sha256(file bytes, line endings normalized to LF)}`,
canonically serialized and hashed. This is stable under CRLF checkouts,
file ordering, and mtimes.

Consumers pin the released digest as `FARADAY_POLICY_DIGEST` and fail
closed: absent pin → Block (`policy_digest_unpinned`); mismatch → Block
(`policy_digest_mismatch`).

Phase changes that would authorize anything (Green approval in Phase 3,
auto-merge in Phase 4) require a new `policy_version`, a schema change
(the authorization fields are schema-pinned `const: false`), a new release
tag, and a new published digest — the pin makes silent policy drift
impossible.
