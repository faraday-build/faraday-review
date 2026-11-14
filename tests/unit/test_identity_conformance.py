"""T23: the vendored agent-contracts v0.4.0 set vectors, run through the real
`aggregate`.

Each copy is written to its own artifact path as telemetry.jsonl, and the
ledger's set-level identity figures are compared with the contract's
expected values. That ties faraday-review to the contract's own reading of
C1-C9 rather than a private one.

Anomaly names: the contract's `invalid_record` is faraday-review's existing
`invalid_telemetry_event`. Every other identity anomaly uses the contract
name unchanged.

Vector records carry no decision.json, so `policy_digest_unverified`
anomalies appear. They are outside the identity rules and are not compared.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from faraday_review.canonical import canonical_bytes
from faraday_review.telemetry.qualification import aggregate

TS = "2026-11-12T00:00:00Z"
VECTORS = json.loads((Path(__file__).resolve().parents[2] / "src" / "faraday_review" / "schemas"
                      / "conformance" / "review-telemetry-identity.vectors.json")
                     .read_text(encoding="utf-8"))
IDENTITY_KINDS = {"event_id_conflict", "multiple_events_per_execution",
                  "legacy_execution_identity_unknown", "invalid_telemetry_event"}
CONTRACT_TO_LOCAL = {"invalid_record": "invalid_telemetry_event"}


def _write(root: Path, copies: list[dict], sources: list[str]) -> None:
    for copy, source in zip(copies, sources):
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(copy["record"], sort_keys=True, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _observed(ledger: dict) -> dict:
    ident = ledger["counts"]["identity"]
    return {
        "events": ident["events"],
        "executions_exact": ident["executions_exact"],
        "execution_count": ident["execution_count"],
        "bounds": ident["execution_count_bounds"],
        "invalid": ident["invalid_events"],
        "conflicts": sorted({a["event_id"] for a in ledger["anomalies"]
                             if a["kind"] == "event_id_conflict"}),
        "anomalies": sorted({a["kind"] for a in ledger["anomalies"]} & IDENTITY_KINDS),
    }


def _expected(vector: dict) -> dict:
    exp = vector["expected"]
    return {
        "events": exp["events"],
        "executions_exact": exp["executions_exact"],
        "execution_count": exp["execution_count"],
        "bounds": exp["bounds"],
        "invalid": exp["invalid"],
        "conflicts": sorted(exp["conflicts"]),
        "anomalies": sorted(CONTRACT_TO_LOCAL.get(k, k) for k in exp["anomalies"]),
    }


def test_vector_file_is_the_v040_set():
    assert VECTORS["contract_version"] == "v0.4.0"
    assert [v["id"] for v in VECTORS["sets"]] == [f"S{i:02d}" for i in range(1, 17)]


@pytest.mark.parametrize("vector", VECTORS["sets"], ids=lambda v: v["id"])
def test_set_vector(tmp_path, vector):
    copies = vector["copies"]
    sources = [c["source"] for c in copies]
    _write(tmp_path / "base", copies, sources)
    base = aggregate(tmp_path / "base", generated_at=TS)
    assert _observed(base) == _expected(vector)

    # C9: every copy order, and every rotation of copies over locations,
    # gives the same identity figures.
    reference = canonical_bytes(_observed(base))
    n = 0
    for order in itertools.permutations(range(len(copies))):
        for shift in range(len(copies)):
            n += 1
            ordered = [copies[i] for i in order]
            rotated = sources[shift:] + sources[:shift]
            root = tmp_path / f"p{n}"
            _write(root, ordered, rotated)
            assert canonical_bytes(_observed(aggregate(root, generated_at=TS))) == reference, \
                (vector["id"], order, shift)
