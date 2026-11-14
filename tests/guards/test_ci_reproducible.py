"""GHO-004 P2 item 5: the qualification-reproducible CI job is read-only,
secret-free, SHA-pinned and runs the reproduction script."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w.-]+)*@[0-9a-f]{40}$")


def _job() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]["qualification-reproducible"]


def test_job_is_read_only_and_bounded():
    job = _job()
    assert job["permissions"] == {"contents": "read"}
    assert isinstance(job["timeout-minutes"], int) and job["timeout-minutes"] <= 15
    assert "secrets." not in CI.read_text()


def test_job_actions_pinned_and_no_credentials_persisted():
    steps = _job()["steps"]
    for step in steps:
        if "uses" in step:
            assert SHA_PIN.match(step["uses"]), step["uses"]
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False


def test_job_runs_reproduction_without_expressions_in_shell():
    runs = [s["run"] for s in _job()["steps"] if "run" in s]
    assert any("tools/qualification/reproduce.py" in r for r in runs)
    for r in runs:
        assert "${{" not in r
