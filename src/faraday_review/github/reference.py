"""`faraday-review reference`: QA-1 independent reference evidence
(GHO-AUTONOMOUS-PRODUCTION-011, Q2). Read-only (GET only).

For every real (non-synthetic) PR in a qualification ledger, this reads,
from GitHub, at the ledger's exact current head:

- L1 path tier: the PR file list (all pages), evaluated by the vendored
  ADR-0014 `reference_path_tiers` (telemetry/reference_tier.py) and raised
  to the enrollment's `repository_floor`.
- L2 Codex: receipts and findings posted by the Codex GitHub App account
  for that commit.
- L3 required checks: the base branch's required status-check rules and the
  latest check run of each required context at the head.
- L4 post-merge outcome: reverts of the PR within the outcome window
  (additional evidence only; an unmerged PR keeps its pre-merge evidence).
- Founder-label events from the PR timeline (actor node_id, label, time).
  They are recorded, not judged: `qualification-v5` decides which ones
  answer an exception or a sample at an exact head.

None of this is caller-generated (HR-1): every layer is re-derived from
GitHub at collection time. The output is qualification evidence only; it
authorizes nothing and is never a founder adjudication.

Privacy: public repositories only (live API and enrollment must both say
public), as for the public archive. Any API error fails the whole command
(exit 2); nothing partial is written.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import FaradayReviewError
from ..telemetry.qualification import repo_identity, require_timestamp
from ..telemetry.reference_tier import UNKNOWN, load_policy, pr_tier, tier_counts
from .client import GitHubClient
from .collection import observed_visibility

REFERENCE_FORMAT = "faraday-review/qualification-reference/v1"
OUTCOME_WINDOW_DAYS = 14
FOUNDER_LABEL_PREFIX = "faraday/founder-"
# The Codex connector GitHub App's bot account. Login, type and the
# immutable account id must all match; a human cannot hold a `[bot]` login.
CODEX_LOGIN = "chatgpt-codex-connector[bot]"
CODEX_ACCOUNT_ID = 199175422
RECEIPT_MARKER = "<!-- codex-review-receipt:v1 -->"
_REVIEWED_RE = re.compile(r"\*\*Reviewed commit:\*\*\s*`([0-9a-f]{7,40})`")
_BADGE_RE = re.compile(r"!\[(P[0-3]) Badge\]")
MATERIAL_SEVERITIES = ("P0", "P1")
MAX_PR_FILES = 3000  # GitHub's hard cap on the PR files listing


class ReferenceError(FaradayReviewError):
    reason = "reference_failed"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_codex(user: Any) -> bool:
    return (isinstance(user, dict) and user.get("login") == CODEX_LOGIN
            and user.get("type") == "Bot" and user.get("id") == CODEX_ACCOUNT_ID)


def _reviewed_prefix(body: Any) -> str | None:
    if not isinstance(body, str) or RECEIPT_MARKER not in body:
        return None
    match = _REVIEWED_RE.search(body)
    return match.group(1) if match else None


def _ledger_prs(ledger: dict[str, Any], synthetic: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per real PR: the current head, and the first head (the
    head of its earliest shadow run, stable across later pushes)."""
    skip = {(repo_identity(s["repository"]), int(s["pr_number"])) for s in synthetic}
    prs: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in ledger["entries"]:
        key = (repo_identity(entry["repository"]), int(entry["pr_number"]))
        if key in skip:
            continue
        first = min((r["first_created_at"] for r in entry["runs"] if r.get("first_created_at")),
                    default=None)
        rec = prs.setdefault(key, {"repository": entry["repository"], "pr_number": key[1],
                                   "head_sha": None, "heads": []})
        rec["heads"].append({"head_sha": entry["head_sha"], "first_created_at": first})
        if entry.get("current"):
            rec["head_sha"] = entry["head_sha"]
    out = []
    for key in sorted(prs):
        rec = prs[key]
        dated = sorted((h for h in rec["heads"] if h["first_created_at"]),
                       key=lambda h: (h["first_created_at"], h["head_sha"]))
        rec["first_head_sha"] = dated[0]["head_sha"] if dated else rec["head_sha"]
        rec["heads"] = sorted(rec["heads"], key=lambda h: (h["first_created_at"] or "", h["head_sha"]))
        if rec["head_sha"] is not None:
            out.append(rec)
    return out


