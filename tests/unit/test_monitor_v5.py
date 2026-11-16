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


def test_outstanding_totals_come_from_gate_conditions(tmp_path):
    """Codex P2 (#24): an unanswered Block whose reference is unknown is
    counted in gate.conditions.exceptions_open (not counts.exception_open);
    the status must report the outstanding totals from the conditions."""
    arc, built = _world(tmp_path)
    v5 = _v5(arc, built, tier="A0")
    doc = json.loads(open(v5).read())
    doc["gate"]["conditions"]["exceptions_open"] = doc["counts"]["exception_open"] + 1
    doc["gate"]["conditions"]["sampling"]["pending"] = doc["counts"]["sample_pending"] + 2
    from faraday_review.telemetry.monitor import v5_summary
    s = v5_summary(doc, None)
    assert s["exceptions_open"] == doc["gate"]["conditions"]["exceptions_open"]
    assert s["samples_pending"] == doc["gate"]["conditions"]["sampling"]["pending"]


def test_rerolled_closed_window_is_a_founder_exception():
    """r50d: a closed window whose recorded membership/samples changed since
    the previous run (or vanished) raises `sampling_rerolled`, routed to the
    founder; unchanged windows and newly closed ones do not."""
    from faraday_review.telemetry.monitor import render_comment, rerolled_windows
    prev = _marker([], closed_windows={"0": "a" * 16, "1": "b" * 16})
    same = _marker([], closed_windows={"0": "a" * 16, "1": "b" * 16, "2": "c" * 16})
    assert notification(same, prev) == [] and rerolled_windows(same, prev) == []
    changed = _marker([], closed_windows={"0": "a" * 16, "1": "f" * 16})
    assert notification(changed, prev) == ["sampling_rerolled"]
    vanished = _marker([], closed_windows={"1": "b" * 16})
    assert rerolled_windows(vanished, prev) == ["0"]
    # No v5 this run (no closed_windows field): not a re-roll.
    assert rerolled_windows(_marker([]), prev) == []
    status = {"status": "ATTENTION", "marker": changed, "kill_switch": {"command": "x"},
              "qualification": {}}
    text = render_comment(status, prev, ["sampling_rerolled"])
    assert "re-rolled" in text and "window 1" in text


def test_parse_marker_validates_closed_windows():
    body = lambda m: "x\n<!-- faraday-review-qualification-monitor: " + json.dumps(m) + " -->\n"
    assert parse_marker(body(_marker([], closed_windows={"0": "a" * 16})), REPO) is not None
    assert parse_marker(body(_marker([], closed_windows={"x": "a" * 16})), REPO) is None
    assert parse_marker(body(_marker([], closed_windows={"0": "short"})), REPO) is None
    assert parse_marker(body(_marker([], closed_windows=["0"])), REPO) is None


def test_marker_records_closed_window_digests(tmp_path):
    arc, built = _world(tmp_path, n=12)
    v5 = _v5(arc, built, tier="A0")
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5", v5))
    doc = json.loads(open(v5).read())
    closed = [w for w in doc["windows"] if w["closed"]]
    assert any(not w["closed"] for w in doc["windows"])  # an open window is never recorded
    assert closed and set(status["marker"]["closed_windows"]) == {str(w["index"]) for w in closed}
    assert all(len(v) == 16 for v in status["marker"]["closed_windows"].values())


def test_unavailable_v5_records_no_windows_and_raises_no_false_reroll(tmp_path):
    """Codex P2 (#24): an unavailable v5 run must not write `closed_windows: {}`,
    which would read as every previously recorded window having vanished."""
    arc, built = _world(tmp_path)
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5-unavailable", "reference_step_failed"))
    assert "closed_windows" not in status["marker"]
    prev = _marker([], closed_windows={"0": "a" * 16})
    assert "sampling_rerolled" not in notification(status["marker"], prev)


def _rec(n0=1):
    return {"seed": "c" * 64, "members": [[n, "red", True] for n in range(n0, n0 + 5)], "sampled": [n0]}


