"""Vendored agent-contracts v0.4.0: byte pin and the Ruling 16 window."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from faraday_review import CONTRACT_VERSION, SUPPORTED_CONTRACT_MAJOR, SUPPORTED_CONTRACT_MINOR
from faraday_review.evidence.schema_validation import validation_errors
from faraday_review.governance.authorization import contract_version_reasons

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "src" / "faraday_review" / "schemas"
PIN = json.loads((SCHEMAS / "VENDORED.json").read_text(encoding="utf-8"))


def test_pin_names_the_release():
    assert PIN["source"] == "tomrivera-ops/agent-contracts"
    assert PIN["tag"] == CONTRACT_VERSION == "v0.4.0"
    assert PIN["source_commit"] == "b7a60291766c54070f8851a080d72fa1d054343a"
    assert PIN["bundle_sha256"] == "4b8847341138025d60ecdd942e4d417eaeb607355a8e6727c64f1a2cc8ef3f60"


def test_every_vendored_byte_matches_the_release_manifest():
    present = sorted(p.relative_to(SCHEMAS).as_posix() for p in SCHEMAS.rglob("*")
                     if p.is_file() and p.name not in ("README.md", "VENDORED.json"))
    assert present == sorted(PIN["files"])
    for rel, digest in PIN["files"].items():
        assert hashlib.sha256((SCHEMAS / rel).read_bytes()).hexdigest() == digest, rel


def test_identity_rules_and_vectors_are_shipped_in_the_package():
    assert (SCHEMAS / "review-telemetry-identity.md").is_file()
    vectors = json.loads((SCHEMAS / "conformance" / "review-telemetry-identity.vectors.json")
                         .read_text(encoding="utf-8"))
    assert vectors["contract_version"] == "v0.4.0"


@pytest.mark.parametrize("version,ok,minor", [
    ("v0.4.0", True, 4), ("v0.3.0", True, 3), ("v0.4.7", True, 4),
    ("v0.2.0", False, None), ("v0.5.0", False, None), ("v1.4.0", False, None),
    ("0.4.0", False, None), (None, False, None),
])
def test_ruling_16_window_is_n_and_n_minus_1(version, ok, minor):
    assert (SUPPORTED_CONTRACT_MAJOR, SUPPORTED_CONTRACT_MINOR) == (0, 4)
    reasons, evaluated = contract_version_reasons(version)
    assert (reasons == []) is ok
    assert evaluated == minor


def _v03_event() -> dict:
    return {
        "contract_version": "v0.3.0", "review_id": "frr-x-y-1-aaaaaaaaaaaa",
        "repository": "x/y", "pr_number": 1, "base_sha": "b" * 40, "head_sha": "a" * 40,
        "fingerprint": "sha256:" + "f" * 64, "deterministic_finding_counts":
        {"info": 0, "warning": 0, "blocking": 0}, "stale_state": "fresh",
        "duration_seconds": 1.0, "github_requests_used": 1, "retries": 0,
        "final_status": "completed", "created_at": "2026-11-01T10:00:00Z",
    }


def test_v030_records_stay_valid_under_the_v040_schema():
    assert validation_errors("review-telemetry-event", _v03_event()) == []


def test_identity_fields_are_rejected_on_a_v030_record():
    event = _v03_event()
    event["event_id"] = "4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f601"
    assert validation_errors("review-telemetry-event", event) != []


def test_v040_record_with_identity_is_valid():
    event = _v03_event()
    event.update(contract_version="v0.4.0", event_id="4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f601",
                 execution={"provider": "github_actions", "run_id": 7, "run_attempt": 1})
    assert validation_errors("review-telemetry-event", event) == []
