"""Phase 1 qualification: cross-run ledger and readiness report.

Shadow runs execute on ephemeral runners; each uploads its artifacts dir
(telemetry.jsonl, decision.json, adjudications.jsonl) as an Actions
artifact. `aggregate` folds any number of downloaded artifact trees into one
deterministic ledger; `readiness` derives the machine-readable Phase 1 gate
report from that ledger.

Determinism: no clock (generated_at is an explicit argument), no network,
no set iteration reaching output, canonical serialization only.

Authorization boundary: this module is a telemetry *consumer*. It never
emits authorization fields. The Validator's class is carried under the
neutral name `classifier_class`, `founder_review_required` as `escalated`,
and the bundle digest as `policy_bundle_digest` — values copied verbatim
from the run's own telemetry/decision, never derived or overridden here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ..canonical import canonical_bytes
from ..evidence.schema_validation import validation_errors
from ..fingerprints import run_fingerprint
from .adjudication import ADJUDICATIONS_FILE, VALID_CLASSIFICATIONS
from .throughput import _parse, _percentile

TELEMETRY_FILE = "telemetry.jsonl"
DECISION_FILE = "decision.json"
DEFAULT_FOUNDER_NODE_ID = "U_kgDODk3msA"
LEDGER_FORMAT = "faraday-review/qualification-ledger/v1"
READINESS_FORMAT = "faraday-review/qualification-readiness/v1"

# Hard floor. The Phase 1 gate may be raised, never lowered.
GATE_THRESHOLD_FLOOR = 20

# Severity order used for over/under-escalation direction.
CLASS_ORDER = VALID_CLASSIFICATIONS  # ("green", "yellow", "red", "block")

LOCAL_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

BUDGET_REASON = "github_api_budget_exceeded"
TOO_LARGE_REASON = "pr_too_large_for_deterministic_classification"


class QualificationError(ValueError):
    """Invalid qualification input; CLI maps this to exit 2."""


# ---------------------------------------------------------------- schemas


@lru_cache(maxsize=None)
def _local_registry() -> Registry:
    """Offline registry of the repo-local schemas; nothing is ever fetched."""
    resources = []
    for path in sorted(LOCAL_SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        resources.append((schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _local_validator(name: str) -> Draft202012Validator:
    schema = json.loads((LOCAL_SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=_local_registry())


def local_validation_errors(name: str, instance: Any) -> list[str]:
    """Sorted errors against a repo-local (non-vendored) schema."""
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in _local_validator(name).iter_errors(instance)
    )


# ---------------------------------------------------------------- helpers


def require_timestamp(value: str, what: str) -> str:
    if _aware(value) is None:
        raise QualificationError(f"{what}: not a timezone-aware ISO-8601 timestamp: {value!r}")
    return value


def repo_identity(repository: str) -> str:
    """Normalized repository identity. GitHub owner/repo names are
    case-insensitive, so every identity comparison (correlation, synthetic
    matching, counting) uses the casefolded form."""
    return repository.casefold()


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = _parse(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sort_key_ts(value: str | None) -> tuple[int, datetime | str]:
    parsed = _aware(value)
    return (1, parsed) if parsed is not None else (0, value or "")


def _sorted_unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[bytes, dict[str, Any]] = {}
    for item in items:
        seen.setdefault(canonical_bytes(item), item)
    return [seen[k] for k in sorted(seen)]


def _read_jsonl(path: Path, rel: str, anomalies: list[dict[str, Any]]) -> list[tuple[int, dict]]:
    rows: list[tuple[int, dict]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            anomalies.append({"kind": "malformed_line", "source": rel, "line": lineno})
            continue
        if not isinstance(obj, dict):
            anomalies.append({"kind": "malformed_line", "source": rel, "line": lineno})
            continue
        rows.append((lineno, obj))
    return rows


# ---------------------------------------------------------------- aggregate


def _run_record(event: dict[str, Any], decision: dict[str, Any] | None, rel: str,
                anomalies: list[dict[str, Any]]) -> dict[str, Any]:
    digest_value = None
    verified = False
    if decision is not None and event.get("reviewer_version"):
        candidate = decision.get("policy_digest")
        recomputed = run_fingerprint(
            event["repository"], event["pr_number"], event["base_sha"], event["head_sha"],
            candidate, event["reviewer_version"],
        )
        if recomputed == event["fingerprint"] and isinstance(candidate, str):
            digest_value = candidate
            verified = True
    if not verified:
        anomalies.append({"kind": "policy_digest_unverified", "source": rel,
                          "review_id": event["review_id"]})
    if event.get("approval_authorized") is True or event.get("merge_authorized") is True:
        anomalies.append({"kind": "authorization_flag_true_in_telemetry", "source": rel,
                          "review_id": event["review_id"]})
    return {
        "review_id": event["review_id"],
        "fingerprint": event["fingerprint"],
        "repository": event["repository"],
        "pr_number": event["pr_number"],
        "base_sha": event["base_sha"],
        "head_sha": event["head_sha"],
        "reviewer_version": event.get("reviewer_version"),
        "policy_bundle_digest": digest_value,
        "policy_digest_verified": verified,
        "classifier_class": event.get("risk_class"),
        "escalated": bool(event.get("founder_review_required")),
        "blocking_reasons": sorted(event.get("blocking_reasons") or []),
        "stale_state": event.get("stale_state"),
        "final_status": event["final_status"],
        "failure_mode": event.get("failure_mode"),
        "created_at": event["created_at"],
    }


def _collapse(group: list[tuple[dict[str, Any], str]], anomalies: list[dict[str, Any]]) -> dict[str, Any]:
    """One run per (review_id, fingerprint): byte-identical uploads and
    same-input re-runs collapse. The LATEST completed fresh record represents,
    because the run fingerprint excludes observed check states, so a later
    fresh re-run can legitimately carry a newer classification (Codex P2,
    e44f0e8). first_created_at keeps the earliest occurrence for latency;
    last_fresh_created_at considers fresh occurrences only."""
    best = max(
        group,
        key=lambda rs: (rs[0]["final_status"] == "completed", rs[0]["stale_state"] == "fresh",
                        _sort_key_ts(rs[0]["created_at"]), canonical_bytes(rs[0]), rs[1]),
    )
    rep = dict(best[0])
    classes = sorted({str(r["classifier_class"]) for r, _ in group})
    if len(classes) > 1:
        anomalies.append({"kind": "classifier_class_differs_for_same_fingerprint",
                          "review_id": rep["review_id"], "classes": classes})
    rep["first_created_at"] = min((r["created_at"] for r, _ in group), key=_sort_key_ts)
    rep["last_created_at"] = max((r["created_at"] for r, _ in group), key=_sort_key_ts)
    fresh = [r["created_at"] for r, _ in group if r["stale_state"] == "fresh"]
    rep["last_fresh_created_at"] = max(fresh, key=_sort_key_ts) if fresh else None
    # Per-state counts over distinct payloads (byte-identical re-uploads count
    # once), so a stale re-run collapsed into a fresh representative is still
    # reported (Codex P2, d1e0437).
    distinct: dict[bytes, str] = {}
    for r, _ in group:
        distinct.setdefault(canonical_bytes(r), str(r["stale_state"]))
    state_counts: dict[str, int] = {}
    for state in distinct.values():
        state_counts[state] = state_counts.get(state, 0) + 1
    rep["occurrence_stale_states"] = dict(sorted(state_counts.items()))
    rep["occurrences"] = len(group)
    rep["distinct_payloads"] = len({canonical_bytes(r) for r, _ in group})
    rep["sources"] = sorted({s for _, s in group})
    return rep


def _prior_inputs(prior_ledger: dict[str, Any], founder_node_id: str) -> set[tuple[str, str]]:
    """(kind, sha256) of every input a previously committed ledger was built
    from. The ledger is a summary, not a re-ingestible sample store, so a
    later aggregation must re-fold the archived raw inputs; this set is what
    proves none were dropped (Codex P2, 4d18890)."""
    errors = local_validation_errors("qualification-ledger", prior_ledger)
    if errors:
        raise QualificationError(f"prior ledger fails qualification-ledger schema: {errors[:3]}")
    if prior_ledger.get("format") != LEDGER_FORMAT:
        raise QualificationError(f"prior ledger format {prior_ledger.get('format')!r} != {LEDGER_FORMAT}")
    if prior_ledger.get("founder_node_id") != founder_node_id:
        raise QualificationError("prior ledger founder_node_id differs from this aggregation")
    return {(i["kind"], i["sha256"]) for i in prior_ledger["inputs"]}


def aggregate(input_dir: Path, *, generated_at: str,
              founder_node_id: str = DEFAULT_FOUNDER_NODE_ID,
              prior_ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fold raw run artifacts into a ledger. With `prior_ledger`, every input
    that ledger recorded must be present again (matched by kind + sha256,
    any path), or aggregation fails closed: a later window can add samples
    but can never silently lose earlier ones."""
    require_timestamp(generated_at, "generated_at")
    if not input_dir.is_dir():
        raise QualificationError(f"input dir {input_dir} is not a directory")
    required_prior = _prior_inputs(prior_ledger, founder_node_id) if prior_ledger is not None else set()

    anomalies: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    raw_runs: list[tuple[dict[str, Any], str]] = []
    raw_adjudications: list[tuple[dict[str, Any], str]] = []

    wanted = {TELEMETRY_FILE: "telemetry", ADJUDICATIONS_FILE: "adjudications",
              DECISION_FILE: "decision"}
    paths = sorted(p for p in input_dir.rglob("*") if p.name in wanted)
    for path in paths:
        rel = path.relative_to(input_dir).as_posix()
        if path.is_symlink() or not path.is_file():
            anomalies.append({"kind": "non_regular_file_skipped", "source": rel})
            continue
        inputs.append({"path": rel, "kind": wanted[path.name],
                       "sha256": _sha256_bytes(path.read_bytes())})

    missing_prior = required_prior - {(i["kind"], i["sha256"]) for i in inputs}
    if missing_prior:
        sample = ", ".join(f"{k}:{h}" for k, h in sorted(missing_prior)[:3])
        raise QualificationError(
            f"{len(missing_prior)} input(s) recorded in the prior ledger are missing from "
            f"{input_dir} (archive them under docs/qualification/artifacts/): {sample}")

    for path in paths:
        rel = path.relative_to(input_dir).as_posix()
        if path.is_symlink() or not path.is_file():
            continue
        if path.name == TELEMETRY_FILE:
            decision = None
            decision_path = path.parent / DECISION_FILE
            if decision_path.is_file() and not decision_path.is_symlink():
                try:
                    loaded = json.loads(decision_path.read_text(encoding="utf-8"))
                    decision = loaded if isinstance(loaded, dict) else None
                except json.JSONDecodeError:
                    anomalies.append({"kind": "malformed_decision",
                                      "source": decision_path.relative_to(input_dir).as_posix()})
            for lineno, event in _read_jsonl(path, rel, anomalies):
                errors = validation_errors("review-telemetry-event", event)
                if not errors and _aware(event.get("created_at")) is None:
                    errors = ["created_at: not a timezone-aware ISO-8601 timestamp"]
                if errors:
                    anomalies.append({"kind": "invalid_telemetry_event", "source": rel,
                                      "line": lineno, "errors": errors[:5]})
                    continue
                raw_runs.append((_run_record(event, decision, rel, anomalies), rel))
        elif path.name == ADJUDICATIONS_FILE:
            for lineno, record in _read_jsonl(path, rel, anomalies):
                errors = local_validation_errors("founder-adjudication-record", record)
                if not errors and _aware(record.get("labeled_at")) is None:
                    errors = ["labeled_at: not a timezone-aware ISO-8601 timestamp"]
                if errors:
                    anomalies.append({"kind": "invalid_adjudication_record", "source": rel,
                                      "line": lineno, "errors": errors[:5]})
                    continue
                raw_adjudications.append((record, rel))

    # Deduplicate runs by run identity.
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    for run, rel in raw_runs:
        groups.setdefault((run["review_id"], run["fingerprint"]), []).append((run, rel))
    runs = [_collapse(groups[k], anomalies) for k in sorted(groups)]

    # Correlate into entries keyed by (repository identity, pr, head, digest).
    entries_by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for run in runs:
        key = (repo_identity(run["repository"]), run["pr_number"], run["head_sha"],
               run["policy_bundle_digest"] or "")
        entry = entries_by_key.setdefault(key, {
            "repository": key[0],
            "repository_spellings": [],
            "pr_number": run["pr_number"],
            "head_sha": run["head_sha"],
            "policy_bundle_digest": run["policy_bundle_digest"],
            "runs": [],
        })
        entry["runs"].append(run)
        if run["repository"] not in entry["repository_spellings"]:
            entry["repository_spellings"] = sorted([*entry["repository_spellings"],
                                                    run["repository"]])

    # Current head: per PR, the head whose newest FRESH run is newest. A run
    # whose freshness gate saw the head move (stale_*) never promotes its
    # head; a PR with no fresh run has no current head at all.
    head_latest: dict[tuple[str, int], dict[str, Any]] = {}
    all_prs = sorted({(k[0], k[1]) for k in entries_by_key})
    for (repo, pr, head, _), entry in entries_by_key.items():
        fresh = [r["last_fresh_created_at"] for r in entry["runs"] if r["last_fresh_created_at"]]
        if not fresh:
            continue
        latest = max(fresh, key=_sort_key_ts)
        bucket = head_latest.setdefault((repo, pr), {})
        if head not in bucket or _sort_key_ts(latest) > _sort_key_ts(bucket[head]):
            bucket[head] = latest
    current_head: dict[tuple[str, int], str | None] = {}
    for pr_key in all_prs:
        if pr_key not in head_latest:
            current_head[pr_key] = None
            anomalies.append({"kind": "no_fresh_run_for_pr", "repository": pr_key[0],
                              "pr_number": pr_key[1]})
            continue
        ranked = sorted(head_latest[pr_key].items(),
                        key=lambda hv: (_sort_key_ts(hv[1]), hv[0]), reverse=True)
        current_head[pr_key] = ranked[0][0]
        if len(ranked) > 1 and _sort_key_ts(ranked[0][1]) == _sort_key_ts(ranked[1][1]):
            anomalies.append({"kind": "current_head_ambiguous", "repository": pr_key[0],
                              "pr_number": pr_key[1],
                              "heads": sorted(h for h, _ in ranked)})

    # Founder adjudications: re-check identity, dedupe, attach by exact head.
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record, _ in raw_adjudications:
        if record["status"] == "accepted":
            if record["actor"]["node_id"] != founder_node_id:
                demoted = dict(record)
                demoted["status"] = "rejected"
                demoted["reject_reason"] = "actor_not_founder_at_aggregate"
                demoted.pop("founder_classification", None)
                rejected.append(demoted)
            else:
                accepted.append(record)
        else:
            rejected.append(record)
    accepted = _sorted_unique(accepted)
    rejected = _sorted_unique(rejected)

    by_head: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for record in accepted:
        by_head.setdefault((repo_identity(record["repository"]), record["pr_number"],
                            record["head_sha"]), []).append(record)
    unmatched: list[dict[str, Any]] = []
    run_heads = {(k[0], k[1], k[2]) for k in entries_by_key}
    for head_key in sorted(by_head):
        if head_key not in run_heads:
            unmatched.extend(by_head[head_key])

    entries: list[dict[str, Any]] = []
    for key in sorted(entries_by_key):
        entry = entries_by_key[key]
        entry["runs"] = sorted(entry["runs"], key=lambda r: (_sort_key_ts(r["created_at"]),
                                                             r["fingerprint"], r["review_id"]))
        current = current_head[(key[0], key[1])]
        entry["current"] = current == key[2]
        entry["superseded"] = current is not None and current != key[2]
        records = by_head.get((key[0], key[1], key[2]), [])
        if records:
            ranked = sorted(records, key=lambda r: (_sort_key_ts(r["labeled_at"]), r["label"]))
            latest = ranked[-1]
            entry["founder_adjudication"] = {
                "founder_classification": latest["founder_classification"],
                "labeled_at": latest["labeled_at"],
                "label": latest["label"],
                "actor": latest["actor"],
                "accepted_records": len(records),
            }
            distinct = sorted({r["founder_classification"] for r in records})
            if len(distinct) > 1:
                anomalies.append({"kind": "founder_relabeled_head", "repository": key[0],
                                  "pr_number": key[1], "head_sha": key[2],
                                  "classifications": distinct})
        else:
            entry["founder_adjudication"] = None
        entries.append(entry)

    for record in unmatched:
        anomalies.append({"kind": "adjudication_without_run",
                          "repository": repo_identity(record["repository"]),
                          "pr_number": record["pr_number"], "head_sha": record["head_sha"]})

    return {
        "format": LEDGER_FORMAT,
        "generated_at": generated_at,
        "founder_node_id": founder_node_id,
        "inputs": sorted(inputs, key=lambda i: (i["path"], i["kind"])),
        "entries": entries,
        "adjudications": {
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "rejected": rejected,
            "unmatched": _sorted_unique(unmatched),
        },
        "counts": {
            "events_read": len(raw_runs),
            "runs_after_dedup": len(runs),
            "duplicates_collapsed": len(raw_runs) - len(runs),
            "entries": len(entries),
            "superseded_entries": sum(1 for e in entries if e["superseded"]),
        },
        "anomalies": _sorted_unique(anomalies),
    }


