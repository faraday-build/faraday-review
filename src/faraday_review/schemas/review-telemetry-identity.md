# Review telemetry identity (normative, v0.4.0)

This document defines identity for `review-telemetry-event` records. It ships in the schema bundle alongside `review-telemetry-event.schema.json`. Conformance vectors are in `conformance/review-telemetry-identity.vectors.json`.

The key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## Identities

| Identity | Definition | Source |
|---|---|---|
| Event | One emitted record | `event_id` (v0.4.0+). For a record without one, a consumer-internal content key over the full record. |
| Execution | One reviewer invocation | `(execution.provider, casefold(repository), execution.run_id, execution.run_attempt)`. If `execution` is absent but `event_id` is present: the event itself. Without `event_id`: **unknown**. |
| Attempt | A platform re-run of the same run | `execution.run_attempt` within `execution.run_id` |
| Archived copy | One stored appearance of an event | Consumer-side location (path, file digest, line). **Never part of any identity.** |

These identifiers keep their existing meaning and are **not** execution identity:

- `review_id` is the review subject.
- `fingerprint` is the idempotency digest over the review inputs.
- `retries` counts in-execution API retries.
- Task identifiers (FounderPM `correlation_id`, packet `idempotency_key`) and Agent Transport `request_id` / `trace_id` / `idempotency_key` are not carried by this contract. A transport-dispatched reviewer would be a new `execution.provider`, which is a contract change.

## Producer rules

- **P1.** A producer MUST emit at most one record per execution.
- **P2.** `event_id` MUST be a fresh lowercase UUID, version 4 or 7, generated per emission. It MUST NOT be derived from record content, so two executions with identical content still differ.
- **P3.** When the platform execution is known, the producer SHOULD emit `execution`. `execution` MUST NOT appear without `event_id`; the schema enforces this.
- **P4.** `event_id` and `execution` MUST NOT appear on a record whose `contract_version` is below v0.4.0; the schema enforces this.

## Consumer rules

Inputs are a multiset of archived copies.

- **C1. Validity.** A copy that fails the schema is excluded and reported as `invalid_record`. It never contributes to identity.
- **C2. Event key.** The key is `event_id` when present. Otherwise it is a content key over the full record: canonical JSON with sorted keys, no insignificant whitespace and UTF-8. Only equality of keys matters, and they need not be stable across implementations. A key over a subset of fields MUST NOT be used.
- **C3. Copies.** Copies with equal key and equal canonical content are one event, whatever their number, location or order.
- **C4. Conflict.** Copies that share an `event_id` but differ in content are `event_id_conflict`. The event is ineligible: it MUST NOT count toward any qualification or select a current head. All its copies are reported. Fail closed: a conflict can remove evidence but never add it.
- **C5. Separate runs.** Different `run_id` values are different executions, even if everything else is identical.
- **C6. Re-runs.** The same `run_id` with a different `run_attempt` is a different execution of the same run. `retries` never creates or merges identities.
- **C7. Duplicate execution.** Different `event_id` values with equal execution identity violate P1. The consumer reports `multiple_events_per_execution`, keeps both events and counts the execution once.
- **C8. Legacy records** (no `event_id`) have unknown execution identity, and the consumer MUST NOT synthesize one. Their presence is reported as `legacy_execution_identity_unknown`. For a set of non-conflicted events:
  - `executions_exact` is the number of distinct known execution identities.
  - `execution_count` is `executions_exact` if no legacy event is present, otherwise `null`.
  - The bounds are `min = executions_exact + distinct legacy events` and `max = executions_exact + legacy copies`. This rests on the v0.3.0 producer emitting one record per execution.
  - A conflicted event (C4) counts in none of these figures.
- **C9. Determinism.** Every result MUST be independent of copy order and location.

## Security

- `event_id` and `execution` are producer assertions, not authentication. A consumer MAY cross-check them against platform records. A mismatch is reported and never raises trust.
- A forged duplicate `event_id` can at worst make an event ineligible (C4). It cannot inflate any count.
- `run_id` and `run_attempt` are public platform values. No identity field carries a secret.
- `run_id` is not a clock. Ordering uses `created_at`.
- `event_id` is also fixed at 36 characters, not by pattern alone, because validators disagree on whether `$` matches before a trailing newline.

## Compatibility

- v0.4.0 is additive. No field is renamed, removed or newly required, and every valid v0.3.0 record is a valid v0.4.0 record.
- Under Ruling 16, consumers accept contract minors N and N-1. A consumer that still validates only against v0.3.0 rejects v0.4.0 identity fields, because v0.3.0 sets `additionalProperties: false`. Once v0.4.0 records exist in an archive, a consumer MUST NOT be rolled back below v0.4.0 acceptance.
