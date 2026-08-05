"""Command-line entry points.

`faraday-validator evaluate-pr` — deterministic evaluation (no network).
`faraday-review …`             — collection, evidence, publication, telemetry.

Exit codes: 0 = command completed (a Block decision is a completed
evaluation); 2 = invocation, configuration, or infrastructure error where
no decision document could be produced.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import REVIEWER_VERSION
from .canonical import write_canonical
from .config import default_config, load_config
from .deterministic.pr_limits import evaluate_limits
from .deterministic.secret_scan import parse_gitleaks_report, run_gitleaks
from .errors import BudgetExceededError, FaradayReviewError
from .evidence.builder import build_evidence
from .evidence.freshness import is_fresh
from .evidence.serialization import write_context, write_evidence
from .fingerprints import evidence_fingerprint, run_fingerprint
from .github.budget import RequestBudget
from .github.client import GitHubClient
from .github.pr_context import collect_context, fetch_head_sha
from .github.publisher import publish_check_run, publish_summary_comment
from .github.secrets import collect_secret_alerts
from .governance.validator_client import evaluate
from .models import DeterministicFinding
from .telemetry.events import build_run_event
from .telemetry.throughput import compute_throughput
from .telemetry.writer import append_event, read_events


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest_source(cli_value: str | None) -> tuple[str | None, str]:
    if cli_value:
        return cli_value, "cli_flag"
    env_value = os.environ.get("FARADAY_POLICY_DIGEST")
    if env_value:
        return env_value, "environment"
    return None, "absent"


def _write_outputs(result, args) -> None:
    write_canonical(Path(args.output), result.decision)
    if args.merge_authorization_output:
        write_canonical(Path(args.merge_authorization_output), result.merge_authorization)
    if args.escalation_output and result.founder_escalation is not None:
        write_canonical(Path(args.escalation_output), result.founder_escalation)


def validator_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faraday-validator")
    sub = parser.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("evaluate-pr", help="deterministic PR evaluation (no network)")
    ev.add_argument("--context", required=True)
    ev.add_argument("--evidence", required=True)
    ev.add_argument("--policy-bundle", required=True)
    ev.add_argument("--expected-policy-digest", default=None)
    ev.add_argument("--output", required=True)
    ev.add_argument("--merge-authorization-output", default=None)
    ev.add_argument("--escalation-output", default=None)
    ev.add_argument("--infrastructure-failure", default=None)
    args = parser.parse_args(argv)

    expected, source = _digest_source(args.expected_policy_digest)
    try:
        result = evaluate(
            context_path=Path(args.context),
            evidence_path=Path(args.evidence),
            bundle_root=Path(args.policy_bundle),
            expected_digest=expected,
            digest_source=source,
            infrastructure_failure=args.infrastructure_failure,
        )
    except (ValueError, FaradayReviewError) as exc:
        print(f"evaluate-pr: {exc}", file=sys.stderr)
        return 2
    _write_outputs(result, args)
    decision = result.decision
    print(
        f"risk_class={decision['risk_class']} "
        f"approval_authorized={decision['approval_authorized']} "
        f"merge_authorized={decision['merge_authorized']} "
        f"blocking_reasons={','.join(decision['blocking_reasons']) or '-'}",
        file=sys.stderr,
    )
    return 0


def _cmd_shadow_run(args) -> int:
    started = time.monotonic()
    config = load_config(Path(args.config)) if args.config else default_config()
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"shadow-run: no token in ${args.token_env}", file=sys.stderr)
        return 2

    artifacts = Path(args.artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    budget = RequestBudget(config.github_api)
    client = GitHubClient(token, config.github_api, budget)

    infrastructure_failure = None
    stale_state = "fresh"
    findings = []
    try:
        context = collect_context(
            client, args.repository, args.pr, sorted(args.required_check or [])
        )
        findings.extend(evaluate_limits(context, config.pr_limits))
        if args.repo_dir:
            report = artifacts / "gitleaks-report.json"
            code = run_gitleaks(Path(args.repo_dir), context.base_sha, context.head_sha, report)
            if code not in (0, 2):
                print(f"shadow-run: gitleaks exited {code}; failing closed", file=sys.stderr)
                return 2
            findings.extend(parse_gitleaks_report(report))
        findings.extend(collect_secret_alerts(client, args.repository))

        # Freshness gate 1: head must still match before evidence is final.
        refetched = fetch_head_sha(client, args.repository, args.pr)
        if not is_fresh(context.head_sha, refetched):
            stale_state = "stale_before_validation"
            findings.append(
                DeterministicFinding(
                    rule_id="head-moved",
                    check="freshness",
                    severity="blocking",
                    message=f"head moved from {context.head_sha} to {refetched} before validation",
                )
            )
        evidence = build_evidence(context, findings)
    except BudgetExceededError as exc:
        # Budget death during collection: no evidence exists to evaluate.
        # This is an infrastructure fault (Correction 2) — fail the run
        # loudly so it pages; it must never be normalized into pr_too_large.
        print(f"shadow-run: github_api_budget_exceeded: {exc}", file=sys.stderr)
        return 2
    context_path = artifacts / "pr-context.json"
    evidence_path = artifacts / "review-evidence.json"
    write_context(context_path, context)
    write_evidence(evidence_path, evidence)

    expected, source = _digest_source(args.expected_policy_digest)
    result = evaluate(
        context_path=context_path,
        evidence_path=evidence_path,
        bundle_root=Path(args.policy_bundle),
        expected_digest=expected,
        digest_source=source,
        infrastructure_failure=infrastructure_failure,
    )
    _write_outputs_shadow(result, artifacts)
    decision = result.decision

    # Freshness gate 2: before publication.
    try:
        refetched = fetch_head_sha(client, args.repository, args.pr)
        if not is_fresh(decision["head_sha"], refetched):
            stale_state = "stale_before_publication"
    except (BudgetExceededError, FaradayReviewError):
        stale_state = "stale_before_publication"

    if not args.skip_publish:
        publish_check_run(client, args.repository, decision)
        publish_summary_comment(client, args.repository, args.pr, decision)

    counts = {"info": 0, "warning": 0, "blocking": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    fingerprint = run_fingerprint(
        args.repository, args.pr, decision["base_sha"], decision["head_sha"],
        decision["policy_digest"], REVIEWER_VERSION,
    )
    event = build_run_event(
        review_id=f"frr-{args.repository.replace('/', '-')}-{args.pr}-{decision['head_sha'][:12]}",
        fingerprint=fingerprint,
        decision=decision,
        finding_counts=counts,
        stale_state=stale_state,
        duration_seconds=round(time.monotonic() - started, 3),
        github_requests_used=budget.requests_used,
        retries=budget.retries_used,
        final_status="completed",
        created_at=_now_iso(),
    )
    append_event(event, root=artifacts)
    print(
        f"shadow-run: risk_class={decision['risk_class']} stale_state={stale_state} "
        f"requests={budget.requests_used} evidence_fingerprint={evidence_fingerprint(evidence.to_json())[:19]}…",
        file=sys.stderr,
    )
    return 0


def _write_outputs_shadow(result, artifacts: Path) -> None:
    write_canonical(artifacts / "decision.json", result.decision)
    write_canonical(artifacts / "merge-authorization.json", result.merge_authorization)
    if result.founder_escalation is not None:
        write_canonical(artifacts / "founder-escalation.json", result.founder_escalation)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faraday-review")
    sub = parser.add_subparsers(dest="command", required=True)

    shadow = sub.add_parser("shadow-run", help="full shadow review of one PR")
    shadow.add_argument("--repository", required=True)
    shadow.add_argument("--pr", type=int, required=True)
    shadow.add_argument("--policy-bundle", required=True)
    shadow.add_argument("--expected-policy-digest", default=None)
    shadow.add_argument("--artifacts-dir", default="artifacts/reviews")
    shadow.add_argument("--repo-dir", default=None, help="checkout for gitleaks diff scan")
    shadow.add_argument("--required-check", action="append", default=[])
    shadow.add_argument("--token-env", default="GITHUB_TOKEN")
    shadow.add_argument("--config", default=None)
    shadow.add_argument("--skip-publish", action="store_true")
    shadow.add_argument("--allow-partial", action="store_true")

    tput = sub.add_parser("throughput", help="metrics from the telemetry log")
    tput.add_argument("--artifacts-dir", default="artifacts/reviews")

    args = parser.parse_args(argv)
    if args.command == "shadow-run":
        try:
            return _cmd_shadow_run(args)
        except FaradayReviewError as exc:
            print(f"shadow-run: {exc.reason}: {exc}", file=sys.stderr)
            return 2
    if args.command == "throughput":
        metrics = compute_throughput(list(read_events(Path(args.artifacts_dir))))
        write_canonical(Path(args.artifacts_dir) / "throughput.json", metrics)
        print(str(metrics), file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