# ---------------------------------------------------------------- readiness


def _summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {"n": len(ordered), "median_seconds": _percentile(ordered, 0.5),
            "p90_seconds": _percentile(ordered, 0.9)}


def readiness(ledger: dict[str, Any], synthetic: list[dict[str, Any]], *, generated_at: str,
              ledger_evidence: dict[str, str], synthetic_evidence: dict[str, str],
              threshold: int = GATE_THRESHOLD_FLOOR) -> dict[str, Any]:
    require_timestamp(generated_at, "generated_at")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < GATE_THRESHOLD_FLOOR:
        raise QualificationError(
            f"gate threshold {threshold!r} rejected: hard floor is {GATE_THRESHOLD_FLOOR}")
    errors = local_validation_errors("qualification-ledger", ledger)
    if errors:
        raise QualificationError(f"ledger fails schema: {errors[:3]}")
    errors = local_validation_errors("synthetic-prs", synthetic)
    if errors:
        raise QualificationError(f"synthetic list fails schema: {errors[:3]}")

    anomalies: list[dict[str, Any]] = list(ledger["anomalies"])
    synthetic_keys = {(repo_identity(s["repository"]), s["pr_number"]) for s in synthetic}
    synthetic_seen: set[tuple[str, int]] = set()
    for s in synthetic:
        skey = (repo_identity(s["repository"]), s["pr_number"])
        if skey in synthetic_seen:
            raise QualificationError(
                f"synthetic list repeats {skey[0]}#{skey[1]} (repository identity is case-insensitive)")
        synthetic_seen.add(skey)
    ledger_prs = {(repo_identity(e["repository"]), e["pr_number"]) for e in ledger["entries"]}

    synthetic_excluded = _sorted_unique([
        {"repository": repo_identity(s["repository"]), "pr_number": s["pr_number"],
         "reason": s["reason"],
         "present_in_ledger": (repo_identity(s["repository"]), s["pr_number"]) in ledger_prs}
        for s in synthetic
    ])

    real_entries = [e for e in ledger["entries"]
                    if (repo_identity(e["repository"]), e["pr_number"]) not in synthetic_keys]
    current_entries = [e for e in real_entries if e["current"]]

    # One counted run per PR: latest completed FRESH run on the current head.
    # Stale-but-completed runs are known-stale evidence and never count.
    per_pr: dict[tuple[str, int], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for entry in current_entries:
        for run in entry["runs"]:
            if run["final_status"] == "completed" and run["stale_state"] == "fresh":
                per_pr.setdefault((repo_identity(entry["repository"]), entry["pr_number"]),
                                  []).append((entry, run))

    counted: list[dict[str, Any]] = []
    for pr_key in sorted(per_pr):
        pairs = per_pr[pr_key]
        entry, run = max(pairs, key=lambda er: (_sort_key_ts(er[1]["last_fresh_created_at"]),
                                                er[1]["fingerprint"]))
        first_seen = min((r["first_created_at"] for _, r in pairs), key=_sort_key_ts)
        classes = sorted({str(r["classifier_class"]) for _, r in pairs})
        if len(classes) > 1:
            anomalies.append({"kind": "classifier_class_differs_on_current_head",
                              "repository": pr_key[0], "pr_number": pr_key[1],
                              "classes": classes})
        counted.append({"entry": entry, "run": run, "first_seen": first_seen,
                        "all_runs": [r for _, r in pairs]})

    # Agreement.
    matrix = {c: {f: 0 for f in CLASS_ORDER} for c in CLASS_ORDER}
    agree = disagree = over = under = 0
    disagreements: list[dict[str, Any]] = []
    latencies_all: list[float] = []
    latencies_escalated: list[float] = []
    adjudicated = 0
    for item in counted:
        entry, run = item["entry"], item["run"]
        adj = entry["founder_adjudication"]
        if adj is None:
            continue
        adjudicated += 1
        cls, founder = run["classifier_class"], adj["founder_classification"]
        if cls not in CLASS_ORDER:
            anomalies.append({"kind": "adjudicated_run_without_class",
                              "repository": entry["repository"], "pr_number": entry["pr_number"]})
        else:
            matrix[cls][founder] += 1
            if cls == founder:
                agree += 1
            else:
                disagree += 1
                direction = ("over_escalation" if CLASS_ORDER.index(cls) > CLASS_ORDER.index(founder)
                             else "under_escalation")
                if direction == "over_escalation":
                    over += 1
                else:
                    under += 1
                disagreements.append({"repository": entry["repository"],
                                      "pr_number": entry["pr_number"],
                                      "head_sha": entry["head_sha"], "classifier_class": cls,
                                      "founder_classification": founder, "direction": direction})
        latency = (_parse(adj["labeled_at"]) - _parse(item["first_seen"])).total_seconds()
        if latency < 0:
            anomalies.append({"kind": "adjudication_precedes_first_run",
                              "repository": entry["repository"], "pr_number": entry["pr_number"],
                              "head_sha": entry["head_sha"]})
            continue
        latencies_all.append(latency)
        if run["escalated"]:
            latencies_escalated.append(latency)

    compared = agree + disagree
    by_class = {c: 0 for c in CLASS_ORDER}
    escalated = 0
    too_large = budget = 0
    for item in counted:
        run = item["run"]
        if run["classifier_class"] in by_class:
            by_class[run["classifier_class"]] += 1
        escalated += 1 if run["escalated"] else 0
        too_large += 1 if TOO_LARGE_REASON in run["blocking_reasons"] else 0
        budget += 1 if BUDGET_REASON in run["blocking_reasons"] else 0

    # Drift and infrastructure health over every real run (incl. superseded).
    current_runs = [r for item in counted for r in item["all_runs"]]
    all_real_runs = [r for e in real_entries for r in e["runs"]]
    digests = sorted({r["policy_bundle_digest"] for r in current_runs if r["policy_bundle_digest"]})
    versions = sorted({r["reviewer_version"] for r in current_runs if r["reviewer_version"]})
    unverified = sum(1 for r in current_runs if not r["policy_digest_verified"])
    if len(digests) > 1:
        anomalies.append({"kind": "policy_digest_drift", "values": digests})
    if len(versions) > 1:
        anomalies.append({"kind": "reviewer_version_drift", "values": versions})
    stale_counts: dict[str, int] = {}
    failure_modes: dict[str, int] = {}
    for run in all_real_runs:
        per_state = run.get("occurrence_stale_states") or {run["stale_state"] or "unknown": 1}
        for state, n in sorted(per_state.items()):
            state = state if state and state != "None" else "unknown"
            stale_counts[state] = stale_counts.get(state, 0) + n
        if run["final_status"] == "failed":
            mode = run["failure_mode"] or "unspecified"
            failure_modes[mode] = failure_modes.get(mode, 0) + 1

    real_pr_count = len(counted)
    return {
        "format": READINESS_FORMAT,
        "generated_at": generated_at,
        "real_pr_count": real_pr_count,
        "counted_prs": [
            {"repository": i["entry"]["repository"], "pr_number": i["entry"]["pr_number"],
             "head_sha": i["entry"]["head_sha"], "review_id": i["run"]["review_id"],
             "classifier_class": i["run"]["classifier_class"],
             "adjudicated": i["entry"]["founder_adjudication"] is not None}
            for i in counted
        ],
        "synthetic_excluded": synthetic_excluded,
        "adjudicated_count": adjudicated,
        "agreement": {
            "compared": compared,
            "agree": agree,
            "disagree": disagree,
            "agreement_rate": (round(agree / compared, 6) if compared else None),
            "confusion_matrix": matrix,
            "direction": {"over_escalation": over, "under_escalation": under},
            "disagreements": disagreements,
        },
        "escalation": {
            "escalated_count": escalated,
            "classifier_class_distribution": by_class,
            "pr_too_large_escalations": too_large,
            "api_budget_exceeded_escalations": budget,
        },
        "founder_latency": {
            "all_adjudicated": _summary(latencies_all),
            "escalated_only": _summary(latencies_escalated),
        },
        "drift": {
            "policy_bundle_digests": digests,
            "policy_digest_drift": len(digests) > 1,
            "policy_digest_unverified_runs": unverified,
            "reviewer_versions": versions,
            "reviewer_version_drift": len(versions) > 1,
            "stale_state_counts": dict(sorted(stale_counts.items())),
            "infrastructure_failures": {
                "failed_runs": sum(failure_modes.values()),
                "failure_modes": dict(sorted(failure_modes.items())),
                "api_budget_exceeded_runs": sum(
                    1 for r in all_real_runs if BUDGET_REASON in r["blocking_reasons"]),
            },
        },
        "anomalies": _sorted_unique(anomalies),
        "evidence": {
            "ledger": ledger_evidence,
            "synthetic": synthetic_evidence,
            "ledger_inputs": ledger["inputs"],
        },
        "gate": {
            "threshold": threshold,
            "criterion": "real_pr_count >= threshold",
            "met": real_pr_count >= threshold,
        },
    }


def file_evidence(path: Path) -> dict[str, str]:
    return {"name": path.name, "sha256": _sha256_bytes(path.read_bytes())}
