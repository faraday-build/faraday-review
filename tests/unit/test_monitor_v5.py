"""QA-1 Q4: the monitor shows the proposed v5 gate next to v4 without
changing v4, and notifies only when the founder queue gains an item."""

from __future__ import annotations

import json

from faraday_review.cli import main
from faraday_review.telemetry.monitor import notification, parse_marker
from test_monitor_report import REPO, TS, Archive, _adjudicated, _monitor

POLICY = {"name": "adr-0014-reference-path-tiers", "sha256": "0" * 64,
          "status": "proposed"}


def _reference(built: dict, tier: str = "A3") -> dict:
    prs = []
    for c in built["report"]["counted_prs"]:
        head = c["head_sha"]
        prs.append({
            "repository": c["repository"], "pr_number": c["pr_number"], "head_sha": head,
            "first_head_sha": head, "heads": [{"head_sha": head, "first_created_at": "2026-11-16T10:00:00Z",
                                               "first_suite_at": None}],
            "live": {"state": "open", "merged": False, "draft": False, "head_sha": head, "base_ref": "main",
                     "merged_at": None, "merge_commit_sha": None},
            "exact_head": True,
            "path_tier": {"status": "complete", "tier": tier, "floor": "A0", "paths": ["x"],
                          "path_tier_counts": {}},
            "codex": {"status": "clean", "receipt_ids": [1], "findings": [], "first_head_receipt_id": 1},
            "required_checks": {"status": "success", "contexts": []},
            "outcome": {"status": "not_merged", "window_days": 14, "reverts": []},
            "founder_label_events": [],
        })
    return {"format": "faraday-review/qualification-reference/v1", "collected_at": TS,
            "reference_policy": POLICY, "outcome_window_days": 14, "prs": prs}


def _v5(arc: Archive, built: dict, tier: str = "A3") -> str:
    ref = arc.tmp / "reference.json"
    ref.write_text(json.dumps(_reference(built, tier)))
    out = arc.tmp / "out" / "qualification-v5.json"
    assert main(["qualification-v5", "--readiness", str(built["readiness"]), "--ledger", str(built["ledger"]),
                 "--reference", str(ref), "--generated-at", TS, "--output", str(out)]) == 0
    return str(out)


def _world(tmp_path, n=5):
    arc = Archive(tmp_path)
    for pr in range(1, n + 1):
        arc.run(100 + pr, pr)
    return arc, arc.build()


def test_without_v5_the_status_is_unchanged(tmp_path):
    arc, built = _world(tmp_path)
    rc, status, md = _monitor(arc, built)
    assert rc == 0 and "qualification_v5" not in status and "founder_queue" not in status["marker"]
    assert "QA-1" not in md


def test_v5_section_is_marked_not_authoritative_and_v4_is_untouched(tmp_path):
    arc, built = _world(tmp_path)
    _, base, _ = _monitor(arc, built)
    v5 = _v5(arc, built, tier="A0")
    rc, status, md = _monitor(arc, built, extra=("--qualification-v5", v5))
    assert rc == 0
    s = status["qualification_v5"]
    assert s["available"] and s["status"] == "proposed" and s["authoritative"] is False
    for key in ("qualification", "gate", "escalation", "checkpoint", "pending_adjudications"):
        assert status[key] == base[key]
    assert "QA-1 proposed gate (v5): NOT authoritative" in md
    assert "gate of record is the v4 gate" in md
    assert status["inputs"]["qualification_v5"]["sha256"]


def test_founder_queue_is_listed_with_exact_head_action_and_raises_attention(tmp_path):
    arc, built = _world(tmp_path)
    classes = {c["classifier_class"] for c in built["report"]["counted_prs"]}
    v5 = _v5(arc, built, tier="A3" if "green" in classes else "A0")
    rc, status, md = _monitor(arc, built, extra=("--qualification-v5", v5))
    queue = status["qualification_v5"]["founder_queue"]
    assert queue, "a sample or an exception must be queued"
    assert status["status"] == "ATTENTION"
    q = queue[0]
    assert f"#{q['pr_number']} head `{q['head_sha'][:12]}`" in md
    assert "Apply exactly one `faraday/founder-<green|yellow|red|block>` label" in md
    assert status["marker"]["founder_queue"]