def _frozen_marker(records, **kw):
    from faraday_review.telemetry.qualification_v5 import window_digest
    return _marker([], frozen_windows=records, closed_windows={k: window_digest(r) for k, r in records.items()},
                   **kw)


def _body(marker):
    return "x\n<!-- faraday-review-qualification-monitor: " + json.dumps(marker) + " -->\n"


def test_body_carries_the_window_baseline_across_an_unavailable_run(tmp_path):
    from faraday_review.telemetry.monitor import next_body
    arc, built = _world(tmp_path)
    _monitor(arc, built, extra=("--qualification-v5-unavailable", "reference_step_failed"))
    prev = _frozen_marker({"0": _rec(1)})
    body = next_body(status_path=arc.tmp / "status.json", previous_body=_body(prev), repository=REPO)
    got = parse_marker(body, REPO)
    assert got["frozen_windows"] == prev["frozen_windows"] and got["closed_windows"] == prev["closed_windows"]


def test_frozen_windows_are_append_only_and_previous_records_win(tmp_path):
    """A verified previous record always wins for its index (never rewritten);
    only windows that closed since are appended, contiguously."""
    from faraday_review.telemetry.monitor import next_body
    arc, built = _world(tmp_path, n=10)
    v5 = _v5(arc, built, tier="A0")
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5", v5))
    assert set(status["marker"]["frozen_windows"]) == {"0", "1"}
    prev = _frozen_marker({"0": _rec(101)})
    got = parse_marker(next_body(status_path=arc.tmp / "status.json", previous_body=_body(prev),
                                 repository=REPO), REPO)
    assert got["frozen_windows"]["0"] == _rec(101)                       # previous wins
    assert got["frozen_windows"]["1"] == status["marker"]["frozen_windows"]["1"]  # appended
    assert got["closed_windows"]["0"] == prev["closed_windows"]["0"]


def test_tampered_previous_record_is_not_carried_and_is_invalid(tmp_path):
    from faraday_review.telemetry.monitor import frozen_from_marker, next_body
    prev = _frozen_marker({"0": _rec(1)})
    prev["frozen_windows"]["0"]["sampled"] = [2]          # record no longer matches its digest
    assert frozen_from_marker(prev, REPO) == (None, "invalid")
    assert frozen_from_marker(_marker([], closed_windows={"0": "a" * 16}), REPO) == (None, "invalid")
    assert frozen_from_marker(_marker([]), REPO) == (None, "none")
    assert frozen_from_marker(None, REPO) == (None, "none")
    good = _frozen_marker({"0": _rec(1)})
    doc, state = frozen_from_marker(good, REPO)
    assert state == "loaded" and doc["windows"] == good["frozen_windows"]
    arc, built = _world(tmp_path)
    _monitor(arc, built, extra=("--qualification-v5-unavailable", "reference_step_failed"))
    got = parse_marker(next_body(status_path=arc.tmp / "status.json", previous_body=_body(prev), repository=REPO),
                       REPO)
    assert "frozen_windows" not in got


def test_monitor_notify_cli_writes_the_body(tmp_path):
    arc, built = _world(tmp_path)
    _monitor(arc, built, extra=("--qualification-v5-unavailable", "reference_step_failed"))
    prev = tmp_path / "prev.md"
    marker = _frozen_marker({"0": _rec(1)})
    prev.write_text(_body(marker))
    out = tmp_path / "body.md"
    assert main(["monitor-notify", "--status", str(arc.tmp / "status.json"), "--repository", REPO,
                 "--previous-body", str(prev), "--comment-output", str(tmp_path / "c.md"),
                 "--body-output", str(out)]) == 0
    assert parse_marker(out.read_text(), REPO)["frozen_windows"] == marker["frozen_windows"]


