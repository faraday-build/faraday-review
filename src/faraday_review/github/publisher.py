"""Publish the governed result: one check run and one idempotent comment.

Never approves. Never merges. The comment is located by the idempotency
marker `<!-- faraday-review:repo:pr:head-sha -->`; an existing marked
comment is updated in place, so racing reruns cannot double-post.
"""

from __future__ import annotations

from typing import Any

from ..fingerprints import idempotency_marker
from .client import REQUEST_TIMEOUT_SECONDS, GitHubClient

CHECK_NAME = "faraday-review / governed-summary"


def _summary_body(decision: dict[str, Any], marker: str) -> str:
    reasons = decision["blocking_reasons"]
    lines = [
        marker,
        "## Faraday Review — governed summary (shadow)",
        "",
        f"| | |",
        f"|---|---|",
        f"| Risk class | **{decision['risk_class']}** |",
        f"| Approval authorized | {decision['approval_authorized']} |",
        f"| Merge authorized | {decision['merge_authorized']} |",
        f"| Founder review required | {decision['founder_review_required']} |",
        f"| Head SHA | `{decision['head_sha']}` |",
        f"| Policy | {decision['policy_version']} (`{decision['policy_digest'][:19]}…`) |",
        f"| Evidence fingerprint | `{decision['evidence_fingerprint'][:19]}…` |",
        f"| Reviewer | {decision.get('reviewer_version', '?')} |",
        "",
    ]
    if reasons:
        lines.append("**Blocking reasons:** " + ", ".join(f"`{r}`" for r in reasons))
    else:
        lines.append("No blocking conditions fired.")
    lines.append("")
    lines.append(
        "_Shadow mode: this plane authorizes nothing. Only the Faraday "
        "Validator and the founder may authorize; founder adjudication via "
        "`faraday/founder-green|yellow|red|block` labels._"
    )
    return "\n".join(lines)


def publish_check_run(client: GitHubClient, repository: str, decision: dict[str, Any]) -> int:
    conclusion = "neutral" if decision["risk_class"] in ("green", "yellow", "red") else "failure"
    response = client.request(
        "POST",
        f"/repos/{repository}/check-runs",
        timeout=REQUEST_TIMEOUT_SECONDS,
        json_body={
            "name": CHECK_NAME,
            "head_sha": decision["head_sha"],
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": f"risk_class={decision['risk_class']} (shadow, authorizes nothing)",
                "summary": _summary_body(decision, marker=""),
            },
        },
    )
    return int(response.json()["id"])


def publish_summary_comment(
    client: GitHubClient, repository: str, pr_number: int, decision: dict[str, Any]
) -> int:
    marker = idempotency_marker(repository, pr_number, decision["head_sha"])
    body = _summary_body(decision, marker)
    comments = client.paginate(f"/repos/{repository}/issues/{pr_number}/comments")
    for comment in comments:
        existing = str(comment.get("body", ""))
        if f"<!-- faraday-review:{repository}:{pr_number}:" in existing:
            response = client.request(
                "PATCH",
                f"/repos/{repository}/issues/comments/{comment['id']}",
                json_body={"body": body},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            return int(response.json()["id"])
    response = client.request(
        "POST",
        f"/repos/{repository}/issues/{pr_number}/comments",
        json_body={"body": body},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return int(response.json()["id"])
