"""`faraday-review preflight --repo X` (GHO-004 P2, item 8).

Read-only (GET only) activation checks for one caller repository:

* `policy_digest`: FARADAY_POLICY_DIGEST resolves for the repo (repository
  variable, else an organization variable visible to it) and equals the
  governance v1.1.0 digest in docs/DEPLOYMENT.md;
* `labels`: the four faraday/founder-* labels exist;
* `caller_pin`: the caller workflow pins shadow.yml by a full SHA, passes
  the same `reviewer_sha`, and that SHA is on faraday-review main;
* the enable variable FARADAY_REVIEW_SHADOW_ENABLED is reported, not gated.

Anything the token cannot read is `unknown`, and unknown is never a pass.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from urllib.parse import quote

import yaml

from ..errors import GitHubApiError
from .client import GitHubClient

# governance v1.1.0 review-policy bundle digest (docs/DEPLOYMENT.md; a test
# pins the two together).
RELEASED_POLICY_DIGEST = "sha256:32eeb3e07271fff267908bc428d77477caa403d9e0f8b0a0244d8c26aec73e12"
DIGEST_VARIABLE = "FARADAY_POLICY_DIGEST"
ENABLE_VARIABLE = "FARADAY_REVIEW_SHADOW_ENABLED"
FOUNDER_LABELS = ("faraday/founder-green", "faraday/founder-yellow", "faraday/founder-red",
                  "faraday/founder-block")
REVIEWER_REPO = "faraday-build/faraday-review"
SHADOW_USES = re.compile(r"^faraday-build/faraday-review/\.github/workflows/shadow\.yml@(.+)$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _status_of(exc: GitHubApiError) -> int | None:
    match = re.search(r"-> (\d{3})", str(exc))
    return int(match.group(1)) if match else None


def _get(client: GitHubClient, path: str) -> tuple[str, Any]:
    """("ok", body) | ("absent", None) on 404 | ("unknown", reason)."""
    try:
        return "ok", client.get_json(path)
    except GitHubApiError as exc:
        status = _status_of(exc)
        if status == 404:
            return "absent", None
        return "unknown", f"HTTP {status}" if status else "request failed"


def _variable(client: GitHubClient, repository: str, name: str,
              repo_visibility: str) -> dict[str, Any]:
    """Resolve an Actions variable as the repository's workflows see it:
    repository level wins, else an organization variable whose visibility
    includes the repository."""
    state, body = _get(client, f"/repos/{repository}/actions/variables/{name}")
    if state == "ok":
        return {"status": "set", "value": body.get("value"), "scope": "repository"}
    if state == "unknown":
        return {"status": "unknown", "reason": f"repository variable: {body}"}
    owner = repository.split("/")[0]
    state, body = _get(client, f"/orgs/{owner}/actions/variables/{name}")
    if state == "absent":
        return {"status": "absent"}
    if state == "unknown":
        return {"status": "unknown", "reason": f"organization variable: {body}"}
    visibility = body.get("visibility")
    if visibility == "all" or (visibility == "private" and repo_visibility != "public"):
        return {"status": "set", "value": body.get("value"), "scope": "organization"}
    if visibility == "selected":
        state, listed = _get(client, f"/orgs/{owner}/actions/variables/{name}/repositories")
        if state != "ok":
            return {"status": "unknown", "reason": "organization variable repository list unreadable"}
        names = {str(r.get("full_name", "")).casefold() for r in listed.get("repositories", [])}
        if repository.casefold() in names:
            return {"status": "set", "value": body.get("value"), "scope": "organization"}
    return {"status": "absent", "reason": f"organization variable not visible (visibility={visibility})"}


def _check_digest(client, repository, repo_visibility) -> tuple[dict, list[str]]:
    var = _variable(client, repository, DIGEST_VARIABLE, repo_visibility)
    if var["status"] == "unknown":
        return ({"check": "policy_digest", "status": "unknown", "detail": var["reason"]},
                [f"{DIGEST_VARIABLE}: unknown (token cannot read variables: {var['reason']})"])
    if var["status"] != "set" or not var.get("value"):
        return ({"check": "policy_digest", "status": "fail", "detail": "unset"},
                [f"{DIGEST_VARIABLE} is not set for {repository} (every run would Block "
                 "policy_digest_unpinned)"])
    if var["value"] != RELEASED_POLICY_DIGEST:
        return ({"check": "policy_digest", "status": "fail",
                 "detail": f"{var['scope']} value {var['value']} != released"},
                [f"{DIGEST_VARIABLE} = {var['value']} does not equal the governance v1.1.0 "
                 f"digest {RELEASED_POLICY_DIGEST}"])
    return {"check": "policy_digest", "status": "pass", "detail": f"{var['scope']} variable"}, []


def _check_labels(client, repository) -> tuple[dict, list[str]]:
    gaps, unknown = [], []
    for label in FOUNDER_LABELS:
        state, body = _get(client, f"/repos/{repository}/labels/{quote(label, safe='')}")
        if state == "absent":
            gaps.append(f"label {label} missing")
        elif state == "unknown":
            unknown.append(f"label {label}: unknown ({body})")
    status = "fail" if gaps else ("unknown" if unknown else "pass")
    return ({"check": "labels", "status": status,
             "detail": f"{len(FOUNDER_LABELS) - len(gaps) - len(unknown)}/{len(FOUNDER_LABELS)} present"},
            gaps + unknown)


def _shadow_jobs(doc: Any) -> list[dict[str, Any]]:
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return []
    return [job for job in jobs.values()
            if isinstance(job, dict) and isinstance(job.get("uses"), str)
            and SHADOW_USES.match(job["uses"])]


def _check_caller(client, repository, workflow) -> tuple[dict, list[str], str | None]:
    def fail(detail: str, status: str = "fail") -> tuple[dict, list[str], None]:
        return ({"check": "caller_pin", "status": status, "detail": detail},
                [f"caller workflow {workflow}: {detail}"], None)

    state, body = _get(client, f"/repos/{repository}/contents/{workflow}")
    if state == "absent":
        return fail("not found on the default branch")
    if state == "unknown":
        return fail(f"unknown ({body})", "unknown")
    try:
        text = base64.b64decode(body.get("content", ""), validate=False).decode("utf-8")
        doc = yaml.safe_load(text)
    except (binascii.Error, UnicodeDecodeError, yaml.YAMLError, AttributeError):
        return fail("unparseable")
    jobs = _shadow_jobs(doc)
    if len(jobs) != 1:
        return fail(f"expected exactly one job using {REVIEWER_REPO} shadow.yml, found {len(jobs)}")
    job = jobs[0]
    ref = SHADOW_USES.match(job["uses"]).group(1)
    if not FULL_SHA.match(ref):
        return fail(f"shadow.yml is pinned to {ref!r}, not a full commit SHA")
    reviewer_sha = str((job.get("with") or {}).get("reviewer_sha", ""))
    if reviewer_sha != ref:
        return fail(f"reviewer_sha {reviewer_sha!r} does not equal the uses: ref {ref}")
    state, cmp = _get(client, f"/repos/{REVIEWER_REPO}/compare/{ref}...main")
    if state == "unknown":
        return fail(f"reachability from main unknown ({cmp})", "unknown")
    status = cmp.get("status") if state == "ok" else "not_found"
    if status not in ("ahead", "identical"):
        return fail(f"{ref} is not on {REVIEWER_REPO} main (compare status: {status})")
    return ({"check": "caller_pin", "status": "pass", "detail": f"pinned {ref} ({status})"},
            [], ref)


def preflight(client: GitHubClient, repository: str, *,
              workflow: str = ".github/workflows/faraday-review.yml") -> dict[str, Any]:
    state, repo = _get(client, f"/repos/{repository}")
    if state == "ok" and isinstance(repo, dict):
        repo_visibility = "public" if repo.get("visibility") == "public" else "private"
    else:
        repo_visibility = "unknown"
    checks, gaps = [], []
    if repo_visibility == "unknown":
        checks.append({"check": "repository", "status": "unknown", "detail": "cannot read repository"})
        gaps.append(f"repository {repository}: unknown (cannot read)")
    for check, found in (_check_digest(client, repository, repo_visibility),
                         _check_labels(client, repository)):
        checks.append(check)
        gaps.extend(found)
    check, found, pinned = _check_caller(client, repository, workflow)
    checks.append(check)
    gaps.extend(found)
    enable = _variable(client, repository, ENABLE_VARIABLE, repo_visibility)
    enable = {k: v for k, v in enable.items() if k in ("status", "value", "scope", "reason")}
    return {
        "repository": repository,
        "workflow": workflow,
        "pinned_reviewer_sha": pinned,
        "released_policy_digest": RELEASED_POLICY_DIGEST,
        "checks": checks,
        "enable_variable": enable,
        "gaps": gaps,
        "ready": not gaps and all(c["status"] == "pass" for c in checks),
    }
