"""`faraday-review preflight --repo X` (GHO-004 P2, item 8): read-only
activation checks. Every gap is listed; anything the token cannot read is
"unknown", which never passes."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from faraday_review import cli
from fake_github import FakeSession

ROOT = Path(__file__).resolve().parents[2]
REPO = "tomrivera-ops/ecosystem-audit"
OWNER = "tomrivera-ops"
DIGEST = "sha256:32eeb3e07271fff267908bc428d77477caa403d9e0f8b0a0244d8c26aec73e12"
SHA = "b9002669fcc71f347f2cd91d4999a9b48fe4db78"
LABELS = ["faraday/founder-green", "faraday/founder-yellow", "faraday/founder-red",
          "faraday/founder-block"]


def _caller(sha: str = SHA, reviewer_sha: str | None = None) -> str:
    return f"""name: faraday-review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
jobs:
  faraday-review:
    if: vars.FARADAY_REVIEW_SHADOW_ENABLED == 'true'
    uses: faraday-build/faraday-review/.github/workflows/shadow.yml@{sha}
    with:
      reviewer_sha: {reviewer_sha or sha}
      governance_sha: 1b98ab9b4aa13a0d481ef47972de62c3782e9f2a
      policy_digest: ${{{{ vars.FARADAY_POLICY_DIGEST }}}}
