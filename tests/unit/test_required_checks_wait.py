"""S6 F-1: stable required-check evaluation.

The shadow job and the caller's required checks start on the same
pull_request event, so at first observation `required-checks` was `pending`
and every first attempt Blocked `required_check_missing` (S6 retry,
2026-11-15). shadow-run now waits, bounded and fail closed, until every
policy-required context is final:

- success -> classify; failure/error -> classify at once
  (required_check_failing);
- no run at all after the missing grace -> classify (required_check_missing);
- still pending at the deadline -> NOT classified: one failed telemetry
  record (required_checks_pending_timeout), exit 2, never a Block;
- the head moving while waiting -> stale (existing freshness semantics);
- a poll that would starve the rest of the run -> github_api_budget_exceeded;
- cancellation -> the process dies, record-failure records the attempt.

These tests drive the real `faraday-review shadow-run` command through the
real GitHubClient against an in-memory GitHub; only time is simulated.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from faraday_review import cli
from faraday_review.config import (
    PENDING_TIMEOUT_MAX_SECONDS,
    RequiredChecksWaitConfig,
    default_config,
)
from faraday_review.evidence.schema_validation import validation_errors
from fake_github import FakeSession

GOLDEN = Path(__file__).resolve().parents[1] / "determinism" / "golden" / "green-docs"
CONTEXT = json.loads((GOLDEN / "context.json").read_text(encoding="utf-8"))
REPO = CONTEXT["repository"]
PR = CONTEXT["pr_number"]
HEAD = CONTEXT["head_sha"]
BASE = CONTEXT["base_sha"]
NEW_HEAD = "9" * 40
RUN_ID, RUN_ATTEMPT = 43664575693, 1
CHECK = "required-checks"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _run(state: str, name: str = CHECK, rid: int = 1) -> dict:
    if state == "pending":
        return {"id": rid, "name": name, "status": "in_progress", "conclusion": None}
    return {"id": rid, "name": name, "status": "completed", "conclusion": state}


class Gh:
    """In-memory GitHub for one PR. `checks` is a script: the n-th
    check-runs listing returns checks[min(n, last)] (a list of runs);
    `heads` likewise scripts the PR head."""

    def __init__(self, checks: list[list[dict]], heads: list[str] | None = None) -> None:
        self.checks = checks
        self.heads = heads or [HEAD]
        self.check_calls = 0
        self.head_calls = 0
        routes = {
            f"/repos/{REPO}/pulls/{PR}": self._pr,
            f"/repos/{REPO}/pulls/{PR}/files": (200, [
                {"filename": "docs/RUNBOOK.md", "status": "modified", "patch": "x" * 10},
                {"filename": "README.md", "status": "modified", "patch": "y" * 10}]),
            f"/repos/{REPO}/commits/{HEAD}/check-runs": self._checks,
            f"/repos/{REPO}/commits/{HEAD}/status": (200, {"state": "pending", "statuses": []}),
            f"/repos/{REPO}/check-runs": (201, {"id": 777}),
            f"/repos/{REPO}/issues/{PR}/comments": self._comments,
        }
        self.session = FakeSession(routes)

    def _pr(self, method, kwargs):
        head = self.heads[min(self.head_calls, len(self.heads) - 1)]
        self.head_calls += 1
        return (200, {"head": {"sha": head, "ref": "change"}, "base": {"sha": BASE, "ref": "main"},
                      "draft": False})

    def _checks(self, method, kwargs):
        runs = self.checks[min(self.check_calls, len(self.checks) - 1)]
        self.check_calls += 1
        return (200, {"total_count": len(runs), "check_runs": runs})

    def _comments(self, method, kwargs):
        return (200, []) if method == "GET" else (201, {"id": 888})

    def posts(self) -> list:
        return [(p, body) for m, p, _, body in self.session.sent if m in ("POST", "PATCH")]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(cli, "_wait_clock", clock.time)
    monkeypatch.setattr(cli, "_wait_sleep", clock.sleep)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-secret")
    bundle = tmp_path / "bundle"
    shutil.copytree(GOLDEN / "policy-bundle", bundle)
    digest = json.loads((GOLDEN / "expected-decision.json").read_text())["policy_digest"]
    return {"clock": clock, "bundle": bundle, "digest": digest, "tmp": tmp_path,
            "monkeypatch": monkeypatch}


def _shadow(env, gh: Gh, *extra: str, publish: bool = False, checks=(CHECK,)) -> tuple[int, Path]:
    env["monkeypatch"].setattr(cli, "_new_session", lambda: gh.session)
    out = env["tmp"] / "artifacts"
    argv = ["shadow-run", "--repository", REPO, "--pr", str(PR),
            "--policy-bundle", str(env["bundle"]), "--expected-policy-digest", env["digest"],
            "--artifacts-dir", str(out), "--run-id", str(RUN_ID), "--run-attempt", str(RUN_ATTEMPT),
            "--head-sha", HEAD, "--base-sha", BASE, *extra]
    for check in checks:
        argv += ["--required-check", check]
    if not publish:
        argv.append("--skip-publish")
    return cli.main(argv), out


def _event(out: Path) -> dict:
    (line,) = (out / "telemetry.jsonl").read_text().splitlines()
    event = json.loads(line)
    assert validation_errors("review-telemetry-event", event) == []
    return event


def _decision(out: Path) -> dict:
    return json.loads((out / "decision.json").read_text())


# ------------------------------------------------------------ classification


def test_pending_then_success_waits_and_classifies_normally(env):
    gh = Gh([[_run("pending")], [_run("pending")], [_run("success")]])
    code, out = _shadow(env, gh)
    assert code == 0
    decision = _decision(out)
    assert decision["risk_class"] == "green" and decision["blocking_reasons"] == []
    event = _event(out)
    assert event["final_status"] == "completed" and event["stale_state"] == "fresh"
    assert env["clock"].sleeps == [10.0, 20.0]  # backoff 10 -> 20
    assert gh.check_calls == 3


def test_already_final_checks_do_not_wait_and_cost_what_they_did(env):
    gh = Gh([[_run("success")]])
    code, out = _shadow(env, gh)
    assert code == 0 and _decision(out)["risk_class"] == "green"
    assert env["clock"].sleeps == [] and gh.check_calls == 1
    # PR (collect) + files + check-runs + status + secret alerts + 2 head refetches.
    assert _event(out)["github_requests_used"] == 7


def test_no_required_checks_never_waits(env):
    gh = Gh([[]])
    code, out = _shadow(env, gh, checks=())
    assert code == 0 and env["clock"].sleeps == []


def test_pending_until_deadline_is_failure_telemetry_not_block(env):
    gh = Gh([[_run("pending")]])
    code, out = _shadow(env, gh, publish=True)
    assert code == 2
    event = _event(out)
    assert event["final_status"] == "failed"
    assert event["failure_mode"] == "required_checks_pending_timeout"
    assert "risk_class" not in event and "blocking_reasons" not in event
    assert event["approval_authorized"] is False and event["merge_authorized"] is False
    assert not (out / "decision.json").exists()
    # Never classified, so nothing governed was published.
    assert gh.posts() == []
    clock = env["clock"]
    assert clock.now == 900.0  # the deadline exactly, never beyond
    assert all(1.0 <= s <= 60.0 for s in clock.sleeps)
    assert clock.sleeps[:4] == [10.0, 20.0, 40.0, 60.0]


def test_pending_timeout_is_configurable(env):
    gh = Gh([[_run("pending")]])
    code, out = _shadow(env, gh, "--checks-pending-timeout", "45", "--checks-missing-grace", "5")
    assert code == 2 and _event(out)["failure_mode"] == "required_checks_pending_timeout"
    assert env["clock"].now == 45.0


def test_missing_after_grace_classifies_required_check_missing(env):
    gh = Gh([[]])  # the required context never gets a run
    code, out = _shadow(env, gh)
    assert code == 0
    decision = _decision(out)
    assert decision["risk_class"] == "block"
    assert decision["blocking_reasons"] == ["required_check_missing"]
    assert _event(out)["final_status"] == "completed"
    assert env["clock"].now == 300.0  # the grace, not the pending deadline


def test_missing_that_appears_within_grace_is_classified_on_its_result(env):
    gh = Gh([[], [_run("pending")], [_run("success")]])
    code, out = _shadow(env, gh)
    assert code == 0 and _decision(out)["risk_class"] == "green"


def test_missing_beside_pending_waits_for_the_pending_one(env):
    gh = Gh([[_run("pending")]] * 30 + [[_run("success")]])
    code, out = _shadow(env, gh, checks=(CHECK, "never-runs"))
    # 'never-runs' passed its grace long ago, but required-checks was still
    # running: the wait continues to the pending deadline and ends failed.
    assert code == 2 and _event(out)["failure_mode"] == "required_checks_pending_timeout"


def test_missing_beside_resolved_check_is_missing_after_grace(env):
    gh = Gh([[_run("pending")], [_run("success")]])
    code, out = _shadow(env, gh, checks=(CHECK, "never-runs"))
    assert code == 0
    assert _decision(out)["blocking_reasons"] == ["required_check_missing"]
    assert env["clock"].now == 300.0


def test_failed_check_ends_the_wait_immediately(env):
    gh = Gh([[_run("failure")]])
    code, out = _shadow(env, gh)
    assert code == 0
    assert _decision(out)["blocking_reasons"] == ["required_check_failing"]
    assert env["clock"].sleeps == []


def test_failure_during_wait_ends_it_with_required_check_failing(env):
    gh = Gh([[_run("pending")], [_run("pending"), _run("timed_out", name="other", rid=2)]])
    code, out = _shadow(env, gh, checks=(CHECK, "other"))
    assert code == 0
    # The Block is decided by the failure; the still-running check is not
    # waited for (it reads as missing beside it, both blocking).
    assert "required_check_failing" in _decision(out)["blocking_reasons"]
    assert env["clock"].sleeps == [10.0]


def test_rerun_of_a_check_during_wait_uses_the_best_state(env):
    # A failed run beside its in-progress re-run is pending (best state):
    # the wait continues and the green re-run is what gets classified.
    gh = Gh([[_run("failure", rid=1), _run("pending", rid=2)],
             [_run("failure", rid=1), _run("success", rid=2)]])
    code, out = _shadow(env, gh)
    assert code == 0
    assert _decision(out)["risk_class"] == "green"
    assert env["clock"].sleeps == [10.0]


# ------------------------------------------------------------ head, budget, cancel


def test_head_moving_during_wait_ends_stale(env):
    # collect_context sees HEAD; the wait's head check sees NEW_HEAD.
    gh = Gh([[_run("pending")]], heads=[HEAD, NEW_HEAD])
    code, out = _shadow(env, gh)
    assert code == 0
    event = _event(out)
    # Existing freshness semantics: stale before validation, and the
    # publication gate sees the moved head too.
    assert event["stale_state"] in ("stale_before_validation", "stale_before_publication")
    assert "evidence_stale" in _decision(out)["blocking_reasons"]
    assert env["clock"].sleeps == [10.0]  # stopped waiting at once


def test_head_moving_and_back_during_wait_is_still_stale(env):
    gh = Gh([[_run("pending")], [_run("success")]], heads=[HEAD, NEW_HEAD, HEAD])
    code, out = _shadow(env, gh)
    assert code == 0
    assert _event(out)["stale_state"] == "stale_before_validation"
    assert "evidence_stale" in _decision(out)["blocking_reasons"]


def test_wait_head_check_spends_no_head_refetch_slot(env):
    """The two freshness gates keep their head-refetch slots
    (max_head_refetches=3): many polls must not exhaust them."""
    gh = Gh([[_run("pending")]] * 8 + [[_run("success")]])
    code, out = _shadow(env, gh)
    assert code == 0 and _decision(out)["risk_class"] == "green"
    assert len(env["clock"].sleeps) == 8


def test_polling_never_exhausts_the_api_budget(env):
    cfg = env["tmp"] / "config.yaml"
    cfg.write_text("github_api:\n  max_requests_per_run: 40\n")
    gh = Gh([[_run("pending")]])
    code, out = _shadow(env, gh, "--config", str(cfg))
    assert code == 2
    event = _event(out)
    assert event["failure_mode"] == "github_api_budget_exceeded"
    reserve = default_config().worst_case_requests()
    # Stopped before a poll could eat into the reserve for the rest of the run.
    assert event["github_requests_used"] + reserve <= 40
    assert env["clock"].now < 900.0


def test_cancellation_during_wait_is_recorded_by_record_failure(env):
    """GitHub cancels a job by signalling the step (SIGINT ->
    KeyboardInterrupt). shadow-run does not swallow it, so it leaves no
    telemetry; the always() record-failure step then records the attempt
    as workflow_step_cancelled, and publish-attempt-record makes it durable."""
    def cancelled(_seconds):
        raise KeyboardInterrupt

    env["monkeypatch"].setattr(cli, "_wait_sleep", cancelled)
    gh = Gh([[_run("pending")]])
    with pytest.raises(KeyboardInterrupt):
        _shadow(env, gh)
    out = env["tmp"] / "artifacts"
    assert not (out / "telemetry.jsonl").exists()
    code = cli.main(["record-failure", "--repository", REPO, "--pr", str(PR),
                     "--head-sha", HEAD, "--base-sha", BASE, "--run-id", str(RUN_ID),
                     "--run-attempt", str(RUN_ATTEMPT), "--failure-mode",
                     "workflow_step_cancelled", "--artifacts-dir", str(out)])
    assert code == 0
    event = _event(out)
    assert event["final_status"] == "failed" and event["failure_mode"] == "workflow_step_cancelled"
    code = cli.main(["publish-attempt-record", "--repository", REPO, "--pr", str(PR),
                     "--head-sha", HEAD, "--run-id", str(RUN_ID), "--run-attempt",
                     str(RUN_ATTEMPT), "--artifacts-dir", str(out)])
    assert code == 0
    ((path, body),) = gh.posts()
    assert body["external_id"] == f"faraday-review-attempt:{RUN_ID}:{RUN_ATTEMPT}"
    assert "workflow_step_cancelled" in body["output"]["text"]


# ------------------------------------------------------------ configuration


@pytest.mark.parametrize("flags", [
    ("--checks-pending-timeout", str(PENDING_TIMEOUT_MAX_SECONDS + 1)),
    ("--checks-missing-grace", "901"),
    ("--checks-pending-timeout", "1800", "--checks-missing-grace", "901"),
    ("--checks-pending-timeout", "60", "--checks-missing-grace", "120"),
])
def test_out_of_bounds_wait_is_a_configuration_failure(env, flags):
    gh = Gh([[_run("success")]])
    code, out = _shadow(env, gh, *flags)
    assert code == 2
    assert _event(out)["failure_mode"] == "configuration_invalid"
    assert gh.check_calls == 0


def test_wait_bounds_load_from_config_file(env):
    cfg = env["tmp"] / "config.yaml"
    cfg.write_text("required_checks_wait:\n  pending_timeout_seconds: 30\n"
                   "  missing_grace_seconds: 10\n  poll_initial_seconds: 5\n"
                   "  poll_max_seconds: 5\n")
    gh = Gh([[_run("pending")]])
    code, out = _shadow(env, gh, "--config", str(cfg))
    assert code == 2 and _event(out)["failure_mode"] == "required_checks_pending_timeout"
    assert env["clock"].sleeps == [5.0] * 6


def test_wait_defaults():
    wait = default_config().required_checks_wait
    assert wait == RequiredChecksWaitConfig(300, 900, 10.0, 60.0)