def _path_tier(client: GitHubClient, repo: str, n: int, pr: dict[str, Any], exact: bool,
               floor: str) -> dict[str, Any]:
    if not exact:
        return {"status": "head_moved", "tier": UNKNOWN, "floor": floor, "paths": [],
                "path_tier_counts": tier_counts([])}
    files = client.paginate(f"/repos/{repo}/pulls/{n}/files")
    paths = set()
    for f in files:
        paths.add(str(f["filename"]))
        if f.get("previous_filename"):
            paths.add(str(f["previous_filename"]))
    expected = int(pr.get("changed_files") or 0)
    complete = len(files) == expected and expected < MAX_PR_FILES and expected > 0
    ordered = sorted(paths)
    return {"status": "complete" if complete else "incomplete",
            "tier": pr_tier(ordered, floor) if complete else UNKNOWN,
            "floor": floor, "paths": ordered, "path_tier_counts": tier_counts(ordered)}


def _codex(client: GitHubClient, repo: str, n: int, head: str, first_head: str) -> dict[str, Any]:
    comments = client.paginate(f"/repos/{repo}/issues/{n}/comments")
    reviews = client.paginate(f"/repos/{repo}/pulls/{n}/reviews")
    inline = client.paginate(f"/repos/{repo}/pulls/{n}/comments")
    receipts = []  # (id, kind, reviewed prefix)
    for c in comments:
        prefix = _reviewed_prefix(c.get("body")) if _is_codex(c.get("user")) else None
        if prefix:
            receipts.append((int(c["id"]), "comment", prefix))
    review_commit = {}
    for r in reviews:
        if not _is_codex(r.get("user")):
            continue
        prefix = _reviewed_prefix(r.get("body"))
        if prefix and str(r.get("commit_id", "")).startswith(prefix):
            receipts.append((int(r["id"]), "review", prefix))
            review_commit[int(r["id"])] = str(r["commit_id"])
    findings = []
    for c in inline:
        if not _is_codex(c.get("user")):
            continue
        rid = c.get("pull_request_review_id")
        if review_commit.get(rid) != head:
            continue
        match = _BADGE_RE.search(str(c.get("body", "")))
        findings.append({"id": int(c["id"]), "severity": match.group(1) if match else "unrated"})
    at_head = sorted(rid for rid, _, p in receipts if head.startswith(p))
    at_first = sorted(rid for rid, _, p in receipts if first_head.startswith(p))
    findings.sort(key=lambda f: f["id"])
    if any(f["severity"] in MATERIAL_SEVERITIES for f in findings):
        status = "findings"
    elif at_head:
        status = "clean"
    else:
        status = "absent"
    return {"status": status, "receipt_ids": at_head, "findings": findings,
            "first_head_receipt_id": at_first[0] if at_first else None}


def _required_checks(client: GitHubClient, repo: str, base_ref: str, head: str) -> dict[str, Any]:
    # Required contexts from both enforcement paths, read-only: classic
    # branch protection (exposed on the branch object) and rulesets.
    required: dict[str, int | None] = {}
    branch = client.get_json(f"/repos/{repo}/branches/{base_ref}")
    classic = ((branch.get("protection") or {}).get("required_status_checks") or {}
               if isinstance(branch, dict) else {})
    for item in classic.get("checks") or []:
        required[str(item["context"])] = item.get("app_id")
    for context in classic.get("contexts") or []:
        required.setdefault(str(context), None)
    rules = client.paginate(f"/repos/{repo}/rules/branches/{base_ref}")
    for rule in rules:
        if rule.get("type") != "required_status_checks":
            continue
        for item in (rule.get("parameters") or {}).get("required_status_checks") or []:
            required[str(item["context"])] = item.get("integration_id")
    if not required:
        return {"status": UNKNOWN, "reason": "no_required_checks", "contexts": []}
    contexts = []
    for context in sorted(required):
        app_id = required[context]
        runs = client.paginate_key(f"/repos/{repo}/commits/{head}/check-runs", "check_runs",
                                   {"check_name": context, "filter": "latest"})
        runs = [r for r in runs if r.get("name") == context and r.get("head_sha") == head
                and (app_id is None or (r.get("app") or {}).get("id") == app_id)]
        latest = max(runs, key=lambda r: (str(r.get("started_at") or ""), int(r["id"])), default=None)
        contexts.append({"context": context, "app_id": app_id,
                         "conclusion": latest.get("conclusion") if latest else None,
                         "check_run_id": int(latest["id"]) if latest else None})
    ok = all(c["conclusion"] == "success" for c in contexts)
    return {"status": "success" if ok else "not_success", "contexts": contexts}


