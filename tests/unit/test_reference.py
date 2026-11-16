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
        self.statuses: list = []
        self.commit_count = None
        self.commit_pulls: dict = {}

    @property
    def routes(self):
        pr = {"state": "closed" if self.merged else "open", "merged": self.merged, "draft": False,
              "head": {"sha": self.head}, "base": {"ref": "main"}, "changed_files": self.changed,
              "merged_at": "2026-11-16T00:00:00Z" if self.merged else None,
              "merge_commit_sha": "c" * 40 if self.merged else None,
              "commits": len(self.pr_commits) if self.commit_count is None else self.commit_count}
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
            f"{b}/commits/{HEAD}/status": (200, {"statuses": self.statuses}),
            **{f"{b}/commits/{sha}/pulls": (200, prs) for sha, prs in self.commit_pulls.items()},
            f"{b}/commits/{HEAD}/check-suites": (200, {"total_count": 1, "check_suites": [
                {"id": 1, "created_at": "2026-11-15T06:19:00Z"}]}),
            f"{b}/commits/{OLD}/check-suites": (200, {"total_count": 0, "check_suites": []}),
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


def test_review_findings_count_even_without_a_receipt_marker(monkeypatch, tmp_path):
    api = Api()
    api.reviews = [{"id": 700, "user": CODEX, "commit_id": HEAD, "body": "Some review without a marker"}]
    api.inline = [{"id": 1, "user": CODEX, "pull_request_review_id": 700,
                   "body": "![P1 Badge](https://img.shields.io/x) bad"}]
    codex = only(run(monkeypatch, tmp_path, api)[1])["codex"]
    assert codex["status"] == "findings" and codex["receipt_ids"] == [900]


def test_requirements_from_both_paths_are_conjunctive(monkeypatch, tmp_path):
    api = Api()
    api.rules = [{"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": "required-checks"}]}}]
    api.check_runs[0]["app"] = {"id": 1}  # success, but not from the classic-pinned app
    rc = only(run(monkeypatch, tmp_path, api)[1])["required_checks"]
    assert rc["status"] == "not_success"
    assert {(c["context"], c["app_id"]) for c in rc["contexts"]} == {("required-checks", APP),
                                                                     ("required-checks", None)}


@pytest.mark.parametrize("state,pinned,status", [("success", False, "success"), ("failure", False, "not_success"),
                                                 ("success", True, "not_success")])
def test_commit_status_satisfies_only_unpinned_requirements(monkeypatch, tmp_path, state, pinned, status):
    api = Api()
    api.check_runs = []
    api.classic = ({"checks": [{"context": "ci/legacy", "app_id": APP}], "contexts": ["ci/legacy"]} if pinned
                   else {"checks": [{"context": "ci/legacy", "app_id": None}], "contexts": ["ci/legacy"]})
    api.statuses = [{"context": "ci/legacy", "state": state}]
    rc = only(run(monkeypatch, tmp_path, api)[1])["required_checks"]
    assert rc["status"] == status


@pytest.mark.parametrize("run_conclusion,state,status", [
    ("success", "success", "success"), ("success", "failure", "not_success"),
    ("success", "pending", "not_success"), ("failure", "success", "not_success")])
def test_same_named_check_run_and_status_are_conjunctive(monkeypatch, tmp_path, run_conclusion, state, status):
    """Codex P2 r2 (#18): an unpinned requirement reported by both a check
    run and a commit status holds only when both pass."""
    api = Api()
    api.classic = {"checks": [{"context": "required-checks", "app_id": None}], "contexts": ["required-checks"]}
    api.check_runs[0]["conclusion"] = run_conclusion
    api.statuses = [{"context": "required-checks", "state": state}]
    rc = only(run(monkeypatch, tmp_path, api)[1])["required_checks"]
    assert rc["status"] == status
    assert rc["contexts"][0]["evidence_kind"] == "check_run+status"


