"""Founder adjudication from PR labels.

Labels faraday/founder-{green,yellow,red,block} write founder_classification
for that head SHA. Matching is on the founder's immutable account node_id —
captured during discovery — NEVER on login. Non-founder labels are ignored
and logged as adjudication_rejected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LABEL_PREFIX = "faraday/founder-"
VALID_CLASSIFICATIONS = ("green", "yellow", "red", "block")


def classification_from_label(label_name: str) -> str | None:
    if not label_name.startswith(LABEL_PREFIX):
        return None
    suffix = label_name.removeprefix(LABEL_PREFIX)
    return suffix if suffix in VALID_CLASSIFICATIONS else None


def process_label_event(
    *,
    label_name: str,
    actor_node_id: str,
    actor_login: str,
    founder_node_id: str,
    risk_class: str | None,
    labeled_at: str,
) -> dict[str, Any]:
    """Return an adjudication record, or a rejection record.

    The caller merges an accepted record into the run's telemetry event for
    the matching head SHA and appends rejections to the log as-is.
    """
    classification = classification_from_label(label_name)
    if classification is None:
        return {"adjudication_rejected": True, "reject_reason": "unrecognized_label",
                "label": label_name, "actor_login": actor_login}
    if actor_node_id != founder_node_id:
        return {"adjudication_rejected": True, "reject_reason": "actor_not_founder",
                "label": label_name, "actor_login": actor_login,
                "actor_node_id": actor_node_id}
    record: dict[str, Any] = {
        "founder_classification": classification,
        "founder_classification_at": labeled_at,
        "adjudicated_by": {"node_id": actor_node_id, "login": actor_login},
    }
    if risk_class is not None:
        record["agreement"] = risk_class == classification
    return record


ADJUDICATIONS_FILE = "adjudications.jsonl"
ADJUDICATION_RECORD_TYPE = "founder_adjudication"


def build_adjudication_record(
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    label_name: str,
    actor_node_id: str,
    actor_login: str,
    founder_node_id: str,
    labeled_at: str,
) -> dict[str, Any]:
    """Durable adjudications.jsonl record for one label event.

    Wraps process_label_event (the single matching rule: node_id, never
    login). Accepted and rejected events are both recorded with the actor's
    identity and timestamp; only accepted records are ever applied by the
    aggregator. No classifier output is read or copied here — agreement is
    computed later, in the readiness report, from the ledger.
    """
    outcome = process_label_event(
        label_name=label_name,
        actor_node_id=actor_node_id,
        actor_login=actor_login,
        founder_node_id=founder_node_id,
        risk_class=None,
        labeled_at=labeled_at,
    )
    record: dict[str, Any] = {
        "record_type": ADJUDICATION_RECORD_TYPE,
        "repository": repository,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "label": label_name,
        "labeled_at": labeled_at,
        "actor": {"node_id": actor_node_id, "login": actor_login},
        "founder_node_id": founder_node_id,
    }
    if outcome.get("adjudication_rejected"):
        record["status"] = "rejected"
        record["reject_reason"] = outcome["reject_reason"]
    else:
        record["status"] = "accepted"
        record["founder_classification"] = outcome["founder_classification"]
    return record


def append_adjudication(record: dict[str, Any], root: Path) -> Path:
    """Append one record to <root>/adjudications.jsonl (append-only)."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / ADJUDICATIONS_FILE
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path