def test_unavailable_v5_is_reported_and_v4_unaffected(tmp_path):
    arc, built = _world(tmp_path)
    _, base, _ = _monitor(arc, built)
    rc, status, md = _monitor(arc, built, extra=("--qualification-v5-unavailable", "reference_step_failed"))
    assert rc == 0 and status["qualification_v5"] == {"available": False, "reason": "reference_step_failed"}
    assert status["status"] == base["status"] and status["qualification"] == base["qualification"]
    assert "Unavailable this run (reference_step_failed)" in md


def _tampered(tmp_path, mutate):
    arc, built = _world(tmp_path)
    _, base, _ = _monitor(arc, built)
    v5 = _v5(arc, built)
    doc = json.loads(open(v5).read())
    mutate(doc)
    open(v5, "w").write(json.dumps(doc))
    rc, status, md = _monitor(arc, built, extra=("--qualification-v5", v5))
    return rc, status, md, base


def test_v5_from_another_ledger_is_dropped_and_v4_continues(tmp_path):
    rc, status, md, base = _tampered(tmp_path, lambda d: d["evidence"].update(ledger="f" * 64))
    assert rc == 0 and status["qualification_v5"] == {"available": False, "reason": "v5_rejected"}
    assert status["qualification"] == base["qualification"] and "qualification_v5" not in status["inputs"]


def test_v5_naming_a_foreign_repository_is_dropped_never_rendered(tmp_path):
    def foreign(d):
        d["prs"][0]["repository"] = "someone-else/hidden-repo"
    rc, status, md, _ = _tampered(tmp_path, foreign)
    assert rc == 0 and status["qualification_v5"]["reason"] == "v5_rejected"
    assert "hidden-repo" not in md and "hidden-repo" not in json.dumps(status)


def test_schema_invalid_v5_is_dropped(tmp_path):
    rc, status, _, _ = _tampered(tmp_path, lambda d: d.update(authoritative=True))
    assert rc == 0 and status["qualification_v5"]["reason"] == "v5_rejected"


def test_v5_report_and_unavailable_reason_are_exclusive(tmp_path):
    arc, built = _world(tmp_path)
    v5 = _v5(arc, built)
    rc, _, _ = _monitor(arc, built, extra=("--qualification-v5", v5, "--qualification-v5-unavailable", "x"))
    assert rc == 2


def test_bad_unavailable_reason_is_refused(tmp_path):
    arc, built = _world(tmp_path)
    rc, _, _ = _monitor(arc, built, extra=("--qualification-v5-unavailable", "Bad Reason!"))
    assert rc == 2


def _marker(queue, **kw):
    m = {"v": 1, "repository": REPO, "checkpoint": 0, "status": "ATTENTION", "anomaly_counts": {},
         "critical_digest": None, "founder_queue": queue}
    m.update(kw)
    return m


def test_notify_only_on_new_queue_items():
    assert notification(_marker(["1:aaaa:sample"]), _marker([])) == ["founder_queue"]
    assert notification(_marker(["1:aaaa:sample"]), _marker(["1:aaaa:sample"])) == []
    assert notification(_marker([]), _marker(["1:aaaa:sample"])) == []
    # A previous marker from before Q4 has no queue field.
    prev = _marker([])
    prev.pop("founder_queue")
    assert notification(_marker(["2:bbbb:X1"]), prev) == ["founder_queue"]


def test_parse_marker_accepts_old_markers_and_rejects_bad_queue():
    good = _marker(["1:a:sample"])
    old = dict(good)
    old.pop("founder_queue")
    body = lambda m: "x\n<!-- faraday-review-qualification-monitor: " + json.dumps(m) + " -->\n"
    assert parse_marker(body(good), REPO) == good
    assert parse_marker(body(old), REPO) == old
    assert parse_marker(body(_marker("1:a")), REPO) is None
    assert parse_marker(body(_marker([1])), REPO) is None