@pytest.mark.parametrize("second,status", [("success", "not_success"), ("failure", "not_success")])
def test_same_named_runs_from_independent_suites_are_conjunctive(monkeypatch, tmp_path, second, status):
    """Codex P2 (#22): a failing `required-checks` from one workflow is not
    hidden by a later successful same-named run from another workflow."""
    api = Api()
    api.check_runs = [
        {"id": 5, "name": "required-checks", "head_sha": HEAD, "app": {"id": APP}, "conclusion": "failure",
         "started_at": "2026-11-15T06:20:00Z", "check_suite": {"id": 71}},
        {"id": 6, "name": "required-checks", "head_sha": HEAD, "app": {"id": APP}, "conclusion": "success",
         "started_at": "2026-11-15T06:25:00Z", "check_suite": {"id": 72}}]
    if second == "failure":
        api.check_runs[0]["conclusion"], api.check_runs[1]["conclusion"] = "success", "failure"
    rc = only(run(monkeypatch, tmp_path, api)[1])["required_checks"]
    assert rc["status"] == status


def test_rerun_within_one_suite_supersedes_the_earlier_attempt(monkeypatch, tmp_path):
    api = Api()
    api.check_runs = [
        {"id": 5, "name": "required-checks", "head_sha": HEAD, "app": {"id": APP}, "conclusion": "failure",
         "started_at": "2026-11-15T06:20:00Z", "check_suite": {"id": 71}},
        {"id": 6, "name": "required-checks", "head_sha": HEAD, "app": {"id": APP}, "conclusion": "success",
         "started_at": "2026-11-15T06:25:00Z", "check_suite": {"id": 71}}]
    rc = only(run(monkeypatch, tmp_path, api)[1])["required_checks"]
    assert rc["status"] == "success" and rc["contexts"][0]["check_run_id"] == 6


@pytest.mark.parametrize("count,revert,status", [
    (251, False, "incomplete"), (2, False, "incomplete"), ("absent", False, "incomplete"),
    (251, True, "adverse"), (1, False, "clear")])
def test_truncated_pr_commit_list_never_reports_clear(monkeypatch, tmp_path, count, revert, status):
    """Codex P2 (#22): GitHub lists at most 250 PR commits; when the listing
    may be incomplete the outcome is incomplete (or adverse), never clear."""
    api = Api(merged=True)
    api.commit_count = None if count == "absent" else count
    if count == "absent":
        api.commit_count = "absent"
    if revert:
        api.commits = [{"sha": "d" * 40, "commit": {"message": f"Revert\n\nThis reverts commit {HEAD}."}}]
    out = only(run(monkeypatch, tmp_path, api, ts="2026-12-15T00:00:00Z")[1])["outcome"]
    assert out["status"] == status
    if status == "incomplete":
        assert out["reason"] == "pr_commits_truncated" and out["reverts"] == []


REBASED = "e" * 40


@pytest.mark.parametrize("pulls,status", [([{"number": 28}], "adverse"), ([{"number": 27}], "clear"),
                                          ([], "clear")])
def test_revert_of_a_rebased_commit_is_attributed_by_its_pull_request(monkeypatch, tmp_path, pulls, status):
    """Codex P2 (#22): a rebase merge lands new SHAs (only the last is
    merge_commit_sha). A revert naming another rebased commit is attributed
    through GitHub's commit-to-PR association."""
    api = Api(merged=True)
    api.commit_pulls = {REBASED: pulls}
    api.commits = [{"sha": "d" * 40, "commit": {"message": f"Revert \"y\"\n\nThis reverts commit {REBASED}."}}]
    out = only(run(monkeypatch, tmp_path, api, ts="2026-12-15T00:00:00Z")[1])["outcome"]
    assert out["status"] == status


