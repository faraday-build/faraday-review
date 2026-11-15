"""Founder ruling 2026-11-14, O3 (Codex P2 on #11, r22): diagnostics that
qualification outputs persist (anomalies[].errors) must never echo input
values. jsonschema messages embed the rejected instance, so a private
repository identity inside an invalid manifest, telemetry event or
adjudication record would otherwise reach a committed ledger/readiness in a
public checkout under no identity key, past the public-identity check."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from qualification_support import write_label, write_run

from faraday_review.canonical import canonical_bytes
from faraday_review.cli import main
from faraday_review.telemetry.adjudication import ADJUDICATIONS_FILE
from faraday_review.telemetry.qualification import (
    aggregate,
    persisted_event_diagnostics,
    persisted_local_diagnostics,
)

TS = "2026-11-14T23:00:00Z"
SECRET_OWNER = "zz-probe-org"
SECRET = f"{SECRET_OWNER}/zz-probe-private"


def _ledger_bytes(input_dir: Path) -> bytes:
    return canonical_bytes(aggregate(input_dir, generated_at=TS))


def _assert_no_identity(blob: bytes) -> None:
    text = blob.decode("utf-8")
    assert SECRET not in text
    assert SECRET_OWNER not in text
    assert "zz-probe-private" not in text


def test_invalid_root_manifest_repository_object_never_echoed(tmp_path):
    # The Codex scenario: `repositories` holds a repository object, not an array.
    (tmp_path / "collection-manifest-20261114T230000Z.json").write_text(json.dumps({
        "format": "qualification-collection-manifest/v1", "collected_at": TS,
        "repositories": {"repository": SECRET, "runs": []},
    }))
    blob = _ledger_bytes(tmp_path)
    ledger = json.loads(blob)
    kinds = [a["kind"] for a in ledger["anomalies"]]
    assert "invalid_collection_manifest" in kinds
    errors = next(a for a in ledger["anomalies"]
                  if a["kind"] == "invalid_collection_manifest")["errors"]
    assert errors and all(": " in e and e.endswith("constraint violated") for e in errors)
    _assert_no_identity(blob)


def _add_key(path: Path) -> None:
    # Make an otherwise valid record invalid by exactly one unexpected key
    # whose NAME is the identity, so the value-echoing message is the only
    # diagnostic and survives the 5-error truncation.
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for record in lines:
        record[SECRET] = 1
    path.write_text("".join(json.dumps(r) + "\n" for r in lines))


def test_invalid_telemetry_event_value_never_echoed(tmp_path):
    run_dir = write_run(tmp_path, "r1", event_id="4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f601")
    _add_key(run_dir / "telemetry.jsonl")
    blob = _ledger_bytes(tmp_path)
    assert "invalid_telemetry_event" in [a["kind"] for a in json.loads(blob)["anomalies"]]
    _assert_no_identity(blob)


def test_invalid_adjudication_record_value_never_echoed(tmp_path):
    write_label(tmp_path, "l1")
    _add_key(tmp_path / "l1" / ADJUDICATIONS_FILE)
    blob = _ledger_bytes(tmp_path)
    assert "invalid_adjudication_record" in [a["kind"] for a in json.loads(blob)["anomalies"]]
    _assert_no_identity(blob)


def test_unrecognised_keys_in_the_location_are_masked():
    # None of today's persisted schemas validates values under arbitrary map
    # keys, so guard the masking directly with a synthetic map schema: an
    # identity used as a key must never appear in a persisted location.
    from jsonschema import Draft202012Validator

    from faraday_review.telemetry.qualification import _value_free

    validator = Draft202012Validator({"type": "object",
                                      "additionalProperties": {"type": "integer"}})
    out = _value_free(validator.iter_errors({SECRET: "x", "plain_key": "y"}))
    assert out == ["<key>: type constraint violated", "plain_key: type constraint violated"]
    for name, instance in (("qualification-collection-manifest",
                            {"repositories": [{SECRET: "x"}]}),):
        diags = persisted_local_diagnostics(name, instance)
        assert diags and all(SECRET not in d and SECRET_OWNER not in d for d in diags)
    diags = persisted_event_diagnostics({"head_sha": SECRET})
    assert diags and all(SECRET not in d for d in diags)

def test_cli_writes_value_free_ledger_into_public_checkout(tmp_path):
    # End to end: an empty enrollment and a git checkout destination. The
    # ledger is written (it names no identity) and the identity is absent.
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "collection-manifest-20261114T230000Z.json").write_text(json.dumps({
        "format": "qualification-collection-manifest/v1", "collected_at": TS,
        "repositories": {"repository": SECRET, "runs": []},
    }))
    repo = tmp_path / "pub"
    (repo / "docs" / "qualification").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    enrollment = repo / "docs" / "qualification" / "enrollment.json"
    enrollment.write_text("[]")
    out = repo / "docs" / "qualification" / "ledger.json"
    assert main(["aggregate", "--input-dir", str(archive), "--output", str(out),
                 "--generated-at", TS, "--enrollment", str(enrollment)]) == 0
    _assert_no_identity(out.read_bytes())


# ------------------------------------------- CLI / raised diagnostics (r22 item 3)
# Qualification commands print schema failures to stderr. If one ever ran in a
# public CI log with a private enrollment, manifest, prior ledger, synthetic
# list or adjudication record, a value-echoing message would publish the
# identity there. These messages are value-free too.


def test_enrollment_schema_failure_message_is_value_free(tmp_path):
    import pytest

    from faraday_review.github.collection import CollectionError, load_enrollment

    path = tmp_path / "enrollment.json"
    path.write_text(json.dumps([{"repository": SECRET, "visibility": SECRET, SECRET: 1}]))
    with pytest.raises(CollectionError) as info:
        load_enrollment(path)
    assert SECRET not in str(info.value) and SECRET_OWNER not in str(info.value)
    assert "constraint violated" in str(info.value)


def test_prior_ledger_failures_are_value_free(tmp_path):
    import pytest

    from faraday_review.telemetry.qualification import QualificationError

    # An otherwise valid prior ledger with one unexpected key named by the
    # identity, so the value-echoing message is the only schema diagnostic.
    near_valid = json.loads(_ledger_bytes_empty())
    near_valid[SECRET] = 1
    for prior in ({"format": SECRET}, near_valid):
        with pytest.raises(QualificationError) as info:
            aggregate(tmp_path, generated_at=TS, prior_ledger=prior)
        assert SECRET not in str(info.value) and SECRET_OWNER not in str(info.value)


def test_synthetic_list_failure_is_value_free():
    import pytest

    from faraday_review.telemetry.qualification import QualificationError, readiness

    ledger = json.loads(_ledger_bytes_empty())
    with pytest.raises(QualificationError) as info:
        readiness(ledger, [{"repository": SECRET, SECRET: 1}], generated_at=TS,
                  ledger_evidence={"path": "l", "sha256": "0" * 64},
                  synthetic_evidence={"path": "s", "sha256": "0" * 64})
    assert SECRET not in str(info.value) and SECRET_OWNER not in str(info.value)


def _ledger_bytes_empty() -> bytes:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        return canonical_bytes(aggregate(Path(d), generated_at=TS))


def test_timestamp_failure_withholds_value():
    import pytest

    from faraday_review.telemetry.qualification import QualificationError, require_timestamp

    with pytest.raises(QualificationError) as info:
        require_timestamp(SECRET, "collected_at")
    assert SECRET not in str(info.value)


def test_adjudicate_schema_failure_on_stderr_is_value_free(tmp_path, capsys):
    # Guard only. The CLI validates its arguments and the builder always emits
    # a schema-valid record (bad labels/actors are recorded as rejected), so
    # the schema-failure branch is not reachable from the CLI today. Its
    # message is value-free anyway.
    code = main(["adjudicate", "--repository", SECRET, "--pr", "1", "--head-sha", SECRET,
                 "--label", "faraday/founder-green", "--actor-node-id", "U_x",
                 "--actor-login", "someone", "--labeled-at", TS,
                 "--artifacts-dir", str(tmp_path / "a")])
    err = capsys.readouterr().err
    assert code == 2
    assert SECRET not in err and SECRET_OWNER not in err