def _outcome(client: GitHubClient, repo: str, n: int, pr: dict[str, Any], head: str,
             collected_at: str) -> dict[str, Any]:
    if not pr.get("merged"):
        return {"status": "not_merged", "window_days": OUTCOME_WINDOW_DAYS, "reverts": []}
    merged_at = _ts(str(pr["merged_at"]))
    window_end = merged_at + timedelta(days=OUTCOME_WINDOW_DAYS)
    now = _ts(collected_at)
    until = min(window_end, now)
    shas = {str(pr.get("merge_commit_sha") or ""), head}
    shas |= {str(c["sha"]) for c in client.paginate(f"/repos/{repo}/pulls/{n}/commits")}
    shas.discard("")
    commits = client.paginate(f"/repos/{repo}/commits",
                              {"sha": pr["base"]["ref"], "since": _iso(merged_at), "until": _iso(until)})
    pr_ref = f"reverts {repo.casefold()}#{n}"
    reverts = sorted(str(c["sha"]) for c in commits
                     if any(f"This reverts commit {s}" in str(c["commit"]["message"]) for s in shas)
                     or re.search(rf"(?<![\w/.-]){re.escape(pr_ref)}(?!\d)",
                                  str(c["commit"]["message"]).casefold()))
    if reverts:
        status = "adverse"
    elif now < window_end:
        status = "pending"
    else:
        status = "clear"
    return {"status": status, "window_days": OUTCOME_WINDOW_DAYS, "merged_at": _iso(merged_at),
            "window_end": _iso(window_end), "reverts": reverts}


def _label_events(client: GitHubClient, repo: str, n: int) -> list[dict[str, Any]]:
    events = []
    for ev in client.paginate(f"/repos/{repo}/issues/{n}/timeline"):
        if ev.get("event") not in ("labeled", "unlabeled"):
            continue
        name = str((ev.get("label") or {}).get("name", ""))
        if not name.startswith(FOUNDER_LABEL_PREFIX):
            continue
        events.append({"event": ev["event"], "label": name,
                       "actor_node_id": str((ev.get("actor") or {}).get("node_id", "")),
                       "created_at": str(ev["created_at"]), "id": int(ev.get("id") or 0)})
    return sorted(events, key=lambda e: (e["created_at"], e["id"]))


def collect_reference(client: GitHubClient, ledger: dict[str, Any], synthetic: list[dict[str, Any]],
                      enrollment: list[dict[str, Any]], *, collected_at: str) -> dict[str, Any]:
    require_timestamp(collected_at, "collected_at")
    policy = load_policy()
    enrolled = {repo_identity(e["repository"]): e for e in enrollment}
    visibility: dict[str, str] = {}
    out = []
    for rec in _ledger_prs(ledger, synthetic):
        repo, n, head = rec["repository"], rec["pr_number"], rec["head_sha"]
        key = repo_identity(repo)
        entry = enrolled.get(key)
        if entry is None:
            raise ReferenceError("a ledger PR's repository is not enrolled; nothing written")
        if key not in visibility:
            visibility[key] = observed_visibility(client, repo)
        if not (entry["visibility"] == "public" and visibility[key] == "public"):
            # The repository name is deliberately not printed (it may be private).
            raise ReferenceError("reference evidence is collected for public repositories only "
                                 "(founder decision P8); nothing written")
        floor = entry.get("repository_floor", UNKNOWN)
        pr = client.get_json(f"/repos/{repo}/pulls/{n}")
        live_head = str(pr["head"]["sha"])
        exact = live_head == head
        live = {"state": str(pr["state"]), "merged": bool(pr.get("merged")),
                "draft": bool(pr.get("draft")), "head_sha": live_head,
                "base_ref": str(pr["base"]["ref"]),
                "merged_at": pr.get("merged_at"), "merge_commit_sha": pr.get("merge_commit_sha")
                if pr.get("merged") else None}
        item = {
            "repository": repo, "pr_number": n, "head_sha": head,
            "first_head_sha": rec["first_head_sha"], "heads": rec["heads"],
            "live": live, "exact_head": exact,
            "path_tier": _path_tier(client, repo, n, pr, exact, floor),
            "codex": _codex(client, repo, n, head, rec["first_head_sha"]),
            "required_checks": (_required_checks(client, repo, live["base_ref"], head) if exact
                                else {"status": UNKNOWN, "reason": "head_moved", "contexts": []}),
            "outcome": _outcome(client, repo, n, pr, head, collected_at),
            "founder_label_events": _label_events(client, repo, n),
        }
        out.append(item)
    return {"format": REFERENCE_FORMAT, "collected_at": collected_at,
            "reference_policy": policy["provenance"],
            "outcome_window_days": OUTCOME_WINDOW_DAYS,
            "prs": out}
