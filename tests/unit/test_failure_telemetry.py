"""Producer failure telemetry (GHO-004 P1).

Every shadow run that ends early must still leave one schema-valid v0.4.0
telemetry record with `final_status: failed`, a `failure_mode`, a fresh
`event_id` and the platform execution identity. Authorization fields are
copied verbatim from the Validator's decision when one exists; otherwise
they are the governance-owned fail-closed posture (approval and merge
false, founder review required). Exit code 2 is unchanged.

These tests drive the real `faraday-review shadow-run` / `record-failure`
command paths. Only the GitHub calls (and, per test, the failing step) are
replaced.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from faraday_review import CONTRACT_VERSION, REVIEWER_VERSION, cli
from faraday_review.errors import (
    BudgetExceededError,
    ConfigurationError,
    GitHubApiError,
    PublicationError,
)
from faraday_review.evidence.schema_validation import validation_errors
from faraday_review.models import PullRequestContext

GOLDEN = Path(__file__).resolve().parents[1] / "determinism" / "golden" / "green-docs"
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
HEAD = "1" * 40
BASE = "2" * 40
FAIL_CLOSED = {"approval_authorized": False, "merge_authorized": False,
               "founder_review_required": True}


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
    return {"bundle": bundle, "digest": digest, "repo": raw["repository"],
            "pr": raw["pr_number"], "context": context}


def _shadow(offline, artifacts: Path, *extra: str, publish: bool = False) -> int:
    argv = ["shadow-run", "--repository", offline["repo"], "--pr", str(offline["pr"]),
            "--policy-bundle", str(offline["bundle"]),
            "--expected-policy-digest", offline["digest"],
            "--artifacts-dir", str(artifacts),
            "--run-id", "43507102983", "--run-attempt", "2", *extra]
    if not publish:
        argv.append("--skip-publish")
    return cli.main(argv)


def _lines(artifacts: Path) -> list[dict]:
    path = artifacts / "telemetry.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def _assert_failed(event: dict, mode: str, *, head: str, base: str) -> None:
    assert validation_errors("review-telemetry-event", event) == []
    assert event["final_status"] == "failed"
    assert event["failure_mode"] == mode
    assert event["contract_version"] == CONTRACT_VERSION == "v0.4.0"
    assert UUID4.match(event["event_id"])
    assert event["execution"] == {"provider": "github_actions", "run_id": 43507102983,
                                  "run_attempt": 2}
    assert event["head_sha"] == head and event["base_sha"] == base
    assert event["reviewer_version"] == REVIEWER_VERSION
    for key, value in FAIL_CLOSED.items():
        assert event[key] is value


# ------------------------------------------------------------ governance


def test_failed_run_posture_is_fail_closed_and_fresh():
    from faraday_review.governance.authorization import failed_run_posture

    posture = failed_run_posture()
    assert posture == {"contract_version": CONTRACT_VERSION, **FAIL_CLOSED}
    posture["approval_authorized"] = True
    assert failed_run_posture()["approval_authorized"] is False


# ------------------------------------------------------------ exit-2 paths


def test_gitleaks_error_emits_failed_event(offline, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "run_gitleaks", lambda *a, **k: 1)
    out = tmp_path / "a"
    assert _shadow(offline, out, "--repo-dir", str(tmp_path)) == 2
    (event,) = _lines(out)
    ctx = offline["context"]
    _assert_failed(event, "gitleaks_error", head=ctx.head_sha, base=ctx.base_sha)
    assert "risk_class" not in event
    assert not (out / "decision.json").exists()


@pytest.mark.parametrize("exc", [FileNotFoundError("gitleaks"),
                                 subprocess.TimeoutExpired("gitleaks", 1)])
def test_gitleaks_not_runnable_is_gitleaks_error(offline, tmp_path, monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(cli, "run_gitleaks", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, "--repo-dir", str(tmp_path)) == 2
    (event,) = _lines(out)
    assert event["failure_mode"] == "gitleaks_error"


def test_budget_exhaustion_before_context_uses_event_payload_shas(offline, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise BudgetExceededError("budget 0/0")
    monkeypatch.setattr(cli, "collect_context", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, "--head-sha", HEAD, "--base-sha", BASE) == 2
    (event,) = _lines(out)
    _assert_failed(event, "github_api_budget_exceeded", head=HEAD, base=BASE)
    assert "stale_state" not in event


def test_budget_exhaustion_after_context_uses_context_shas(offline, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise BudgetExceededError("budget spent on alerts")
    monkeypatch.setattr(cli, "collect_secret_alerts", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, "--head-sha", HEAD, "--base-sha", BASE) == 2
    (event,) = _lines(out)
    ctx = offline["context"]
    # Collected context outranks the event payload: it is what was reviewed.
    _assert_failed(event, "github_api_budget_exceeded", head=ctx.head_sha, base=ctx.base_sha)
    # No freshness gate ran yet: freshness is unknown, never assumed.
    assert "stale_state" not in event


def test_github_api_failure_emits_failed_event(offline, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise GitHubApiError("502")
    monkeypatch.setattr(cli, "collect_context", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, "--head-sha", HEAD, "--base-sha", BASE) == 2
    (event,) = _lines(out)
    _assert_failed(event, "github_api_failure", head=HEAD, base=BASE)


def test_unexpected_exception_is_internal_error_exit_2(offline, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("validator crashed")
    monkeypatch.setattr(cli, "evaluate", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out) == 2
    (event,) = _lines(out)
    ctx = offline["context"]
    _assert_failed(event, "internal_error", head=ctx.head_sha, base=ctx.base_sha)


def test_failure_after_decision_copies_decision_verbatim(offline, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PublicationError("check run 403")
    monkeypatch.setattr(cli, "publish_check_run", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, publish=True) == 2
    (event,) = _lines(out)
    decision = json.loads((out / "decision.json").read_text())
    assert validation_errors("review-telemetry-event", event) == []
    assert event["final_status"] == "failed"
    assert event["failure_mode"] == "publication_failure"
    for key in ("contract_version", "risk_class", "approval_authorized", "merge_authorized",
                "founder_review_required", "blocking_reasons", "head_sha", "base_sha"):
        assert event[key] == decision[key], key
    assert event["stale_state"] == "fresh"


def test_missing_token_emits_failed_event(offline, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    out = tmp_path / "a"
    assert _shadow(offline, out, "--head-sha", HEAD, "--base-sha", BASE) == 2
    (event,) = _lines(out)
    _assert_failed(event, "github_token_missing", head=HEAD, base=BASE)
    assert event["github_requests_used"] == 0


def test_invalid_config_emits_failed_event(offline, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ConfigurationError("pr_limits must be positive")
    monkeypatch.setattr(cli, "load_config", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, "--config", "x.yml", "--head-sha", HEAD, "--base-sha", BASE) == 2
    (event,) = _lines(out)
    _assert_failed(event, "configuration_invalid", head=HEAD, base=BASE)


def test_no_event_when_head_unknown_but_still_exit_2(offline, tmp_path, monkeypatch, capsys):
    """head_sha/base_sha are required by the contract. With no context and
    no event-payload SHAs nothing schema-valid can be written: the run still
    fails closed (exit 2) and says why."""
    def boom(*a, **k):
        raise BudgetExceededError("budget 0/0")
    monkeypatch.setattr(cli, "collect_context", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out) == 2
    assert _lines(out) == []
    assert "no failure telemetry" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["abc", "G" * 40, "1" * 39])
def test_malformed_payload_sha_is_never_written(offline, tmp_path, monkeypatch, capsys, bad):
    def boom(*a, **k):
        raise BudgetExceededError("budget 0/0")
    monkeypatch.setattr(cli, "collect_context", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, "--head-sha", bad, "--base-sha", BASE) == 2
    assert _lines(out) == []
    # Rejected as unknown identity up front, not left to the schema backstop.
    assert "head/base SHA unknown" in capsys.readouterr().err


def test_failure_after_append_never_writes_a_second_event(offline, tmp_path, monkeypatch):
    """P1: at most one record per execution."""
    def boom(*a, **k):
        raise RuntimeError("late crash")
    monkeypatch.setattr(cli, "evidence_fingerprint", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out) == 2
    (event,) = _lines(out)
    assert event["final_status"] == "completed"


def test_completed_run_is_unchanged(offline, tmp_path):
    out = tmp_path / "a"
    assert _shadow(offline, out, "--head-sha", HEAD, "--base-sha", BASE) == 0
    (event,) = _lines(out)
    assert event["final_status"] == "completed" and "failure_mode" not in event
    # Payload SHAs are a fallback only: the reviewed context wins.
    assert event["head_sha"] == offline["context"].head_sha


# ------------------------------------------------------------ record-failure


def _record(out: Path, *extra: str) -> int:
    return cli.main(["record-failure", "--repository", "faraday-build/pilot", "--pr", "7",
                     "--head-sha", HEAD, "--base-sha", BASE,
                     "--run-id", "43507102983", "--run-attempt", "2",
                     "--artifacts-dir", str(out), *extra])


@pytest.mark.parametrize("mode", ["workflow_step_failed", "workflow_step_cancelled"])
def test_record_failure_writes_when_step_left_no_telemetry(tmp_path, mode):
    out = tmp_path / "a"
    assert _record(out, "--failure-mode", mode) == 0
    (event,) = _lines(out)
    _assert_failed(event, mode, head=HEAD, base=BASE)
    assert event["repository"] == "faraday-build/pilot" and event["pr_number"] == 7


def test_record_failure_is_a_noop_when_telemetry_exists(offline, tmp_path):
    out = tmp_path / "a"
    assert _shadow(offline, out) == 0
    before = (out / "telemetry.jsonl").read_bytes()
    assert _record(out, "--failure-mode", "workflow_step_failed") == 0
    assert (out / "telemetry.jsonl").read_bytes() == before


def test_record_failure_rejects_unknown_mode(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _record(tmp_path / "a", "--failure-mode", "github_api_budget_exceeded")
    assert exc.value.code == 2


@pytest.mark.parametrize("flag,value", [("--head-sha", "abc"), ("--base-sha", "x" * 40),
                                        ("--repository", "not a repo")])
def test_record_failure_rejects_invalid_identity(tmp_path, capsys, flag, value):
    argv = ["record-failure", "--repository", "faraday-build/pilot", "--pr", "7",
            "--head-sha", HEAD, "--base-sha", BASE, "--run-id", "1", "--run-attempt", "1",
            "--failure-mode", "workflow_step_failed", "--artifacts-dir", str(tmp_path / "a")]
    argv[argv.index(flag) + 1] = value
    assert cli.main(argv) == 2
    assert _lines(tmp_path / "a") == []
    assert "record-failure: invalid" in capsys.readouterr().err


# ------------------------------------------------------------ consumer visibility


def _ledger_and_readiness(tmp_path: Path, archive: Path) -> tuple[dict, dict]:
    ledger_path, ready_path = tmp_path / "ledger.json", tmp_path / "readiness.json"
    synthetic = tmp_path / "synthetic.json"
    synthetic.write_text("[]\n")
    assert cli.main(["aggregate", "--input-dir", str(archive), "--output", str(ledger_path),
                     "--generated-at", "2026-11-14T00:00:00Z"]) == 0
    assert cli.main(["readiness", "--ledger", str(ledger_path), "--synthetic", str(synthetic),
                     "--generated-at", "2026-11-14T00:00:00Z", "--output", str(ready_path)]) == 0
    return json.loads(ledger_path.read_text()), json.loads(ready_path.read_text())


def test_budget_failure_reaches_api_budget_tallies(offline, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise BudgetExceededError("budget 0/0")
    monkeypatch.setattr(cli, "collect_context", boom)
    archive = tmp_path / "archive"
    assert _shadow(offline, archive / "run-1", "--head-sha", HEAD, "--base-sha", BASE) == 2
    ledger, ready = _ledger_and_readiness(tmp_path, archive)
    infra = ready["drift"]["infrastructure_failures"]
    assert infra["failed_runs"] == 1
    assert infra["api_budget_exceeded_runs"] == 1
    assert infra["failure_modes"] == {"github_api_budget_exceeded": 1}
    assert infra["status"] == "exact"
    # A failed run never qualifies.
    assert ready["real_pr_count"] == 0


def test_failed_then_completed_run_keeps_the_failure(offline, tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    monkeypatch.setattr(cli, "run_gitleaks", lambda *a, **k: 1)
    assert _shadow(offline, archive / "a1", "--repo-dir", str(tmp_path)) == 2
    monkeypatch.setattr(cli, "run_gitleaks", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "parse_gitleaks_report", lambda *a, **k: [])
    assert cli.main(["shadow-run", "--repository", offline["repo"], "--pr", str(offline["pr"]),
                     "--policy-bundle", str(offline["bundle"]),
                     "--expected-policy-digest", offline["digest"],
                     "--artifacts-dir", str(archive / "a2"), "--repo-dir", str(tmp_path),
                     "--run-id", "43507102983", "--run-attempt", "3", "--skip-publish"]) == 0
    _, ready = _ledger_and_readiness(tmp_path, archive)
    infra = ready["drift"]["infrastructure_failures"]
    assert infra["failed_runs"] == 1
    assert infra["failure_modes"] == {"gitleaks_error": 1}
    assert ready["real_pr_count"] == 1


# ------------------------------------------------------------ Codex round 1 (f58cd77)


LONG_REPO = "faraday-capital-systems/faraday-architecture-registry"


def test_review_id_bounded_for_long_repository_names(tmp_path):
    """Codex P2 #8 cli.py:204: the review_id must fit the schema for every
    valid repository name, or the failure record is dropped."""
    out = tmp_path / "a"
    assert cli.main(["record-failure", "--repository", LONG_REPO, "--pr", "1234567",
                     "--head-sha", HEAD, "--base-sha", BASE, "--run-id", "1",
                     "--run-attempt", "1", "--failure-mode", "workflow_step_failed",
                     "--artifacts-dir", str(out)]) == 0
    (event,) = _lines(out)
    assert validation_errors("review-telemetry-event", event) == []
    assert len(event["review_id"]) <= 64


def test_review_id_short_form_unchanged_and_long_form_distinct():
    assert cli._review_id("faraday-build/pilot", 7, HEAD) == f"frr-faraday-build-pilot-7-{HEAD[:12]}"
    a = cli._review_id(LONG_REPO, 7, HEAD)
    b = cli._review_id(LONG_REPO + "s", 7, HEAD)
    c = cli._review_id(LONG_REPO, 8, HEAD)
    assert len({a, b, c}) == 3
    assert all(len(x) <= 64 for x in (a, b, c))
    assert a == cli._review_id(LONG_REPO, 7, HEAD)


def test_completed_run_with_long_repository_is_schema_valid(offline, tmp_path, monkeypatch):
    ctx = offline["context"]
    raw = json.loads((GOLDEN / "context.json").read_text(encoding="utf-8"))
    raw["contract_version"] = "v0.4.0"
    raw["repository"] = LONG_REPO
    long_ctx = PullRequestContext.from_json(raw)
    monkeypatch.setattr(cli, "collect_context", lambda *a, **k: long_ctx)
    out = tmp_path / "a"
    assert cli.main(["shadow-run", "--repository", LONG_REPO, "--pr", str(offline["pr"]),
                     "--policy-bundle", str(offline["bundle"]),
                     "--expected-policy-digest", offline["digest"],
                     "--artifacts-dir", str(out), "--skip-publish"]) == 0
    (event,) = _lines(out)
    assert validation_errors("review-telemetry-event", event) == []
    assert ctx.head_sha == event["head_sha"]


def test_record_failure_copies_an_existing_decision(offline, tmp_path, monkeypatch):
    """Codex P2 #8 cli.py:360: a step killed after decision.json was written
    must still carry the Validator's fields verbatim."""
    def boom(*a, **k):
        raise PublicationError("killed mid-publication")
    monkeypatch.setattr(cli, "publish_check_run", boom)
    out = tmp_path / "a"
    assert _shadow(offline, out, publish=True) == 2
    decision = json.loads((out / "decision.json").read_text())
    (out / "telemetry.jsonl").unlink()  # as if the process died before appending
    assert cli.main(["record-failure", "--repository", offline["repo"], "--pr", str(offline["pr"]),
                     "--head-sha", HEAD, "--base-sha", BASE, "--run-id", "43507102983",
                     "--run-attempt", "2", "--failure-mode", "workflow_step_cancelled",
                     "--artifacts-dir", str(out)]) == 0
    (event,) = _lines(out)
    assert validation_errors("review-telemetry-event", event) == []
    for key in ("contract_version", "risk_class", "approval_authorized", "merge_authorized",
                "founder_review_required", "blocking_reasons", "head_sha", "base_sha"):
        assert event[key] == decision[key], key
    assert event["failure_mode"] == "workflow_step_cancelled"


