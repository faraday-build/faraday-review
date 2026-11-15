"""QA-1 Q2: `faraday-review reference` reads independent evidence from
GitHub (GET only) at each real PR's exact ledger head."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faraday_review import cli
from faraday_review.telemetry.qualification import local_validation_errors
from fake_github import FakeSession

REPO = "tomrivera-ops/ecosystem-audit"
HEAD = "a" * 40
OLD = "b" * 40
TS = "2026-11-20T00:00:00Z"
CODEX = {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": 199175422}
FOUNDER = "U_kgDODk3msA"
APP = 15368


def ledger(*entries):
    return {"entries": list(entries)}


def entry(pr=28, head=HEAD, current=True, first="2026-11-15T06:24:20Z"):
    return {"repository": REPO, "pr_number": pr, "head_sha": head, "current": current,
            "runs": [{"first_created_at": first}]}


def receipt(commit, rid=900, user=CODEX):
    return {"id": rid, "user": user,
            "body": f"<!-- codex-review-receipt:v1 -->\nCodex Review: ok\n\n**Reviewed commit:** `{commit[:10]}`"}


class Api:
    def __init__(self, files=(".github/workflows/x.yml",), merged=False, head=HEAD):
        self.files = [{"filename": f} for f in files]
        self.changed = len(self.files)
        self.merged = merged
        self.head = head
        self.comments = [receipt(HEAD)]
        self.reviews: list = []
        self.inline: list = []
        self.classic = {"checks": [{"context": "required-checks", "app_id": APP}],
                        "contexts": ["required-checks"]}
        self.rules: list = []
        self.check_runs = [{"id": 5, "name": "required-checks", "head_sha": HEAD, "app": {"id": APP},
                            "conclusion": "success", "started_at": "2026-11-15T06:20:00Z"}]
        self.commits: list = []
        self.pr_commits = [{"sha": HEAD}]
        self.timeline: list = []
        self.visibility = {"visibility": "public", "private": False}

    @property
    def routes(self):
        pr = {"state": "closed" if self.merged else "open", "merged": self.merged, "draft": False,
              "head": {"sha": self.head}, "base": {"ref": "main"}, "changed_files": self.changed,
              "merged_at": "2026-11-16T00:00:00Z" if self.merged else None,
              "merge_commit_sha": "c" * 40 if self.merged else None}
        b = f"/repos/{REPO}"
        return {
            b: (200, self.visibility),
            f"{b}/pulls/28": (200, pr),
            f"{b}/pulls/28/files": (200, self.files),
            f"{b}/issues/28/comments": (200, self.comments),
            f"{b}/pulls/28/reviews": (200, self.reviews),
            f"{b}/pulls/28/comments": (200, self.inline),
            f"{b}/branches/main": (200, {"protection": {"required_status_checks": self.classic}}),
            f"{b}/rules/branches/main": (200, self.rules),
            f"{b}/commits/{HEAD}/check-runs": (200, {"total_count": len(self.check_runs),
                                                     "check_runs": self.check_runs}),
            f"{b}/pulls/28/commits": (200, self.pr_commits),
            f"{b}/commits": (200, self.commits),
            f"{b}/issues/28/timeline": (200, self.timeline),
        }


def run(monkeypatch, tmp_path, api, led=None, floor="A0", synthetic=(), visibility="public", ts=TS):
    enrollment = [{"repository": REPO, "visibility": visibility, "workflow": ".github/workflows/faraday-review.yml"}]
    if floor is not None:
        enrollment[0]["repository_floor"] = floor
    (tmp_path / "enrollment.json").write_text(json.dumps(enrollment))
    (tmp_path / "ledger.json").write_text(json.dumps(led or ledger(entry())))
    (tmp_path / "synthetic.json").write_text(json.dumps(list(synthetic)))
    session = FakeSession(api.routes)
    monkeypatch.setattr(cli, "_new_session", lambda: session)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-a-secret")
    out = tmp_path / "out" / "reference.json"
    code = cli.main(["reference", "--ledger", str(tmp_path / "ledger.json"),
                     "--synthetic", str(tmp_path / "synthetic.json"),
                     "--enrollment", str(tmp_path / "enrollment.json"),
                     "--collected-at", ts, "--output", str(out)])
    doc = json.loads(out.read_text()) if out.exists() else None
    return code, doc, session


def only(doc):
    (pr,) = doc["prs"]
    return pr


def test_all_four_layers_at_exact_head_and_get_only(monkeypatch, tmp_path):
    code, doc, session = run(monkeypatch, tmp_path, Api())
    assert code == 0
    assert local_validation_errors("qualification-reference", doc) == []
    pr = only(doc)
    assert pr["exact_head"] is True
    assert pr["path_tier"] == {"status": "complete", "tier": "A3", "floor": "A0",
                               "paths": [".github/workflows/x.yml"],
                               "path_tier_counts": {"A0": 0, "A1": 0, "A2": 0, "A3": 1}}
    assert pr["codex"]["status"] == "clean" and pr["codex"]["receipt_ids"] == [900]
    assert pr["required_checks"]["status"] == "success"
    assert pr["outcome"]["status"] == "not_merged"
    assert doc["reference_policy"]["status"] == "proposed"
    assert {m for m, _ in session.calls} == {"GET"}


def test_codex_impostor_is_ignored(monkeypatch, tmp_path):
    api = Api()
    api.comments = [receipt(HEAD, user={"login": "chatgpt-codex-connector[bot]", "type": "User", "id": 1}),
                    receipt(HEAD, rid=901, user={"login": "someone", "type": "Bot", "id": 199175422}),
                    receipt(HEAD, rid=902, user={"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": 7}),
                    receipt(HEAD, rid=903, user={"login": "chatgpt-codex-connector[bot]", "type": "User",
                                                 "id": 199175422})]
    assert only(run(monkeypatch, tmp_path, api)[1])["codex"]["status"] == "absent"


def test_codex_receipt_for_another_commit_is_absent(monkeypatch, tmp_path):
    api = Api()
    api.comments = [receipt(OLD)]
    assert only(run(monkeypatch, tmp_path, api)[1])["codex"]["status"] == "absent"


def _findings_review(api, commit, severity, rid=700):
    api.comments = []
    api.reviews = [{"id": rid, "user": CODEX, "commit_id": commit,
                    "body": receipt(commit)["body"]}]
    api.inline = [{"id": 1, "user": CODEX, "pull_request_review_id": rid,
                   "body": f"**<sub><sub>![{severity} Badge](https://img.shields.io/x)</sub></sub> x**"}]


@pytest.mark.parametrize("severity,status", [("P0", "findings"), ("P1", "findings"),
                                             ("P2", "clean"), ("P3", "clean")])
def test_codex_findings_at_head(monkeypatch, tmp_path, severity, status):
    api = Api()
    _findings_review(api, HEAD, severity)
    codex = only(run(monkeypatch, tmp_path, api)[1])["codex"]
    assert codex["status"] == status
    assert codex["findings"] == [{"id": 1, "severity": severity}]


def test_codex_findings_on_an_older_commit_do_not_count_at_head(monkeypatch, tmp_path):
    api = Api()
    _findings_review(api, OLD, "P1")
    api.comments = [receipt(HEAD)]
    codex = only(run(monkeypatch, tmp_path, api)[1])["codex"]
    assert codex["status"] == "clean" and codex["findings"] == []


def test_first_head_receipt_is_recorded_for_sampling(monkeypatch, tmp_path):
    api = Api()
    api.comments = [receipt(OLD, rid=50), receipt(HEAD, rid=60)]
    led = ledger(entry(head=OLD, current=False, first="2026-11-15T01:00:00Z"), entry())
    pr = only(run(monkeypatch, tmp_path, api, led=led)[1])
    assert pr["first_head_sha"] == OLD and pr["codex"]["first_head_receipt_id"] == 50


@pytest.mark.parametrize("changed,status", [(2, "incomplete"), (0, "incomplete")])
def test_incomplete_file_list_is_unknown(monkeypatch, tmp_path, changed, status):
    api = Api()
    api.changed = changed
    pt = only(run(monkeypatch, tmp_path, api)[1])["path_tier"]
    assert pt["status"] == status and pt["tier"] == "unknown"


def test_renamed_file_counts_both_names(monkeypatch, tmp_path):
    api = Api(files=("docs/a.md",))
    api.files[0]["previous_filename"] = ".github/workflows/old.yml"
    pt = only(run(monkeypatch, tmp_path, api)[1])["path_tier"]
    assert pt["tier"] == "A3" and pt["paths"] == [".github/workflows/old.yml", "docs/a.md"]


def test_head_moved_makes_l1_and_l3_unknown(monkeypatch, tmp_path):
    pr = only(run(monkeypatch, tmp_path, Api(head=OLD))[1])
    assert pr["exact_head"] is False
    assert pr["path_tier"]["status"] == "head_moved" and pr["path_tier"]["tier"] == "unknown"
    assert pr["required_checks"]["status"] == "unknown"


def test_missing_floor_is_unknown_tier(monkeypatch, tmp_path):
    pt = only(run(monkeypatch, tmp_path, Api(), floor=None)[1])["path_tier"]
    assert pt["floor"] == "unknown" and pt["tier"] == "unknown"


def test_production_floor_raises_docs_to_a3(monkeypatch, tmp_path):
    pt = only(run(monkeypatch, tmp_path, Api(files=("README.md",)), floor="A3")[1])["path_tier"]
    assert pt["tier"] == "A3"


@pytest.mark.parametrize("mutate,status", [
    (lambda a: a.check_runs[0].update(conclusion="failure"), "not_success"),
    (lambda a: a.check_runs[0].update(conclusion="neutral"), "not_success"),
    (lambda a: a.check_runs[0].update(app={"id": 1}), "not_success"),
    (lambda a: a.check_runs[0].update(head_sha=OLD), "not_success"),
    (lambda a: setattr(a, "check_runs", []), "not_success"),
    (lambda a: setattr(a, "classic", {}), "unknown"),
])
def test_required_checks(monkeypatch, tmp_path, mutate, status):
    api = Api()
    mutate(api)
    assert only(run(monkeypatch, tmp_path, api)[1])["required_checks"]["status"] == status


def test_ruleset_required_checks_are_included(monkeypatch, tmp_path):
    api = Api()
    api.classic = {}
    api.rules = [{"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": "required-checks", "integration_id": APP}]}}]
    assert only(run(monkeypatch, tmp_path, api)[1])["required_checks"]["status"] == "success"


@pytest.mark.parametrize("message,status", [
    ("Revert \"x\"\n\nThis reverts commit " + "c" * 40 + ".", "adverse"),
    ("Revert \"x\"\n\nThis reverts commit " + HEAD + ".", "adverse"),
    ("Revert \"x\"\n\nReverts tomrivera-ops/ecosystem-audit#28", "adverse"),
    ("Reverts tomrivera-ops/ecosystem-audit#280", "clear"),
    ("unrelated", "clear"),
])
def test_post_merge_outcome_after_window(monkeypatch, tmp_path, message, status):
    api = Api(merged=True)
    api.commits = [{"sha": "d" * 40, "commit": {"message": message}}]
    out = only(run(monkeypatch, tmp_path, api, ts="2026-12-01T00:00:00Z")[1])["outcome"]
    assert out["status"] == status and out["window_end"] == "2026-11-30T00:00:00Z"


def test_outcome_window_open_is_pending_and_revert_still_adverse(monkeypatch, tmp_path):
    api = Api(merged=True)
    assert only(run(monkeypatch, tmp_path, api)[1])["outcome"]["status"] == "pending"
    api.commits = [{"sha": "d" * 40, "commit": {"message": "This reverts commit " + HEAD}}]
    (tmp_path / "b").mkdir()
    assert only(run(monkeypatch, tmp_path / "b", api)[1])["outcome"]["status"] == "adverse"


def test_founder_label_events_only(monkeypatch, tmp_path):
    api = Api()
    api.timeline = [
        {"id": 2, "event": "labeled", "label": {"name": "faraday/founder-red"},
         "actor": {"node_id": FOUNDER}, "created_at": "2026-11-15T07:00:00Z"},
        {"id": 1, "event": "labeled", "label": {"name": "bug"}, "actor": {"node_id": FOUNDER},
         "created_at": "2026-11-15T06:59:00Z"},
        {"id": 3, "event": "commented", "created_at": "2026-11-15T07:01:00Z"},
    ]
    events = only(run(monkeypatch, tmp_path, api)[1])["founder_label_events"]
    assert events == [{"event": "labeled", "label": "faraday/founder-red", "actor_node_id": FOUNDER,
                       "created_at": "2026-11-15T07:00:00Z", "id": 2}]


def test_synthetic_prs_are_skipped(monkeypatch, tmp_path):
    code, doc, session = run(monkeypatch, tmp_path, Api(),
                             synthetic=[{"repository": REPO.upper(), "pr_number": 28, "reason": "x"}])
    assert code == 0 and doc["prs"] == [] and session.calls == []


@pytest.mark.parametrize("visibility,live", [("private", "public"), ("public", "private")])
def test_private_repository_is_refused_and_nothing_written(monkeypatch, tmp_path, visibility, live):
    api = Api()
    api.visibility = {"visibility": live, "private": live == "private"}
    code, doc, _ = run(monkeypatch, tmp_path, api, visibility=visibility)
    assert code == 2 and doc is None


def test_api_error_writes_nothing(monkeypatch, tmp_path):
    api = Api()
    routes = api.routes
    routes.pop(f"/repos/{REPO}/pulls/28/files")
    session = FakeSession(routes)
    monkeypatch.setattr(Api, "routes", property(lambda self: routes))
    code, doc, _ = run(monkeypatch, tmp_path, api)
    assert code == 2 and doc is None


def test_empty_file_list_is_incomplete(monkeypatch, tmp_path):
    pt = only(run(monkeypatch, tmp_path, Api(files=()))[1])["path_tier"]
    assert pt["status"] == "incomplete" and pt["tier"] == "unknown"


def test_output_names_only_the_caller_repository(monkeypatch, tmp_path):
    from faraday_review.github.collection import public_identity_violations
    _, doc, _ = run(monkeypatch, tmp_path, Api())
    assert public_identity_violations(doc, [{"repository": REPO, "visibility": "public"}]) == 0
    assert "agent-contracts" not in json.dumps(doc)