def test_existing_adjudicated_flow_still_ok_with_v5(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 5)
    built = arc.build()
    v5 = _v5(arc, built, tier="A0")
    rc, status, _ = _monitor(arc, built, extra=("--qualification-v5", v5))
    assert rc == 0 and status["qualification"]["adjudicated_qualifying"] == 5


def test_founder_queue_alone_raises_attention_on_an_ok_v4_status(tmp_path):
    arc = Archive(tmp_path)
    _adjudicated(arc, 5)
    built = arc.build()
    _, base, _ = _monitor(arc, built)
    assert base["status"] == "OK"
    classes = {c["classifier_class"] for c in built["report"]["counted_prs"]}
    v5 = _v5(arc, built, tier="A3" if "green" in classes else "A0")
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5", v5))
    assert status["qualification_v5"]["founder_queue"] and status["status"] == "ATTENTION"



def test_malformed_nested_v5_conditions_are_dropped_not_fatal(tmp_path):
    rc, status, _, base = _tampered(tmp_path, lambda d: d["gate"].update(conditions={}))
    assert rc == 0 and status["qualification_v5"] == {"available": False, "reason": "v5_rejected"}
    assert status["qualification"] == base["qualification"]


def test_queue_items_beyond_the_marker_cap_still_notify():
    from faraday_review.telemetry.monitor import MAX_QUEUE_MARKER
    visible = [f"{n}:aaaa:sample" for n in range(MAX_QUEUE_MARKER)]
    prev = _marker(visible, founder_queue_total=MAX_QUEUE_MARKER + 1, founder_queue_digest="d1")
    cur = _marker(visible, founder_queue_total=MAX_QUEUE_MARKER + 2, founder_queue_digest="d2")
    assert notification(cur, prev) == ["founder_queue"]
    assert notification(cur, dict(cur)) == []


def test_marker_carries_total_and_digest(tmp_path):
    arc, built = _world(tmp_path)
    classes = {c["classifier_class"] for c in built["report"]["counted_prs"]}
    v5 = _v5(arc, built, tier="A3" if "green" in classes else "A0")
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5", v5))
    m = status["marker"]
    assert m["founder_queue_total"] == len(m["founder_queue"]) and m["founder_queue_digest"].startswith("sha256:")


def test_large_founder_queue_keeps_the_issue_body_under_the_limit(tmp_path):
    """Codex P2 r2 (#20): 450 queued entries must not push status.md (the
    issue body) past GitHub's 65,536-character limit; status.json keeps all."""
    from faraday_review.telemetry import monitor
    from faraday_review.telemetry.monitor import _queue_items, render_markdown
    MAX_RENDERED_QUEUE = getattr(monitor, "MAX_RENDERED_QUEUE", 50)
    from faraday_review.canonical import canonical_bytes
    import hashlib
    arc, built = _world(tmp_path)
    v5 = _v5(arc, built, tier="A0")
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5", v5))
    queue = [{"pr_number": n, "head_sha": f"{n:040d}", "reasons": ["X1", "X2", "X3", "sample"]}
             for n in range(1, 451)]
    status["qualification_v5"]["founder_queue"] = queue
    items = _queue_items(status["qualification_v5"])
    status["marker"]["founder_queue"] = items[:500]
    status["marker"]["founder_queue_total"] = len(items)
    status["marker"]["founder_queue_digest"] = hashlib.sha256(canonical_bytes(items)).hexdigest()
    md = render_markdown(status)
    assert len(md) < 65536
    assert md.count("Apply exactly one") == MAX_RENDERED_QUEUE
    assert f"… and {450 - MAX_RENDERED_QUEUE} more (full queue in status.json)" in md
    assert "#### Founder queue (450)" in md