"""


def _routes(**over) -> dict:
    routes = {
        f"/repos/{REPO}": (200, {"full_name": REPO, "visibility": "public", "private": False}),
        f"/repos/{REPO}/actions/variables/FARADAY_POLICY_DIGEST":
            (200, {"name": "FARADAY_POLICY_DIGEST", "value": DIGEST}),
        f"/repos/{REPO}/actions/variables/FARADAY_REVIEW_SHADOW_ENABLED":
            (200, {"name": "FARADAY_REVIEW_SHADOW_ENABLED", "value": "false"}),
        f"/repos/{REPO}/contents/.github/workflows/faraday-review.yml":
            (200, {"encoding": "base64",
                   "content": base64.b64encode(_caller().encode()).decode()}),
        f"/repos/faraday-build/faraday-review/compare/{SHA}...main": (200, {"status": "ahead"}),
    }
    for label in LABELS:
        routes[f"/repos/{REPO}/labels/{label}"] = (200, {"name": label})
    routes.update(over)
    return routes


def _preflight(monkeypatch, capsys, routes) -> tuple[int, dict, FakeSession]:
    session = FakeSession(routes)
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-secret")
    code = cli.main(["preflight", "--repo", REPO])
    report = json.loads(capsys.readouterr().out)
    return code, report, session


def _status(report: dict) -> dict:
    return {c["check"]: c["status"] for c in report["checks"]}


def test_released_digest_equals_deployment_doc():
    from faraday_review.github.preflight import RELEASED_POLICY_DIGEST

    doc = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    assert re.findall(r"sha256:[0-9a-f]{64}", doc) == [RELEASED_POLICY_DIGEST]
    assert RELEASED_POLICY_DIGEST == DIGEST


def test_all_pass_exit_0_and_only_reads(monkeypatch, capsys):
    code, report, session = _preflight(monkeypatch, capsys, _routes())
    assert code == 0, report
    assert report["gaps"] == []
    assert set(_status(report).values()) == {"pass"}
    assert report["enable_variable"] == {"status": "set", "value": "false", "scope": "repository"}
    assert session.calls and all(m == "GET" for m, _ in session.calls)


def test_digest_unset_everywhere_is_a_gap(monkeypatch, capsys):
    routes = _routes()
    del routes[f"/repos/{REPO}/actions/variables/FARADAY_POLICY_DIGEST"]
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 1
    assert _status(report)["policy_digest"] == "fail"
    assert any("FARADAY_POLICY_DIGEST" in g for g in report["gaps"])


def test_org_digest_counts(monkeypatch, capsys):
    routes = _routes()
    del routes[f"/repos/{REPO}/actions/variables/FARADAY_POLICY_DIGEST"]
    routes[f"/orgs/{OWNER}/actions/variables/FARADAY_POLICY_DIGEST"] = (
        200, {"name": "FARADAY_POLICY_DIGEST", "value": DIGEST, "visibility": "all"})
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 0, report


def test_org_digest_selected_without_this_repo_is_a_gap(monkeypatch, capsys):
    routes = _routes()
    del routes[f"/repos/{REPO}/actions/variables/FARADAY_POLICY_DIGEST"]
    routes[f"/orgs/{OWNER}/actions/variables/FARADAY_POLICY_DIGEST"] = (
        200, {"name": "FARADAY_POLICY_DIGEST", "value": DIGEST, "visibility": "selected"})
    routes[f"/orgs/{OWNER}/actions/variables/FARADAY_POLICY_DIGEST/repositories"] = (
        200, {"total_count": 1, "repositories": [{"full_name": "tomrivera-ops/other"}]})
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 1 and _status(report)["policy_digest"] == "fail"


def test_wrong_digest_is_a_gap(monkeypatch, capsys):
    code, report, _ = _preflight(monkeypatch, capsys, _routes(**{
        f"/repos/{REPO}/actions/variables/FARADAY_POLICY_DIGEST":
            (200, {"name": "FARADAY_POLICY_DIGEST", "value": "sha256:" + "0" * 64})}))
    assert code == 1 and _status(report)["policy_digest"] == "fail"


def test_unreadable_variables_are_unknown_never_pass(monkeypatch, capsys):
    code, report, _ = _preflight(monkeypatch, capsys, _routes(**{
        f"/repos/{REPO}/actions/variables/FARADAY_POLICY_DIGEST": (403, {"message": "no"}),
        f"/repos/{REPO}/actions/variables/FARADAY_REVIEW_SHADOW_ENABLED": (403, {"message": "no"}),
    }))
    assert code == 1
    assert _status(report)["policy_digest"] == "unknown"
    assert report["enable_variable"]["status"] == "unknown"
    assert any("unknown" in g for g in report["gaps"])


def test_missing_labels_each_listed(monkeypatch, capsys):
    routes = _routes()
    del routes[f"/repos/{REPO}/labels/faraday/founder-red"]
    del routes[f"/repos/{REPO}/labels/faraday/founder-block"]
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 1
    assert _status(report)["labels"] == "fail"
    gaps = " ".join(report["gaps"])
    assert "faraday/founder-red" in gaps and "faraday/founder-block" in gaps
    assert "faraday/founder-green" not in gaps


@pytest.mark.parametrize("content,compare", [
    (_caller(sha="main"), None),
    (_caller(reviewer_sha="a" * 40), "ahead"),
    (_caller(), "diverged"),
    ("name: x\non: pull_request\njobs: {}\n", None),
])
def test_caller_pin_gaps(monkeypatch, capsys, content, compare):
    routes = _routes(**{f"/repos/{REPO}/contents/.github/workflows/faraday-review.yml":
                        (200, {"encoding": "base64",
                               "content": base64.b64encode(content.encode()).decode()})})
    if compare is not None:
        routes[f"/repos/faraday-build/faraday-review/compare/{SHA}...main"] = (200, {"status": compare})
    # A branch ref must fail as unpinned even if the branch is "on main".
    routes["/repos/faraday-build/faraday-review/compare/main...main"] = (200, {"status": "identical"})
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 1
    assert _status(report)["caller_pin"] == "fail"


def test_caller_workflow_missing(monkeypatch, capsys):
    routes = _routes()
    del routes[f"/repos/{REPO}/contents/.github/workflows/faraday-review.yml"]
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 1 and _status(report)["caller_pin"] == "fail"


def test_every_gap_listed_at_once(monkeypatch, capsys):
    routes = {f"/repos/{REPO}": (200, {"full_name": REPO, "visibility": "public"})}
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 1
    assert {"policy_digest", "labels", "caller_pin"} <= {
        c["check"] for c in report["checks"] if c["status"] != "pass"}
    assert len(report["gaps"]) >= 6  # digest + 4 labels + caller pin


def test_invalid_repo_argument(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert cli.main(["preflight", "--repo", "not a repo"]) == 2


def test_unknown_enable_variable_is_a_gap(monkeypatch, capsys):
    """Codex P2 #10 preflight.py:180: a known disabled value is not gating,
    an unreadable one is."""
    code, report, _ = _preflight(monkeypatch, capsys, _routes(**{
        f"/repos/{REPO}/actions/variables/FARADAY_REVIEW_SHADOW_ENABLED": (403, {"message": "no"})}))
    assert code == 1
    assert report["enable_variable"]["status"] == "unknown"
    assert any(ENABLE in g for g in report["gaps"])


def test_absent_enable_variable_is_reported_not_gating(monkeypatch, capsys):
    routes = _routes()
    del routes[f"/repos/{REPO}/actions/variables/FARADAY_REVIEW_SHADOW_ENABLED"]
    code, report, _ = _preflight(monkeypatch, capsys, routes)
    assert code == 0, report
    assert report["enable_variable"]["status"] == "absent"


ENABLE = "FARADAY_REVIEW_SHADOW_ENABLED"
