"""Fixture builders for qualification tests: one shadow-run artifact dir
per call, shaped exactly like `faraday-review shadow-run` output
(telemetry.jsonl + decision.json), built through the real event builder."""

from __future__ import annotations

import hashlib
from pathlib import Path

from faraday_review import CONTRACT_VERSION, REVIEWER_VERSION
from faraday_review.canonical import write_canonical
from faraday_review.fingerprints import run_fingerprint
from faraday_review.telemetry.adjudication import append_adjudication, build_adjudication_record
from faraday_review.telemetry.events import build_run_event
from faraday_review.telemetry.writer import append_event

FOUNDER = "U_kgDODk3msA"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REPO = "faraday-build/pilot"


def sha(tag: str) -> str:
    return hashlib.sha1(tag.encode()).hexdigest()


def write_run(root: Path, name: str, *, repo: str = REPO, pr: int = 1, head: str = "h1",
              cls: str = "yellow", escalated: bool = True, created_at: str = "2026-11-01T10:00:00Z",
              digest: str = DIGEST_A, reviewer_version: str = REVIEWER_VERSION,
              reasons: tuple[str, ...] = (), stale_state: str = "fresh",
              with_decision: bool = True, final_status: str = "completed",
              failure_mode: str | None = None) -> Path:
    head_sha, base_sha = sha(head), sha("base")
    decision = {
        "contract_version": CONTRACT_VERSION,
        "repository": repo,
        "pr_number": pr,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "reviewer_version": reviewer_version,
        "evidence_class": "deterministic",
        "risk_class": cls,
        "approval_authorized": False,
        "merge_authorized": False,
        "founder_review_required": escalated,
        "blocking_reasons": list(reasons),
        "policy_digest": digest,
    }
    event = build_run_event(
        review_id=f"frr-{repo.replace('/', '-')}-{pr}-{head_sha[:12]}",
        fingerprint=run_fingerprint(repo, pr, base_sha, head_sha, digest, reviewer_version),
        decision=decision,
        finding_counts={"info": 0, "warning": 0, "blocking": 0},
        stale_state=stale_state,
        duration_seconds=1.5,
        github_requests_used=7,
        retries=0,
        final_status=final_status,
        created_at=created_at,
        failure_mode=failure_mode,
    )
    run_dir = root / name
    append_event(event, root=run_dir)
    if with_decision:
        write_canonical(run_dir / "decision.json", decision)
    return run_dir


def write_label(root: Path, name: str, *, repo: str = REPO, pr: int = 1, head: str = "h1",
                label: str = "faraday/founder-yellow", node_id: str = FOUNDER,
                login: str = "tomrivera-PSE", labeled_at: str = "2026-11-01T11:00:00Z") -> dict:
    record = build_adjudication_record(
        repository=repo, pr_number=pr, head_sha=sha(head), label_name=label,
        actor_node_id=node_id, actor_login=login, founder_node_id=FOUNDER,
        labeled_at=labeled_at,
    )
    append_adjudication(record, root / name)
    return record


def write_real_prs(root: Path, count: int, *, repo: str = REPO, start: int = 1) -> None:
    for pr in range(start, start + count):
        write_run(root, f"run-{repo.replace('/', '-')}-{pr}", repo=repo, pr=pr, head=f"h{pr}",
                  created_at=f"2026-11-01T10:{pr % 60:02d}:00Z")