def test_unavailable_run_keeps_the_founder_queue_baseline_and_recovery_does_not_renotify(tmp_path):
    """Codex P2 (#24): outstanding work -> v5 unavailable -> v5 recovers with
    the same work must not re-notify the founder."""
    from faraday_review.telemetry.monitor import next_body
    queue = ["1:aaaaaaaaaaaa:sample", "2:bbbbbbbbbbbb:X1"]
    prev_body = "x\n<!-- faraday-review-qualification-monitor: " + json.dumps(
        _marker(queue, founder_queue_total=2, founder_queue_digest="d1")) + " -->\n"
    arc, built = _world(tmp_path)
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5-unavailable", "reference_step_failed"))
    assert "founder_queue" not in status["marker"]
    assert notification(status["marker"], parse_marker(prev_body, REPO)) == []
    carried = parse_marker(next_body(status_path=arc.tmp / "status.json", previous_body=prev_body,
                                     repository=REPO), REPO)
    assert carried["founder_queue"] == queue and carried["founder_queue_digest"] == "d1"
    # Recovery with exactly the same outstanding work: no founder-queue notification.
    recovered = _marker(queue, founder_queue_total=2, founder_queue_digest="d1")
    assert "founder_queue" not in notification(recovered, carried)


def test_monitor_frozen_cli_states(tmp_path):
    out = tmp_path / "frozen.json"
    assert main(["monitor-frozen", "--repository", REPO, "--output", str(out)]) == 0 and not out.exists()
    good = tmp_path / "good.md"
    good.write_text(_body(_frozen_marker({"0": _rec(1)})))
    assert main(["monitor-frozen", "--previous-body", str(good), "--repository", REPO, "--output", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["windows"]["0"] == _rec(1) and doc["repository"] == REPO
    bad = tmp_path / "bad.md"
    m = _frozen_marker({"0": _rec(1)})
    m["frozen_windows"]["0"]["seed"] = "d" * 64
    bad.write_text(_body(m))
    assert main(["monitor-frozen", "--previous-body", str(bad), "--repository", REPO, "--output", str(out)]) == 0
    assert not out.exists()
    garbled = tmp_path / "garbled.md"
    garbled.write_text("<!-- faraday-review-qualification-monitor: {not json -->\n")
    assert main(["monitor-frozen", "--previous-body", str(garbled), "--repository", REPO, "--output", str(out)]) == 0
    assert not out.exists()


def test_monitor_frozen_cli_prints_the_state(tmp_path, capsys):
    out = tmp_path / "f.json"
    garbled = tmp_path / "g.md"
    garbled.write_text("<!-- faraday-review-qualification-monitor: {not json -->\n")
    main(["monitor-frozen", "--previous-body", str(garbled), "--repository", REPO, "--output", str(out)])
    assert capsys.readouterr().out.strip() == "invalid"
    ok = tmp_path / "ok.md"
    ok.write_text(_body(_frozen_marker({"0": _rec(1)})))
    main(["monitor-frozen", "--previous-body", str(ok), "--repository", REPO, "--output", str(out)])
    assert capsys.readouterr().out.strip() == "loaded"


def test_well_digested_but_malformed_record_is_invalid():
    """A record whose digest matches but which is structurally wrong (4
    members) is still rejected: the digest alone never makes a record usable."""
    from faraday_review.telemetry.monitor import frozen_from_marker
    bad = _rec(1)
    bad["members"].pop()
    assert frozen_from_marker(_frozen_marker({"0": bad}), REPO) == (None, "invalid")


def test_marker_keeps_the_oldest_windows_when_capped(tmp_path, monkeypatch):
    """Frozen records must stay contiguous from window 0, so the cap keeps the
    OLDEST closed windows."""
    from faraday_review.telemetry import monitor as mon
    monkeypatch.setattr(mon, "MAX_WINDOW_MARKER", 1)
    arc, built = _world(tmp_path, n=10)
    v5 = _v5(arc, built, tier="A0")
    _, status, _ = _monitor(arc, built, extra=("--qualification-v5", v5))
    assert set(status["marker"]["frozen_windows"]) == {"0"} and set(status["marker"]["closed_windows"]) == {"0"}