@pytest.mark.parametrize("tamper", [{"repository": "other/repo"}, {"pr_number": 999},
                                    "not-json", {"head_sha": "zz"}])
def test_record_failure_ignores_a_foreign_or_broken_decision(tmp_path, tamper):
    out = tmp_path / "a"
    out.mkdir()
    decision = {"repository": "faraday-build/pilot", "pr_number": 7, "head_sha": HEAD,
                "base_sha": BASE, "risk_class": "green", "approval_authorized": False,
                "merge_authorized": False, "founder_review_required": False,
                "contract_version": "v0.4.0", "blocking_reasons": []}
    if isinstance(tamper, dict):
        decision.update(tamper)
        (out / "decision.json").write_text(json.dumps(decision))
    else:
        (out / "decision.json").write_text(tamper)
    assert _record(out, "--failure-mode", "workflow_step_failed") == 0
    (event,) = _lines(out)
    _assert_failed(event, "workflow_step_failed", head=HEAD, base=BASE)
    assert "risk_class" not in event


def test_decision_captured_before_outputs_are_written(offline, tmp_path, monkeypatch):
    """Codex P2 #8 cli.py:320: a failure while writing the Validator's
    outputs must still copy the decision it produced."""
    real = cli.write_canonical

    def flaky(path, obj):
        if Path(path).name == "merge-authorization.json":
            raise OSError("disk full")
        return real(path, obj)
    monkeypatch.setattr(cli, "write_canonical", flaky)
    out = tmp_path / "a"
    assert _shadow(offline, out) == 2
    (event,) = _lines(out)
    decision = json.loads((out / "decision.json").read_text())
    assert event["failure_mode"] == "internal_error"
    for key in ("risk_class", "blocking_reasons", "founder_review_required"):
        assert event[key] == decision[key], key
