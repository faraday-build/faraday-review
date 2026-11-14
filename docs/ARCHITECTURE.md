# Architecture — Phase 1 Deterministic Plane

## Authority invariant

```text
Governance defines policy. Validator authorizes. GitHub enforces.
Founder handles exceptions.
```

Phase 1 has no model reviewers; the deterministic plane produces the
evidence itself (`evidence_class: deterministic`).

## Data flow (shadow run)

```text
collect PR metadata ─┐
collect changed files ├─> PullRequestContext ──┐
observe req. checks ──┘                        │
gitleaks + GH alerts ──> DeterministicFindings ├─> ReviewEvidence
refetch head (freshness gate 1) ───────────────┘        │
                                                        v
policy bundle (pinned digest) ──> faraday-validator evaluate-pr
                                                        │
                    RiskAssessment + MergeAuthorization + FounderEscalation
                                                        │
refetch head (freshness gate 2) ──> check run + idempotent comment
                                                        │
                                        append-only telemetry JSONL
```

## Module boundaries

| Package | Responsibility | May write authorization fields? |
|---|---|---|
| `canonical` | canonical bytes, digests | n/a |
| `config` | budget + pr_limits, startup assertion | no |
| `deterministic/` | path class, checks, secrets, limits — findings only | no |
| `evidence/` | envelope build, schema validation, freshness | no |
| `github/` | budgeted client, collection, publication | no |
| `governance/` | policy load, digest pin, classification, decision | **yes — only here** |
| `telemetry/` | run events, adjudication, throughput, qualification ledger + readiness | passthrough only (neutral names in qualification outputs) |

`tools/guards/authorization_boundary.py` enforces the last column in CI.

## Determinism

The Validator (`governance/validator_client.py`) is deterministic for
identical inputs: no network, no RNG, no set iteration reaching output,
`blocking_reasons` sorted, `evaluated_at` the only volatile field. The
golden harness (`tests/determinism`) proves it byte-for-byte across six
interpreter hash seeds and a same-process repeat.

The one permitted RNG in the runtime is retry jitter (`github/budget.py`);
it affects timing only and cannot reach a decision.

## Contracts

agent-contracts **v0.4.0**, vendored under `src/faraday_review/schemas/`.
Ruling 16: evidence contract minor N and N-1 accepted;
`evaluated_contract_minor` recorded in every decision. The evidence
envelope is `PullRequestReviewEvidence` — distinct from the TR-004
`ReviewEvidence` session artifact (one authority per contract name).

## Policy

faraday-governance **v1.1.0**, `review-policy/` bundle, pinned as
`FARADAY_POLICY_DIGEST` (`canonical.bundle_digest` over the bundle
directory). Absent pin or mismatch ⇒ Block. The bundle schema pins
`approval_authorized`/`merge_authorized` to `const: false`; Phases 3–4
must change policy version, schema, tag, and digest to alter that.