class SuiteCount(Api):
    def __init__(self, total):
        super().__init__()
        self.total = total

    @property
    def routes(self):
        routes = dict(super().routes)
        body = routes[f"/repos/{REPO}/commits/{HEAD}/check-suites"][1]
        routes[f"/repos/{REPO}/commits/{HEAD}/check-suites"] = (200, dict(body, total_count=self.total)
                                                               if self.total is not None
                                                               else {"check_suites": body["check_suites"]})
        return routes


@pytest.mark.parametrize("total,status,reason", [(1000, "success", None), (1001, "unknown", "check_suites_capped"),
                                                 (None, "unknown", "check_suites_count_unknown")])
def test_check_suite_listing_cap_fails_closed(monkeypatch, tmp_path, total, status, reason):
    """Codex P2 (#22): beyond 1,000 check suites the check-runs listing can
    omit an older failing suite, so required checks become unknown."""
    rc = only(run(monkeypatch, tmp_path, SuiteCount(total))[1])["required_checks"]
    assert rc["status"] == status and rc.get("reason") == reason


class PagedStatuses(Api):
    """Combined status split over two pages, the required context on page 2."""

    def __init__(self, truncate=False):
        super().__init__()
        self.check_runs = []
        self.classic = {"checks": [{"context": "ci/legacy", "app_id": None}], "contexts": ["ci/legacy"]}
        self.truncate = truncate

    @property
    def routes(self):
        routes = dict(super().routes)
        b = f"/repos/{REPO}"
        page1 = [{"context": f"ctx-{i}", "state": "success"} for i in range(30)]
        total = 32 if self.truncate else 31
        routes[f"{b}/commits/{HEAD}/status"] = (
            200, {"total_count": total, "statuses": page1},
            {"next": {"url": f"https://api.github.com{b}/status-page-2"}})
        routes[f"{b}/status-page-2"] = (200, {"total_count": total,
                                              "statuses": [{"context": "ci/legacy", "state": "success"}]})
        return routes


def test_combined_status_is_paginated(monkeypatch, tmp_path):
    """Codex P2 r2 (#18): a required status on a later page is read."""
    rc = only(run(monkeypatch, tmp_path, PagedStatuses())[1])["required_checks"]
    assert rc["status"] == "success" and rc["contexts"][0]["evidence_kind"] == "status"


def test_truncated_combined_status_fails_closed(monkeypatch, tmp_path):
    rc = only(run(monkeypatch, tmp_path, PagedStatuses(truncate=True))[1])["required_checks"]
    assert rc["status"] == "unknown" and rc["reason"] == "statuses_truncated"


class MovingHead(Api):
    """The PR head changes after the first PR read (a push mid-collection)."""

    @property
    def routes(self):
        routes = dict(super().routes)
        first = routes[f"/repos/{REPO}/pulls/28"][1]
        seen = {"n": 0}

        def pr(method, kwargs):
            seen["n"] += 1
            return 200, first if seen["n"] == 1 else dict(first, head={"sha": OLD})
        routes[f"/repos/{REPO}/pulls/28"] = pr
        return routes


def test_head_that_moves_during_collection_fails_closed(monkeypatch, tmp_path):
    pr = only(run(monkeypatch, tmp_path, MovingHead())[1])
    assert pr["exact_head"] is False and pr["path_tier"]["tier"] == "unknown"
    assert pr["required_checks"]["status"] == "unknown"


def test_head_transition_evidence_is_recorded(monkeypatch, tmp_path):
    pr = only(run(monkeypatch, tmp_path, Api())[1])
    assert pr["heads"][0]["first_suite_at"] == "2026-11-15T06:19:00Z"


def test_exactly_250_listed_commits_is_complete(monkeypatch, tmp_path):
    api = Api(merged=True)
    api.pr_commits = [{"sha": HEAD}] + [{"sha": f"{i:040x}"} for i in range(1, 250)]
    out = only(run(monkeypatch, tmp_path, api, ts="2026-12-15T00:00:00Z")[1])["outcome"]
    assert out["status"] == "clear"
