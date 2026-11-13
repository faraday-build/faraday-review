"""Static contract tests for the reusable shadow workflow.

These pin the security properties of .github/workflows/shadow.yml so a
later edit cannot silently widen it: reusable-only trigger, no secrets,
guard-ceiling permissions, SHA-pinned actions, no expression interpolation
inside shell, binding verified before reviewer code is checked out, fork
PRs never elevated, evidence retained.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "shadow.yml"
CEILING = {
    "contents": "read",
    "pull-requests": "write",
    "checks": "write",
    "actions": "read",
}
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w.-]+)*@[0-9a-f]{40}$")


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers(doc: dict) -> dict:
    # PyYAML parses the bare key `on` as boolean True.
    return doc.get("on", doc.get(True))


def _steps() -> list[dict]:
    return _doc()["jobs"]["shadow"]["steps"]


def _index(name_fragment: str) -> int:
    for i, step in enumerate(_steps()):
        if name_fragment in step.get("name", ""):
            return i
    raise AssertionError(f"no step named like {name_fragment!r}")


def test_only_workflow_call_trigger():
    triggers = _triggers(_doc())
    assert list(triggers) == ["workflow_call"]


def test_declares_no_secrets():
    call = _triggers(_doc())["workflow_call"]
    assert "secrets" not in call
    assert "secrets." not in WORKFLOW.read_text()
    assert "inherit" not in WORKFLOW.read_text()


def test_inputs_contract():
    inputs = _triggers(_doc())["workflow_call"]["inputs"]
    assert set(inputs) == {"reviewer_sha", "governance_sha", "policy_digest", "required_checks"}
    assert inputs["reviewer_sha"]["required"] is True
    assert inputs["governance_sha"]["required"] is True
    # A missing digest must reach the CLI as empty => policy_digest_unpinned.
    assert inputs["policy_digest"]["default"] == ""


def test_permissions_exactly_ceiling():
    doc = _doc()
    assert doc["permissions"] == CEILING
    assert doc["jobs"]["shadow"]["permissions"] == CEILING


def test_all_actions_sha_pinned():
    for step in _steps():
        if "uses" in step:
            assert SHA_PIN.match(step["uses"]), step["uses"]


def test_no_expressions_inside_shell():
    for step in _steps():
        if "run" in step:
            assert "${{" not in step["run"], step.get("name")


def test_checkouts_do_not_persist_credentials():
    checkouts = [s for s in _steps() if s.get("uses", "").startswith("actions/checkout@")]
    assert len(checkouts) == 3
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


def test_rejects_non_pull_request_events():
    run = _steps()[_index("Validate event")]["run"]
    assert '"$EVENT_NAME" != "pull_request"' in run
    assert "pull_request_target" not in yaml.safe_dump(_triggers(_doc()))


def test_binding_verified_before_reviewer_checkout():
    assert _index("Validate event") < _index("Verify reviewer SHA binding")
    assert _index("Verify reviewer SHA binding") < _index("Check out reviewer")
    run = _steps()[_index("Verify reviewer SHA binding")]["run"]
    assert "referenced_workflows" in run
    assert "compare/$REVIEWER_SHA...main" in run


def test_reviewer_and_policy_checked_out_at_inputs():
    reviewer = _steps()[_index("Check out reviewer")]["with"]
    policy = _steps()[_index("Check out policy bundle")]["with"]
    assert reviewer["repository"] == "faraday-build/faraday-review"
    assert reviewer["ref"] == "${{ inputs.reviewer_sha }}"
    assert policy["repository"] == "faraday-capital-systems/faraday-governance"
    assert policy["ref"] == "${{ inputs.governance_sha }}"


def test_digest_only_from_input():
    env = _steps()[_index("Shadow review")]["env"]
    assert env["FARADAY_POLICY_DIGEST"] == "${{ inputs.policy_digest }}"
    assert "--expected-policy-digest" not in _steps()[_index("Shadow review")]["run"]


def test_fork_prs_skip_publication():
    run = _steps()[_index("Shadow review")]["run"]
    assert '"$HEAD_REPO" != "$CALLER_REPO"' in run
    assert "--skip-publish" in run


def test_gitleaks_checksum_verified():
    run = _steps()[_index("Install reviewer")]["run"]
    assert "sha256sum -c" in run
    assert re.fullmatch(r"[0-9a-f]{64}", _doc()["jobs"]["shadow"]["env"]["GITLEAKS_SHA256"])


def test_evidence_retained_for_qualification():
    upload = _steps()[_index("Upload shadow evidence")]
    assert upload["if"] == "always()"
    assert upload["with"]["retention-days"] == 90
    assert _steps()[_index("Record run binding")]["if"] == "always()"


def test_never_approves_or_merges():
    text = WORKFLOW.read_text()
    for forbidden in ("--approve", "gh pr merge", "/merge", "pulls/reviews", "contents: write"):
        assert forbidden not in text


def test_hardening_guard_passes():
    proc = subprocess.run(
        [sys.executable, "tools/guards/workflow_hardening.py", ".github/workflows"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
