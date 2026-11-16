"""Static contract tests for the reusable qualification monitor workflow
(GHO-AUTONOMOUS-PRODUCTION-010 M1).

They pin .github/workflows/qualification-monitor.yml so a later edit cannot
silently widen it: reusable-only trigger, no secrets, exact least-privilege
permissions per job (issues: write only on the status-issue job), SHA-pinned
actions, reviewer binding verified before reviewer code is checked out, the
caller proven public AND enrolled before anything is collected, no
pull-request / contents / variable / secret writes, one deduplicated status
issue, evidence retained 90 days, CRITICAL fails the run.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "qualification-monitor.yml"
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w.-]+)*@[0-9a-f]{40}$")
MONITOR_PERMS = {"contents": "read", "actions": "read", "checks": "read", "pull-requests": "read"}
ISSUE_PERMS = {"contents": "read", "issues": "write"}


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))


def _jobs() -> dict:
    return _doc()["jobs"]


def _steps(job: str) -> list[dict]:
    return _jobs()[job]["steps"]


def _index(job: str, fragment: str) -> int:
    for i, step in enumerate(_steps(job)):
        if fragment in step.get("name", ""):
            return i
    raise AssertionError(f"no step in {job} named like {fragment!r}")


def _runs(job: str | None = None) -> list[str]:
    jobs = [job] if job else list(_jobs())
    return [s["run"] for j in jobs for s in _steps(j) if isinstance(s.get("run"), str)]


def test_only_workflow_call_trigger():
    assert list(_triggers(_doc())) == ["workflow_call"]


def test_inputs_contract():
    inputs = _triggers(_doc())["workflow_call"]["inputs"]
    assert set(inputs) == {"reviewer_sha", "max_requests", "stall_hours"}
    assert inputs["reviewer_sha"]["required"] is True
    assert inputs["max_requests"]["type"] == "number"


def test_declares_and_uses_no_secrets():
    text = WORKFLOW.read_text()
    assert "secrets" not in _triggers(_doc())["workflow_call"]
    assert "secrets." not in text
    assert "inherit" not in text


def test_permissions_exact():
    doc = _doc()
    assert doc["permissions"] == {"contents": "read"}
    assert set(_jobs()) == {"monitor", "status-issue"}
    assert _jobs()["monitor"]["permissions"] == MONITOR_PERMS
    assert _jobs()["status-issue"]["permissions"] == ISSUE_PERMS


def test_issues_write_only_on_status_issue_job():
    for name, job in _jobs().items():
        perms = job.get("permissions") or {}
        if name != "status-issue":
            assert "issues" not in perms
            assert all(v != "write" for v in perms.values()), name


def test_all_actions_sha_pinned():
    for job in _jobs():
        for step in _steps(job):
            if "uses" in step:
                assert SHA_PIN.match(step["uses"]), step["uses"]


def test_every_job_has_timeout_and_concurrency():
    for name, job in _jobs().items():
        assert isinstance(job.get("timeout-minutes"), int), name
        assert "concurrency" in job, name
        assert job["concurrency"].get("cancel-in-progress") is False, name


def test_event_restricted_to_schedule_and_dispatch():
    run = _steps("monitor")[_index("monitor", "Validate event")]["run"]
    assert "schedule" in run and "workflow_dispatch" in run
    assert "exit 2" in run


def test_binding_verified_before_reviewer_checkout():
    bind = _index("monitor", "Verify reviewer SHA binding")
    checkout = _index("monitor", "Check out reviewer")
    assert bind < checkout
    run = _steps("monitor")[bind]["run"]
    assert "/.github/workflows/qualification-monitor.yml@" in run
    assert "referenced_workflows" in run
    assert 'compare/$REVIEWER_SHA...main' in run
    step = _steps("monitor")[checkout]
    assert step["with"]["ref"] == "${{ inputs.reviewer_sha }}"
    assert step["with"]["persist-credentials"] is False


def test_public_and_enrolled_precheck_before_collect():
    pre = _index("monitor", "Verify caller is public and enrolled")
    collect = _index("monitor", "Collect")
    assert pre < collect
    run = _steps("monitor")[pre]["run"]
    assert 'gh api "repos/$CALLER_REPO"' in run
    assert ".visibility" in run and ".private" in run
    assert 'if [ "$observed" != "public false" ]; then\n' in run
    assert run.index('"public false" ]; then') < run.index("exit 2")
    assert ".faraday-review/docs/qualification/enrollment.json" in run
    assert ".faraday-review/docs/qualification/synthetic-prs.json" in run
    assert 'get("visibility") != "public"' in run
    assert "exit 2" in run and "sys.exit(2)" in run
    # No step before the pre-check touches runs, artifacts or archives.
    for step in _steps("monitor")[:pre]:
        assert "faraday-review collect" not in step.get("run", "")


def test_collect_uses_single_entry_enrollment_and_job_token():
    step = _steps("monitor")[_index("monitor", "Collect")]
    run = step["run"]
    assert "faraday-review collect" in run
    assert '--enrollment "$W/enrollment.json"' in run
    assert "--max-requests" in run
    assert "--private-archive-dir" not in WORKFLOW.read_text()
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"


def test_pipeline_order_and_fresh_aggregate():
    order = [_index("monitor", n) for n in ("Collect", "Aggregate and readiness",
                                            "Qualification status report", "Upload", "Fail on CRITICAL")]
    assert order == sorted(order)
    run = _steps("monitor")[_index("monitor", "Aggregate and readiness")]["run"]
    assert "--prior-ledger" not in run
    assert '--synthetic "$W/synthetic-prs.json"' in run


def test_report_writes_step_summary_and_flags_critical():
    run = _steps("monitor")[_index("monitor", "Qualification status report")]["run"]
    assert "faraday-review monitor-report" in run
    assert '--repository "$CALLER_REPO"' in run
    assert "$GITHUB_STEP_SUMMARY" in run
    assert "3) crit=true" in run
    fail = _steps("monitor")[_index("monitor", "Fail on CRITICAL")]
    assert fail["if"] == "steps.report.outputs.critical == 'true'"
    assert "exit 1" in fail["run"]
    assert "gh variable delete FARADAY_REVIEW_SHADOW_ENABLED" in fail["run"]


def test_evidence_retained_90_days():
    step = _steps("monitor")[_index("monitor", "Upload")]
    # Never publish an input set monitor-report refused (Codex P1, #17).
    assert step["if"] == "always() && steps.report.outputs.ready == 'true'"
    assert step["with"]["retention-days"] == 90


def test_status_issue_runs_after_report_even_on_critical():
    job = _jobs()["status-issue"]
    assert job["needs"] == "monitor"
    assert job["if"] == "always() && needs.monitor.outputs.report_ready == 'true'"


def test_dedupe_logic_present():
    run = _steps("status-issue")[_index("status-issue", "Update status issue")]["run"]
    assert 'TITLE="Faraday Review qualification status"' in run
    assert "LABEL=faraday-review-qualification" in run
    assert "labels=$LABEL" in run and "creator=github-actions%5Bbot%5D" in run
    assert 'i.get("pull_request") is None' in run and 'i.get("title") == t' in run
    assert '"github-actions[bot]"' in run
    assert "faraday-review monitor-notify" in run and "--previous-body" in run
    assert '-X PATCH "repos/$CALLER_REPO/issues/$number"' in run
    # A comment only when monitor-notify asked for one.
    assert 'if [ -s "$W/comment.md" ]' in run
    assert run.index("monitor-notify") < run.index("issues/$number/comments")
    # The marker advances only after the comment was posted (Codex P2, #17).
    assert run.index("issues/$number/comments") < run.index('-X PATCH "repos/$CALLER_REPO/issues/$number"')


def test_only_allowed_write_endpoints():
    allowed = {'-X POST "repos/$CALLER_REPO/labels"', '-X POST "repos/$CALLER_REPO/issues"',
               '-X PATCH "repos/$CALLER_REPO/issues/$number"',
               '-X POST "repos/$CALLER_REPO/issues/$number/comments"'}
    found = set()
    for job in _jobs():
        for run in _runs(job):
            for m in re.finditer(r'-X (POST|PATCH|PUT|DELETE) "[^"]+"', run):
                found.add(m.group(0))
                assert job == "status-issue", m.group(0)
            assert "--method" not in run
    assert found == allowed


def test_no_pr_contents_variable_or_secret_writes():
    forbidden = [r"\bgh pr\b", r"/pulls\b", r"/merge\b", r"/reviews\b", r"\bapprove",
                 r"/variables\b", r"\bgh variable (set|delete)\b(?![^\n]*echo)", r"/secrets\b",
                 r"\bgh secret\b", r"/contents\b", r"\bgit push\b", r"--add-label",
                 r"issues/[^\s\"]*/labels", r"\bgh issue (edit|close|lock)\b"]
    for job in _jobs():
        for run in _runs(job):
            for line in run.splitlines():
                if line.strip().startswith("echo"):
                    continue  # kill-switch instruction text only
                for pattern in forbidden:
                    assert not re.search(pattern, line), (job, pattern, line)


def test_no_expression_interpolation_in_shell():
    for run in _runs():
        assert "${{" not in run, run[:80]


def test_hardening_guard_accepts_workflow():
    proc = subprocess.run([sys.executable, "tools/guards/workflow_hardening.py", ".github/workflows"],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_hardening_guard_still_rejects_issues_write_elsewhere(tmp_path):
    wf = tmp_path / "wf"
    wf.mkdir()
    text = WORKFLOW.read_text()
    # Same workflow, but issues: write granted to the monitor job: refused.
    bad = text.replace("      pull-requests: read\n", "      pull-requests: read\n      issues: write\n", 1)
    assert bad != text
    (wf / "qualification-monitor.yml").write_text(bad)
    proc = subprocess.run([sys.executable, str(ROOT / "tools/guards/workflow_hardening.py"), str(wf)],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "FW-P0-002" in proc.stderr
    # And any other workflow file granting issues: write is refused too.
    (wf / "qualification-monitor.yml").unlink()
    (wf / "other.yml").write_text(text)
    proc = subprocess.run([sys.executable, str(ROOT / "tools/guards/workflow_hardening.py"), str(wf)],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "FW-P0-002" in proc.stderr
    # The exception never covers write on any other scope in that job.
    bad2 = text.replace("      issues: write\n", "      issues: write\n      contents: write\n", 1)
    (wf / "other.yml").unlink()
    (wf / "qualification-monitor.yml").write_text(bad2.replace("contents: read\n      issues", "issues", 1))
    proc = subprocess.run([sys.executable, str(ROOT / "tools/guards/workflow_hardening.py"), str(wf)],
                          capture_output=True, text=True)
    assert proc.returncode == 1


# QA-1 (GHO-AUTONOMOUS-PRODUCTION-011, Q4): the proposed v5 step is
# read-only, non-blocking, and can never stop or alter the v4 status.

def test_v5_step_sits_between_readiness_and_report():
    order = [_index("monitor", n) for n in ("Aggregate and readiness",
                                            "QA-1 reference and proposed v5 gate (non-blocking)",
                                            "Qualification status report")]
    assert order == sorted(order)


def test_v5_step_is_non_blocking_and_cleans_up_on_failure():
    step = _steps("monitor")[_index("monitor", "QA-1 reference and proposed v5 gate (non-blocking)")]
    run = step["run"]
    assert step["id"] == "v5"
    assert "set -uo pipefail" in run and "set -e" not in run.replace("set -euo", "")
    assert "faraday-review reference" in run and "faraday-review qualification-v5" in run
    assert 'rm -f "$W/out/reference.json" "$W/out/qualification-v5.json"' in run
    assert 'echo "state=unavailable" >> "$GITHUB_OUTPUT"' in run
    assert step["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert '--enrollment "$W/enrollment.json"' in run
    assert "${{" not in run


def test_report_consumes_v5_via_env_and_drops_rejected_reports():
    step = _steps("monitor")[_index("monitor", "Qualification status report")]
    assert step["env"]["V5_STATE"] == "${{ steps.v5.outputs.state }}"
    run = step["run"]
    assert '--qualification-v5-unavailable reference_step_failed' in run
    assert '"${v5[@]}"' in run
    assert run.index('rm -f "$W/out/reference.json" "$W/out/qualification-v5.json"') < run.index('case "$rc" in')


def test_v5_step_is_wall_clock_bounded_and_reserves_time_for_v4():
    """Codex P2 r2 (#20): the optional reference/v5 commands run under
    `timeout`, so a slow or rate-limited collection cannot exhaust the
    job timeout before the v4 status report runs."""
    import re
    step = _steps("monitor")[_index("monitor", "QA-1 reference and proposed v5 gate (non-blocking)")]
    run = step["run"]
    bounds = []
    for cmd in ("faraday-review reference", "faraday-review qualification-v5"):
        m = re.search(r"timeout -k (\d+) (\d+) " + re.escape(cmd), run)
        assert m, f"{cmd} must run under timeout"
        bounds.append(int(m.group(1)) + int(m.group(2)))
    job_minutes = _jobs()["monitor"]["timeout-minutes"]
    assert sum(bounds) <= (job_minutes - 15) * 60, "at least 15 minutes must remain for v4"
    assert "continue-on-error" not in step
