#!/usr/bin/env python3
"""Deterministic GitHub Actions hardening guards.

Replaces the advisory Bugbot scope for tomrivera-ops/.github and every repo
that consumes the reusable Faraday Review workflow.

Usage:
    python tools/guards/workflow_hardening.py .github/workflows
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_PERMISSIONS = {
    "contents": "read",
    "pull-requests": "write",
    "checks": "write",
    "actions": "read",
}
RANK = {"none": 0, "read": 1, "write": 2}

SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w.-]+)*@[0-9a-f]{40}$")
LOCAL_REF = re.compile(r"^(\./|docker://)")
EVENT_INTERP = re.compile(r"\$\{\{\s*github\.(event|head_ref)[^}]*\}\}")
CHECKOUT_HEAD = re.compile(r"github\.event\.pull_request\.head\.(sha|ref)")


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    severity: str
    message: str

    def as_text(self) -> str:
        return f"{self.path}: [{self.severity}] {self.rule} {self.message}"


def _triggers(doc: dict) -> set[str]:
    # PyYAML parses the bare key `on` as boolean True.
    raw = doc.get("on", doc.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return set(raw)
    if isinstance(raw, dict):
        return set(raw)
    return set()


def _walk_jobs(doc: dict):
    for job_id, job in (doc.get("jobs") or {}).items():
        if isinstance(job, dict):
            yield job_id, job


def check_file(path: Path, raw: str, doc: dict) -> list[Finding]:
    rel = str(path).replace("\\", "/")
    out: list[Finding] = []

    def add(rule: str, sev: str, msg: str) -> None:
        out.append(Finding(rel, rule, sev, msg))

    triggers = _triggers(doc)

    # FW-P0-001 — pull_request_target checking out untrusted head
    if "pull_request_target" in triggers and CHECKOUT_HEAD.search(raw):
        add(
            "FW-P0-001",
            "P0",
            "pull_request_target checks out PR head — fork code executes with "
            "write token and secret access. Use pull_request.",
        )

    # FW-P0-002 — permissions absent or over-scoped
    if "permissions" not in doc and not any("permissions" in j for _, j in _walk_jobs(doc)):
        add("FW-P0-002", "P0", "no 'permissions:' block; inherits default write token.")

    scopes = [("workflow", doc.get("permissions"))]
    scopes += [(jid, job.get("permissions")) for jid, job in _walk_jobs(doc)]
    for where, perms in scopes:
        if not isinstance(perms, dict):
            if perms == "write-all":
                add("FW-P0-002", "P0", f"{where}: permissions 'write-all'.")
            continue
        for scope, level in perms.items():
            ceiling = MAX_PERMISSIONS.get(scope)
            if ceiling is None:
                if RANK.get(str(level), 2) >= RANK["write"]:
                    add(
                        "FW-P0-002",
                        "P0",
                        f"{where}: '{scope}: {level}' is outside the least-privilege set "
                        f"{sorted(MAX_PERMISSIONS)}.",
                    )
            elif RANK.get(str(level), 2) > RANK[ceiling]:
                add("FW-P0-002", "P0", f"{where}: '{scope}: {level}' exceeds ceiling '{ceiling}'.")

    for job_id, job in _walk_jobs(doc):
        # FW-P1-005 — missing timeout
        if "timeout-minutes" not in job and "uses" not in job:
            add("FW-P1-005", "P1", f"job '{job_id}' has no timeout-minutes.")

        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            label = step.get("name") or uses or job_id

            # FW-P0-003 — unpinned action
            if uses and not SHA_PIN.match(uses) and not LOCAL_REF.match(uses):
                add(
                    "FW-P0-003",
                    "P0",
                    f"job '{job_id}' step '{label}': '{uses}' is not pinned to a "
                    "40-char commit SHA.",
                )

            # FW-P0-004 — script injection via event context
            run = step.get("run")
            if isinstance(run, str):
                hit = EVENT_INTERP.search(run)
                if hit:
                    add(
                        "FW-P0-004",
                        "P0",
                        f"job '{job_id}' step '{label}': '{hit.group(0)}' interpolated "
                        "into run: — pass via env: and quote.",
                    )

            # FW-P1-006 — fail-open step
            if step.get("continue-on-error") is True:
                add(
                    "FW-P1-006",
                    "P1",
                    f"job '{job_id}' step '{label}': continue-on-error fails open. "
                    "Governance-relevant steps must block.",
                )

    # FW-P1-007 — no concurrency guard breaks comment idempotency
    if triggers & {"pull_request", "pull_request_target"} and "concurrency" not in doc:
        add(
            "FW-P1-007",
            "P1",
            "no 'concurrency:' group — racing runs can double-post the governed summary.",
        )

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    for path in sorted(list(args.root.rglob("*.yml")) + list(args.root.rglob("*.yaml"))):
        raw = path.read_text(encoding="utf-8")
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            findings.append(Finding(str(path), "FW-P0-000", "P0", f"unparseable: {exc}"))
            continue
        if isinstance(doc, dict):
            findings.extend(check_file(path, raw, doc))

    for f in findings:
        print(f.as_text(), file=sys.stderr)
    p0 = sum(1 for f in findings if f.severity == "P0")
    print(f"workflow_hardening: {p0} P0, {len(findings) - p0} P1", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
