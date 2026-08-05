"""Founder adjudication from PR labels.

Labels faraday/founder-{green,yellow,red,block} write founder_classification
for that head SHA. Matching is on the founder's immutable account node_id —
captured during discovery — NEVER on login. Non-founder labels are ignored
and logged as adjudication_rejected.
"""

from __future__ import annotations

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
