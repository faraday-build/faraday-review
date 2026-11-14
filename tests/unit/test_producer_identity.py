"""Producer identity (agent-contracts v0.4.0, review-telemetry-identity P1-P4).

T27 runs the real `faraday-review shadow-run` command path. Only the GitHub
calls are replaced: context collection, head refetch and secret alerts.
The Validator, the policy bundle (a golden fixture), the event builder and
the writer are the real ones.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from faraday_review import cli
from faraday_review.evidence.schema_validation import validation_errors
from faraday_review.models import PullRequestContext
from faraday_review.telemetry.events import build_run_event
from faraday_review.telemetry.qualification import aggregate

GOLDEN = Path(__file__).resolve().parents[1] / "determinism" / "golden" / "green-docs"
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture()
def offline(monkeypatch, tmp_path):
    raw = json.loads((GOLDEN / "context.json").read_text(encoding="utf-8"))
    raw["contract_version"] = "v0.4.0"
    context = PullRequestContext.from_json(raw)
    monkeypatch.setattr(cli, "collect_context", lambda *a, **k: context)
    monkeypatch.setattr(cli, "fetch_head_sha", lambda *a, **k: context.head_sha)
    monkeypatch.setattr(cli, "collect_secret_alerts", lambda *a, **k: [])
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-secret")
    bundle = tmp_path / "bundle"
    shutil.copytree(GOLDEN / "policy-bundle", bundle)
    digest = json.loads((GOLDEN / "expected-decision.json").read_text())["policy_digest"]
    return {"bundle": bundle, "digest": digest, "repo": raw["repository"], "pr": raw["pr_number"]}


def _shadow(offline, artifacts: Path, *extra: str) -> int:
    return cli.main(["shadow-run", "--repository", offline["repo"], "--pr", str(offline["pr"]),
                     "--policy-bundle", str(offline["bundle"]),
                     "--expected-policy-digest", offline["digest"],
                     "--artifacts-dir", str(artifacts), "--skip-publish", *extra])


def _lines(artifacts: Path) -> list[dict]:
    return [json.loads(x) for x in (artifacts / "telemetry.jsonl").read_text().splitlines() if x]


def test_t27_one_identified_event_per_invocation(offline, tmp_path):
    out = tmp_path / "a"
    assert _shadow(offline, out, "--run-id", "43507102983", "--run-attempt", "2") == 0
    events = _lines(out)
    assert len(events) == 1
    event = events[0]
    assert validation_errors("review-telemetry-event", event) == []
    assert event["contract_version"] == "v0.4.0"
    assert UUID4.match(event["event_id"])
    assert event["execution"] == {"provider": "github_actions", "run_id": 43507102983,
                                  "run_attempt": 2}


def test_t27b_each_invocation_gets_a_fresh_event_id(offline, tmp_path):
    """P2: two invocations with identical inputs, even into one directory,
    are two events that never collapse."""
    out = tmp_path / "a"
    for _ in range(2):
        assert _shadow(offline, out, "--run-id", "7", "--run-attempt", "1") == 0
    events = _lines(out)
    assert len(events) == 2
    assert events[0]["event_id"] != events[1]["event_id"]
    ledger = aggregate(tmp_path, generated_at="2026-11-12T00:00:00Z")
    assert ledger["counts"]["identity"]["events"] == 2
    # Same run id and attempt: the consumer sees one execution, two events (C7).
    assert ledger["counts"]["identity"]["executions_exact"] == 1
    assert "multiple_events_per_execution" in {a["kind"] for a in ledger["anomalies"]}


def test_t27c_local_run_emits_event_id_without_execution(offline, tmp_path):
    out = tmp_path / "a"
    assert _shadow(offline, out) == 0
    (event,) = _lines(out)
    assert UUID4.match(event["event_id"])
    assert "execution" not in event
    assert validation_errors("review-telemetry-event", event) == []


def test_t27d_event_id_is_injectable(offline, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_new_event_id", lambda: "4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f601")
    out = tmp_path / "a"
    assert _shadow(offline, out, "--run-id", "1", "--run-attempt", "1") == 0
    assert _lines(out)[0]["event_id"] == "4f1c2a3b-5d6e-4f70-8a91-b2c3d4e5f601"


@pytest.mark.parametrize("extra", [
    ["--run-id", "5"],
    ["--run-attempt", "1"],
    ["--run-id", "0", "--run-attempt", "1"],
    ["--run-id", "5", "--run-attempt", "0"],
    ["--run-id", "-3", "--run-attempt", "1"],
    ["--run-id", "x", "--run-attempt", "1"],
    ["--run-id", "5", "--run-attempt", "1.5"],
])
def test_t27e_run_flags_are_both_or_neither_and_positive(offline, tmp_path, extra):
    out = tmp_path / "a"
    with pytest.raises(SystemExit) as exc:
        _shadow(offline, out, *extra)
    assert exc.value.code == 2
    assert not (out / "telemetry.jsonl").exists()


def test_builder_never_emits_execution_without_event_id():
    with pytest.raises(ValueError, match="P3"):
        build_run_event(review_id="r", fingerprint="sha256:" + "f" * 64, decision={},
                        finding_counts={}, stale_state="fresh", duration_seconds=1.0,
                        github_requests_used=1, retries=0, final_status="completed",
                        created_at="2026-11-01T10:00:00Z",
                        execution={"provider": "github_actions", "run_id": 1, "run_attempt": 1})


def test_builder_without_identity_is_unchanged():
    event = build_run_event(review_id="r", fingerprint="sha256:" + "f" * 64, decision={},
                            finding_counts={}, stale_state="fresh", duration_seconds=1.0,
                            github_requests_used=1, retries=0, final_status="completed",
                            created_at="2026-11-01T10:00:00Z")
    assert "event_id" not in event and "execution" not in event
