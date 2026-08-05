from faraday_review.telemetry.adjudication import process_label_event
from faraday_review.telemetry.throughput import compute_throughput

FOUNDER = "U_kgDODk3msA"


def test_founder_label_writes_classification_and_agreement():
    record = process_label_event(
        label_name="faraday/founder-red",
        actor_node_id=FOUNDER,
        actor_login="tomrivera-PSE",
        founder_node_id=FOUNDER,
        risk_class="red",
        labeled_at="2026-08-04T21:00:00Z",
    )
    assert record["founder_classification"] == "red"
    assert record["agreement"] is True
    assert record["adjudicated_by"]["node_id"] == FOUNDER


def test_disagreement_computed():
    record = process_label_event(
        label_name="faraday/founder-green",
        actor_node_id=FOUNDER,
        actor_login="tomrivera-PSE",
        founder_node_id=FOUNDER,
        risk_class="yellow",
        labeled_at="2026-08-04T21:00:00Z",
    )
    assert record["agreement"] is False


def test_non_founder_label_rejected_by_node_id_not_login():
    record = process_label_event(
        label_name="faraday/founder-green",
        actor_node_id="BOT_kgDOEeALbA",
        actor_login="tomrivera-PSE",  # spoofed login must not matter
        founder_node_id=FOUNDER,
        risk_class="green",
        labeled_at="2026-08-04T21:00:00Z",
    )
    assert record["adjudication_rejected"] is True
    assert record["reject_reason"] == "actor_not_founder"


def test_unrecognized_label_rejected():
    record = process_label_event(
        label_name="faraday/founder-purple",
        actor_node_id=FOUNDER,
        actor_login="tomrivera-PSE",
        founder_node_id=FOUNDER,
        risk_class=None,
        labeled_at="2026-08-04T21:00:00Z",
    )
    assert record["adjudication_rejected"] is True


def _event(**overrides):
    base = {
        "repository": "o/r",
        "pr_number": 1,
        "risk_class": "yellow",
        "founder_review_required": True,
        "blocking_reasons": [],
        "created_at": "2026-08-04T10:00:00Z",
    }
    base.update(overrides)
    return base


def test_throughput_derivable_from_telemetry_alone():
    events = [
        _event(pr_number=1, founder_classification="yellow",
               founder_classification_at="2026-08-04T10:10:00Z", agreement=True),
        _event(pr_number=2, founder_classification="green",
               founder_classification_at="2026-08-04T12:00:00Z", agreement=False),
        _event(pr_number=3),
        _event(pr_number=4, risk_class="green", founder_review_required=False),
        _event(pr_number=5, risk_class="block",
               blocking_reasons=["github_api_budget_exceeded"]),
        _event(pr_number=6, risk_class="block",
               blocking_reasons=["pr_too_large_for_deterministic_classification"]),
    ]
    metrics = compute_throughput(events)
    assert metrics["founder_reviews_requested"] == 5
    assert metrics["founder_reviews_completed"] == 2
    assert metrics["median_seconds_to_adjudication"] == 600.0
    assert metrics["risk_class_distribution"]["block"] == 2
    assert metrics["pr_too_large_escalations"] == 1
    assert metrics["api_budget_exceeded_escalations"] == 1
    assert metrics["false_positive_candidates"] == ["o/r#2"]
    assert metrics["agreement_rate"] == 0.5
