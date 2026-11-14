"""Command-line entry points.

`faraday-validator evaluate-pr` — deterministic evaluation (no network).
`faraday-review …`             — collection, evidence, publication, telemetry.

Exit codes: 0 = command completed (a Block decision is a completed
evaluation); 2 = invocation, configuration, or infrastructure error where
no decision document could be produced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
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
from .telemetry.adjudication import append_adjudication, build_adjudication_record
from .telemetry.events import build_run_event
from .telemetry.qualification import (
    DEFAULT_FOUNDER_NODE_ID,
    GATE_THRESHOLD_FLOOR,
    QualificationError,
    aggregate,
    file_evidence,
    local_validation_errors,
    readiness,
    require_timestamp,
)
from .telemetry.throughput import compute_throughput
from .telemetry.writer import append_event, read_events


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_event_id() -> str:
    """P2: a fresh UUIDv4 per emission, never derived from content. Module
    level so tests can pin it."""
    return str(uuid.uuid4())


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1: {value!r}")
    return parsed


def _execution_from_args(args) -> dict[str, int | str] | None:
    """`execution` for a GitHub Actions run, or None for a local run. The
    flags are given together or not at all (checked in main)."""
    if args.run_id is None:
        return None
    return {"provider": "github_actions", "run_id": args.run_id,
            "run_attempt": args.run_attempt}


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
        event_id=_new_event_id(),
        execution=_execution_from_args(args),
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


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _write_validated(path: Path, obj, schema: str, command: str) -> int:
    errors = local_validation_errors(schema, obj)
    if errors:
        print(f"{command}: output fails {schema} schema: {errors[:3]}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    write_canonical(path, obj)
    return 0


def _cmd_adjudicate(args) -> int:
    if not _REPOSITORY_RE.match(args.repository) or args.pr < 1 or not _SHA_RE.match(args.head_sha):
        print("adjudicate: invalid --repository, --pr, or --head-sha", file=sys.stderr)
        return 2
    if not args.actor_node_id or not args.founder_node_id:
        print("adjudicate: --actor-node-id and --founder-node-id must be non-empty", file=sys.stderr)
        return 2
    require_timestamp(args.labeled_at, "--labeled-at")
    record = build_adjudication_record(
        repository=args.repository,
        pr_number=args.pr,
        head_sha=args.head_sha,
        label_name=args.label,
        actor_node_id=args.actor_node_id,
        actor_login=args.actor_login,
        founder_node_id=args.founder_node_id,
        labeled_at=args.labeled_at,
    )
    errors = local_validation_errors("founder-adjudication-record", record)
    if errors:
        print(f"adjudicate: record fails schema: {errors[:3]}", file=sys.stderr)
        return 2
    path = append_adjudication(record, Path(args.artifacts_dir))
    print(f"adjudicate: status={record['status']} "
          f"{record.get('reject_reason') or record.get('founder_classification')} -> {path}",
          file=sys.stderr)
    return 0


def _cmd_aggregate(args) -> int:
    prior = None
    if args.prior_ledger:
        try:
            prior = json.loads(Path(args.prior_ledger).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QualificationError(f"cannot read prior ledger {args.prior_ledger}: {exc}") from exc
        if not isinstance(prior, dict):
            raise QualificationError("prior ledger is not a JSON object")
    ledger = aggregate(Path(args.input_dir), generated_at=args.generated_at,
                       founder_node_id=args.founder_node_id, prior_ledger=prior)
    code = _write_validated(Path(args.output), ledger, "qualification-ledger", "aggregate")
    if code == 0:
        print(f"aggregate: {ledger['counts']} anomalies={len(ledger['anomalies'])}", file=sys.stderr)
    return code


def _cmd_readiness(args) -> int:
    ledger_path, synthetic_path = Path(args.ledger), Path(args.synthetic)
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        synthetic = json.loads(synthetic_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"readiness: cannot read inputs: {exc}", file=sys.stderr)
        return 2
    report = readiness(
        ledger, synthetic, generated_at=args.generated_at, threshold=args.threshold,
        ledger_evidence=file_evidence(ledger_path),
        synthetic_evidence=file_evidence(synthetic_path),
    )
    code = _write_validated(Path(args.output), report, "qualification-readiness", "readiness")
    if code == 0:
        print(f"readiness: adjudicated_qualifying_real_pr_count="
              f"{report['adjudicated_qualifying_real_pr_count']} "
              f"qualifying_real_pr_count={report['qualifying_real_pr_count']} "
              f"real_pr_count={report['real_pr_count']} "
              f"threshold={report['gate']['threshold']} met={report['gate']['met']} "
              f"(criterion {report['gate']['criterion_status']})",
              file=sys.stderr)
    return code


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
    shadow.add_argument("--run-id", type=_positive_int, default=None,
                        help="GitHub Actions run id (github.run_id); requires --run-attempt")
    shadow.add_argument("--run-attempt", type=_positive_int, default=None,
                        help="GitHub Actions run attempt (github.run_attempt); requires --run-id")

    tput = sub.add_parser("throughput", help="metrics from the telemetry log")
    tput.add_argument("--artifacts-dir", default="artifacts/reviews")

    adj = sub.add_parser("adjudicate", help="record one founder label event (no network)")
    adj.add_argument("--repository", required=True)
    adj.add_argument("--pr", type=int, required=True)
    adj.add_argument("--head-sha", required=True)
    adj.add_argument("--label", required=True)
    adj.add_argument("--actor-node-id", required=True)
    adj.add_argument("--actor-login", required=True)
    adj.add_argument("--labeled-at", required=True)
    adj.add_argument("--founder-node-id", default=DEFAULT_FOUNDER_NODE_ID)
    adj.add_argument("--artifacts-dir", default="artifacts/reviews")

    agg = sub.add_parser("aggregate", help="fold downloaded run artifacts into one ledger")
    agg.add_argument("--input-dir", required=True)
    agg.add_argument("--output", required=True)
    agg.add_argument("--generated-at", required=True, help="explicit ISO-8601 timestamp; no clock is read")
    agg.add_argument("--founder-node-id", default=DEFAULT_FOUNDER_NODE_ID)
    agg.add_argument("--prior-ledger", default=None,
                     help="previously committed ledger; every input it recorded must be present again")

    rdy = sub.add_parser("readiness", help="Phase 1 qualification gate report from a ledger")
    rdy.add_argument("--ledger", required=True)
    rdy.add_argument("--synthetic", required=True)
    rdy.add_argument("--generated-at", required=True, help="explicit ISO-8601 timestamp; no clock is read")
    rdy.add_argument("--output", required=True)
    rdy.add_argument("--threshold", type=int, default=GATE_THRESHOLD_FLOOR,
                     help=f"real-PR gate; hard floor {GATE_THRESHOLD_FLOOR}, lower values are rejected")

    args = parser.parse_args(argv)
    qualification = {"adjudicate": _cmd_adjudicate, "aggregate": _cmd_aggregate,
                     "readiness": _cmd_readiness}
    if args.command in qualification:
        try:
            return qualification[args.command](args)
        except QualificationError as exc:
            print(f"{args.command}: {exc}", file=sys.stderr)
            return 2
    if args.command == "shadow-run" and (args.run_id is None) != (args.run_attempt is None):
        parser.error("shadow-run: --run-id and --run-attempt must be given together")
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
