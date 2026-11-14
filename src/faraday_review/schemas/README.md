# Vendored contracts

Vendored verbatim from `tomrivera-ops/agent-contracts` at tag **v0.4.0**
(source commit `b7a60291766c54070f8851a080d72fa1d054343a`, the merge of
agent-contracts PR #14). Release bundle `agent-contracts-schema-bundle.tar.gz`
sha256 `4b8847341138025d60ecdd942e4d417eaeb607355a8e6727c64f1a2cc8ef3f60`.
The validator works offline, so its inputs cannot vary with registry
availability.

`VENDORED.json` pins the sha256 of every file here, copied from the release
manifest. `tests/unit/test_vendored_contracts.py` fails if any vendored byte
differs from that pin, or if a file is added or removed without updating it.

v0.4.0 adds to the v0.3.0 set:

- `review-telemetry-event.schema.json`: optional `event_id` and `execution`.
  This is the only changed schema.
- `review-telemetry-identity.md`: normative identity rules (P1–P4, C1–C9).
  `telemetry/qualification.py` implements the consumer rules.
- `conformance/review-telemetry-identity.vectors.json`: the contract's own
  conformance vectors, run against `aggregate` by
  `tests/unit/test_identity_conformance.py`.

Do not edit these files here. To move to a new contract release:

1. Bump the pin in `faraday_review/__init__.py`.
2. Re-vendor from the released bundle, after `scripts/verify-bundle.sh` passes on it.
3. Regenerate `VENDORED.json` from the release manifest.
4. Update the Ruling 16 acceptance window (N/N-1).

Do all four in one PR.
