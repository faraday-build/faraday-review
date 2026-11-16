"""QA-1 Q3: the proposed v5 gate (pure)."""

from __future__ import annotations

import copy
import json

import pytest

from faraday_review import cli
from faraday_review.telemetry.qualification import QualificationError, local_validation_errors
from faraday_review.telemetry.qualification_v5 import qualification_v5

REPO = "tomrivera-ops/ecosystem-audit"
FOUNDER = "U_kgDODk3msA"
TS = "2026-11-20T00:00:00Z"
DIGEST = "sha256:" + "d" * 64
POLICY = {"name": "adr-0014-reference-path-tiers", "sha256": "0" * 64, "status": "proposed"}


def h(n: int, v: int = 0) -> str:
    return f"{n:04d}{v:02d}".ljust(40, "a")


class World:
    def __init__(self):
        self.prs: list[dict] = []

    def add(self, n, cls="red", tier="A3", codex="clean", checks="success", outcome="not_merged",
            escalated=None, status="unadjudicated", heads=None, events=(), exact=True):
        heads = heads or [(h(n), f"2026-11-15T{n % 24:02d}:00:00Z")]
        head = heads[-1][0]
        self.prs.append(dict(n=n, cls=cls, tier=tier, codex=codex, checks=checks, outcome=outcome,
                             escalated=(cls != "green") if escalated is None else escalated,
                             status=status, heads=heads, head=head, events=list(events), exact=exact))
        return self

    def readiness(self):
        return {"gate": {"released_policy_digests": [DIGEST]},
                "counted_prs": [{"repository": REPO, "pr_number": p["n"], "head_sha": p["head"],
                                 "classifier_class": p["cls"], "qualification": {"status": p["status"]}}
                                for p in self.prs]}

    def ledger(self):
        return {"entries": [{"repository": REPO, "pr_number": p["n"], "head_sha": p["head"], "current": True,
                             "runs": [{"occurrence_log": [{
                                 "eligible": True, "final_status": "completed", "stale_state": "fresh",
                                 "classifier_class": p["cls"], "created_at": p["heads"][-1][1],
                                 "event_key": "k", "escalated": p["escalated"], "blocking_reasons": []}],
                                 "policy_digest_verified": True, "policy_bundle_digest": DIGEST}]}
                            for p in self.prs]}

    def reference(self):
        prs = []
        for p in self.prs:
            prs.append({
                "repository": REPO, "pr_number": p["n"], "head_sha": p["head"],
                "first_head_sha": p["heads"][0][0],
                "heads": [{"head_sha": s, "first_created_at": t, "first_suite_at": None} for s, t in p["heads"]],
                "live": {"state": "open", "merged": False, "draft": False, "head_sha": p["head"],
                         "base_ref": "main", "merged_at": None, "merge_commit_sha": None},
                "exact_head": p["exact"],
                "path_tier": {"status": "complete" if p["tier"] != "unknown" else "incomplete",
                              "tier": p["tier"], "floor": "A0", "paths": ["x"], "path_tier_counts": {}},
                "codex": {"status": p["codex"], "receipt_ids": [p["n"]], "findings": [],
                          "first_head_receipt_id": 1000 + p["n"]},
                "required_checks": {"status": p["checks"], "contexts": []},
                "outcome": {"status": p["outcome"], "window_days": 14, "reverts": []},
                "founder_label_events": [
                    {"event": e[0], "label": f"faraday/founder-{e[1]}", "actor_node_id": e[3] if len(e) > 3 else FOUNDER,
                     "created_at": e[2], "id": i} for i, e in enumerate(p["events"])],
            })
        return {"format": "faraday-review/qualification-reference/v1", "collected_at": TS,
                "reference_policy": POLICY, "outcome_window_days": 14, "prs": prs}

    def run(self, **kw):
        return qualification_v5(self.readiness(), self.ledger(), self.reference(), generated_at=TS,
                                founder_node_id=FOUNDER, evidence={}, **kw)


def pr(report, n):
    return next(p for p in report["prs"] if p["pr_number"] == n)


def full(world, start=1, count=5, **kw):
    for n in range(start, start + count):
        world.add(n, **kw)
    return world


def answer_queue(world, report, cls_for=lambda n: None):
    """Founder answers every queued PR at its head (default: agrees with Faraday)."""
    for q in report["founder_queue"]:
        p = next(x for x in world.prs if x["n"] == q["pr_number"])
        cls = cls_for(p["n"]) or p["cls"]
        p["events"].append(("labeled", cls, "2026-11-19T00:00:00Z"))


def test_report_is_schema_valid_proposed_and_non_authoritative():
    r = full(World()).run()
    assert local_validation_errors("qualification-v5", r) == []
    assert (r["status"], r["authoritative"], r["adopted"]) == ("proposed", False, False)


def test_open_window_does_not_count():
    r = World().add(1).add(2).run()
    assert {p["verification"] for p in r["prs"]} == {"window_open"}
    assert r["counts"]["verified"] == 0


def test_closed_red_window_samples_one_and_counts_after_answer():
    w = full(World())
    r = w.run()
    (win,) = r["windows"]
    assert win["closed"] and len(win["sampled"]) == 1  # no green: one representative
    assert r["counts"]["sample_pending"] == 1 and r["counts"]["verified"] == 4
    answer_queue(w, r)
    r2 = w.run()
    assert r2["counts"]["verified"] == 5 and r2["gate"]["conditions"]["sampling"]["status"] == "pass"
    assert r2["gate"]["conditions"]["escalation"] == {"status": "pass", "mandatory_total": 5, "missed": 0,
                                                      "unknown": 0}


def test_window_with_green_samples_green_plus_one():
    w = World()
    for n in range(1, 6):
        w.add(n, cls="green" if n in (2, 4) else "red", tier="A0" if n in (2, 4) else "A3")
    win = w.run()["windows"][0]
    sampled = {s["pr_number"] for s in win["sampled"]}
    assert len(sampled) == 2 and sampled & {2, 4}


@pytest.mark.parametrize("field,value,reason", [
    ("tier", "unknown", "path_tier_unknown"), ("codex", "absent", "codex_receipt_absent"),
    ("checks", "unknown", "required_checks_unknown"), ("exact", False, "head_mismatch")])
def test_unknown_reference_never_counts(field, value, reason):
    w = full(World(), count=6)
    w.prs[0][field] = value
    r = w.run()
    assert pr(r, 1)["verification"] == "reference_unknown" and pr(r, 1)["reference"]["reason"] == reason
    assert r["counts"]["reference_unknown"] == 1


def test_missing_reference_is_unknown():
    w = full(World())
    ref = w.reference()
    ref["prs"] = ref["prs"][1:]
    r = qualification_v5(w.readiness(), w.ledger(), ref, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert pr(r, 1)["reference"]["reason"] == "reference_missing"


@pytest.mark.parametrize("kw,exc", [
    (dict(cls="green", tier="A2"), ["X1"]), (dict(cls="green", tier="A3"), ["X1"]),
    (dict(cls="green", tier="A0", checks="not_success"), ["X1"]),
    (dict(cls="green", tier="A1", codex="findings"), ["X2"]),
    (dict(cls="green", tier="A0", outcome="adverse"), ["X3"]),
    (dict(cls="yellow", tier="A3"), ["X1"]), (dict(cls="yellow", tier="A2"), []),
    (dict(cls="yellow", tier="A2", codex="findings"), ["X2"]),
    (dict(cls="red", tier="A0"), []), (dict(cls="red", tier="A3", codex="findings"), []),
    (dict(cls="block", tier="A0"), ["X4"]), (dict(cls="green", tier="A1"), []),
])
def test_exceptions(kw, exc):
    r = World().add(1, **kw).run()
    assert pr(r, 1)["exceptions"] == exc
    if exc:
        assert pr(r, 1)["verification"] == "exception_open"
        assert r["founder_queue"][0]["reasons"][:len(exc)] == exc


def test_founder_resolves_exception_in_agreement():
    w = full(World(), start=2, count=4).add(1, cls="green", tier="A2", escalated=False)
    w.prs[-1]["events"] = [("labeled", "green", "2026-11-19T00:00:00Z")]
    r = w.run()
    assert pr(r, 1)["verification"] in ("verified", "sample_pending")
    assert r["confirmed_material_disagreements"] == []


def test_founder_confirms_under_escalation_fails_gate():
    w = full(World(), start=2, count=4).add(1, cls="green", tier="A2", escalated=False)
    w.prs[-1]["events"] = [("labeled", "red", "2026-11-19T00:00:00Z")]
    r = w.run()
    assert r["confirmed_material_disagreements"] == [
        {"repository": REPO, "pr_number": 1, "faraday_class": "green", "founder_class": "red"}]
    assert r["gate"]["met"] is False


def test_sampled_material_disagreement_fails_sampling_and_counts_missed():
    w = World()
    for n in range(1, 6):
        w.add(n, cls="green", tier="A0", escalated=False)
    r = w.run()
    answer_queue(w, r, cls_for=lambda n: "yellow")
    r2 = w.run()
    s = r2["gate"]["conditions"]
    assert s["sampling"]["status"] == "fail" and s["sampling"]["sampled_material_disagreements"] >= 1
    assert s["escalation"]["status"] == "fail" and s["escalation"]["missed"] >= 1


@pytest.mark.parametrize("events,status,cls", [
    ([("labeled", "red", "2026-11-19T00:00:00Z", "U_other")], "none", None),
    ([("labeled", "red", "2026-11-19T00:00:00Z")], "answered", "red"),
    ([("labeled", "red", "2026-11-19T00:00:00Z"), ("unlabeled", "red", "2026-11-19T01:00:00Z")], "none", None),
    ([("labeled", "red", "2026-11-19T00:00:00Z"), ("labeled", "green", "2026-11-19T01:00:00Z")],
     "ambiguous", None),
    ([("labeled", "red", "2026-11-14T00:00:00Z")], "none", None),  # before this head existed
    ([("labeled", "blue", "2026-11-19T00:00:00Z")], "none", None),
])
def test_founder_answer_rules(events, status, cls):
    r = World().add(1, events=events).run()
    ans = pr(r, 1)["founder_answer"]
    assert (ans["status"], ans["class"]) == (status, cls)


def test_label_on_an_earlier_head_is_ignored_after_a_push():
    heads = [(h(1, 0), "2026-11-15T01:00:00Z"), (h(1, 1), "2026-11-16T01:00:00Z")]
    r = World().add(1, heads=heads, events=[("labeled", "red", "2026-11-15T12:00:00Z")]).run()
    assert pr(r, 1)["founder_answer"]["status"] == "none"
    r = World().add(1, heads=heads, events=[("labeled", "red", "2026-11-16T12:00:00Z")]).run()
    assert pr(r, 1)["founder_answer"]["class"] == "red"


def test_sampling_is_reproducible_and_not_rerolled_by_a_later_push_or_rerun():
    w = World()
    for n in range(1, 6):
        w.add(n, cls="green" if n % 2 else "red", tier="A0" if n % 2 else "A3")
    base = w.run()["windows"]
    # Operator re-run at another time: identical selection.
    again = qualification_v5(w.readiness(), w.ledger(), w.reference(), generated_at="2027-01-01T00:00:00Z",
                             founder_node_id=FOUNDER, evidence={})["windows"]
    assert again == base
    # A later push on a member changes its current head, not its rank.
    w2 = copy.deepcopy(w)
    p = w2.prs[0]
    p["heads"] = p["heads"] + [(h(1, 9), "2026-11-17T00:00:00Z")]
    p["head"] = h(1, 9)
    assert [win["sampled"] for win in w2.run()["windows"]] == [win["sampled"] for win in base]


def test_input_order_does_not_change_output():
    w = World()
    for n in range(1, 8):
        w.add(n)
    ref = w.reference()
    ref_rev = copy.deepcopy(ref)
    ref_rev["prs"].reverse()
    a = qualification_v5(w.readiness(), w.ledger(), ref, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    b = qualification_v5(w.readiness(), w.ledger(), ref_rev, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert a == b


def test_unknown_escalation_record_keeps_condition_unknown():
    w = full(World())
    w.prs[1]["escalated"] = "n/a"
    r = w.run()
    answer_queue(w, r)
    assert w.run()["gate"]["conditions"]["escalation"]["status"] == "unknown"


def test_non_qualifying_prs_are_window_members_but_never_counted():
    """r50d: a non-qualifying counted PR anchors window membership only; it is
    never verified, queued or counted toward the gate."""
    r = World().add(1, status="policy_digest_unverified", cls="block").run()
    (p,) = r["prs"]
    assert p["verification"] == "not_qualifying" and p["exceptions"] == []
    assert r["counts"]["qualifying"] == 0 and r["counts"]["not_qualifying"] == 1
    assert r["founder_queue"] == [] and r["counts"]["verified"] == 0
    assert r["gate"]["conditions"]["exceptions_open"] == 0


def _met_world(greens=5):
    w = World()
    for n in range(1, 21):
        g = n <= greens
        w.add(n, cls="green" if g else "red", tier="A0" if g else "A3", escalated=not g)
    r = w.run()
    answer_queue(w, r)
    return w


def test_gate_met_needs_twenty_verified_and_green_coverage():
    r = _met_world().run()
    assert r["counts"]["verified"] == 20 and r["gate"]["met"] is True
    r4 = _met_world(greens=4).run()
    assert r4["gate"]["conditions"]["green_coverage"]["ok"] is False and r4["gate"]["met"] is False


def test_open_exception_blocks_the_gate():
    w = _met_world()
    w.add(21, cls="block", tier="A3")
    assert w.run()["gate"]["met"] is False


def test_threshold_floor():
    with pytest.raises(QualificationError):
        World().run(threshold=19)


def test_cli_writes_validated_report(tmp_path):
    w = _met_world()
    for name, doc in (("readiness", w.readiness()), ("ledger", w.ledger()), ("reference", w.reference())):
        (tmp_path / f"{name}.json").write_text(json.dumps(doc))
    out = tmp_path / "v5.json"
    code = cli.main(["qualification-v5", "--readiness", str(tmp_path / "readiness.json"),
                     "--ledger", str(tmp_path / "ledger.json"), "--reference", str(tmp_path / "reference.json"),
                     "--generated-at", TS, "--output", str(out)])
    doc = json.loads(out.read_text())
    assert code == 0 and doc["gate"]["met"] is True and set(doc["evidence"]) == {"ledger", "readiness", "reference"}


def test_rank_binds_github_assigned_first_head_identity():
    from faraday_review.telemetry.qualification_v5 import _rank
    a = {"repository": REPO, "pr_number": 1, "first_head_sha": h(1)}
    b = dict(a, first_head_sha=h(1, 7))
    assert _rank("s", a) != _rank("s", b)
    assert _rank("s", a) == _rank("s", dict(a, repository=REPO.upper()))
    assert _rank("s", a) != _rank("t", a)


def test_confirmed_disagreement_alone_blocks_an_otherwise_met_gate():
    w = _met_world(greens=6)
    unsampled = next(x["pr_number"] for x in w.run()["prs"] if x["faraday_class"] == "green" and not x["sampled"])
    p = next(x for x in w.prs if x["n"] == unsampled)  # green, escalated anyway; reference tier A2 (X1)
    p.update(tier="A2", escalated=True)
    p["events"] = [("labeled", "red", "2026-11-19T00:00:00Z")]
    r = w.run()
    c = r["gate"]["conditions"]
    assert c["verified_count"]["ok"] and c["escalation"]["status"] == "pass"
    assert c["sampling"]["status"] == "pass" and c["exceptions_open"] == 0 and c["green_coverage"]["ok"]
    assert c["confirmed_material_disagreements"] == 1 and r["gate"]["met"] is False


def test_report_names_only_the_caller_repository():
    from faraday_review.github.collection import public_identity_violations
    r = _met_world().run()
    assert public_identity_violations(r, [{"repository": REPO, "visibility": "public"}]) == 0


def test_escalation_is_taken_from_a_qualifying_occurrence_only():
    w = World().add(1, cls="green", tier="A0", escalated=False)
    led = w.ledger()
    run = led["entries"][0]["runs"][0]
    # A newer same-class occurrence under an unreleased digest says escalated:
    # it must not hide the qualifying occurrence's non-escalation.
    led["entries"][0]["runs"].append({**run, "policy_bundle_digest": "sha256:" + "e" * 64,
                                      "occurrence_log": [{**run["occurrence_log"][0], "escalated": True,
                                                          "created_at": "2026-11-18T00:00:00Z"}]})
    r = qualification_v5(w.readiness(), led, w.reference(), generated_at=TS, founder_node_id=FOUNDER,
                         evidence={})
    assert pr(r, 1)["escalated"] is False


def test_voluntary_founder_disagreement_counts_as_confirmed():
    w = _met_world(greens=6)
    target = next(x["pr_number"] for x in w.run()["prs"]
                  if x["faraday_class"] == "red" and not x["sampled"])
    p = next(x for x in w.prs if x["n"] == target)
    p.update(cls="yellow", tier="A2")
    p["events"] = [("labeled", "red", "2026-11-19T00:00:00Z")]
    r = w.run()
    assert {"pr_number": target, "founder_class": "red"}.items() <= r["confirmed_material_disagreements"][0].items()
    assert r["gate"]["met"] is False


def test_label_after_push_but_before_shadow_run_binds_to_the_new_head():
    w = World().add(1, events=[("labeled", "red", "2026-11-15T00:30:00Z")],
                    heads=[(h(1), "2026-11-15T01:00:00Z")])
    ref = w.reference()
    ref["prs"][0]["heads"][0]["first_suite_at"] = "2026-11-15T00:20:00Z"
    r = qualification_v5(w.readiness(), w.ledger(), ref, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert pr(r, 1)["founder_answer"]["class"] == "red"
    # Without the push evidence the same label predates the head's run: ignored.
    assert pr(w.run(), 1)["founder_answer"]["status"] == "none"


def test_class_and_escalation_come_from_the_latest_qualifying_occurrence():
    """Codex P2 r2 (#19): older qualifying green, later qualifying red (no
    escalation recorded as True), newest green under an unreleased digest.
    Readiness's representative says green; v5 must score the qualifying red
    occurrence, with its own escalation value."""
    w = World().add(1, cls="green", tier="A0")
    led = w.ledger()
    run = led["entries"][0]["runs"][0]
    base = run["occurrence_log"][0]
    run["occurrence_log"] = [
        dict(base, classifier_class="green", created_at="2026-11-15T01:00:00Z", event_key="a", escalated=False),
        dict(base, classifier_class="red", created_at="2026-11-15T02:00:00Z", event_key="b", escalated=False),
    ]
    led["entries"][0]["runs"].append({
        "occurrence_log": [dict(base, classifier_class="green", created_at="2026-11-15T03:00:00Z",
                                event_key="c", escalated=False)],
        "policy_digest_verified": True, "policy_bundle_digest": "sha256:" + "e" * 64})
    r = qualification_v5(w.readiness(), led, w.reference(), generated_at=TS,
                         founder_node_id=FOUNDER, evidence={})
    assert pr(r, 1)["faraday_class"] == "red"
    assert pr(r, 1)["escalated"] is False


def test_no_qualifying_occurrence_keeps_escalation_unknown():
    w = World().add(1, cls="red")
    led = w.ledger()
    led["entries"][0]["runs"][0]["policy_bundle_digest"] = "sha256:" + "e" * 64
    r = qualification_v5(w.readiness(), led, w.reference(), generated_at=TS,
                         founder_node_id=FOUNDER, evidence={})
    assert pr(r, 1)["escalated"] is None and pr(r, 1)["faraday_class"] == "red"


@pytest.mark.parametrize("field,value", [("codex", "absent"), ("checks", "unknown")])
def test_member_turning_unknown_does_not_reroll_other_windows(field, value):
    """Codex P2 r2 (#19): windows stay anchored to arrival identity; a member
    whose reference becomes unknown stays in its window, ineligible."""
    def world(change):
        w = World()
        for n in range(1, 21):
            kw = {"cls": "green", "tier": "A0"} if n % 4 == 0 else {}
            if change and n == 2:
                kw[field] = value
            w.add(n, **kw)
        return w.run()
    base, changed = world(False), world(True)
    strip = lambda ws: [(x["index"], x["seed"], x["members"], x["sampled"]) for x in ws]
    assert strip(changed["windows"]) == strip(base["windows"])
    assert len(changed["windows"]) == 4
    assert pr(changed, 2)["verification"] == "reference_unknown"


def test_unanswered_sample_with_unknown_reference_stays_outstanding():
    """Codex P2 (#23): a sampled PR whose reference became unknown is not
    verified, but its sample review stays in the founder queue and keeps
    sampling pending, so unknown evidence cannot bypass sampling."""
    base = full(World(), count=5).run()
    (sampled,) = [s["pr_number"] for s in base["windows"][0]["sampled"]][:1] or [None]
    w = World()
    for n in range(1, 6):
        w.add(n, **({"checks": "unknown"} if n == sampled else {}))
    r = w.run()
    assert [s["pr_number"] for s in r["windows"][0]["sampled"]] == [s["pr_number"] for s in base["windows"][0]["sampled"]]
    assert pr(r, sampled)["verification"] == "reference_unknown"
    assert any(q["pr_number"] == sampled and "sample" in q["reasons"] for q in r["founder_queue"])
    assert r["gate"]["conditions"]["sampling"]["status"] == "pending"
    assert r["gate"]["conditions"]["sampling"]["pending"] >= 1


def test_unanswered_block_with_unknown_reference_stays_an_open_exception():
    """Codex P2 (#23): an unsampled Block PR whose reference is unknown keeps
    its X4 exception in the founder queue and blocks the gate."""
    w = World()
    for n in range(1, 26):
        kw = {"cls": "green", "tier": "A0"} if n % 3 == 0 else {}
        w.add(n, **kw)
    base = w.run()
    sampled = {s["pr_number"] for win in base["windows"] for s in win["sampled"]}
    target = next(n for n in range(1, 26) if n not in sampled)
    w2 = World()
    for n in range(1, 26):
        kw = {"cls": "green", "tier": "A0"} if n % 3 == 0 else {}
        if n == target:
            kw = {"cls": "block", "checks": "unknown"}
        w2.add(n, **kw)
    r = w2.run()
    assert pr(r, target)["verification"] == "reference_unknown"
    assert pr(r, target)["exceptions"] == ["X4"]
    assert any(q["pr_number"] == target and "X4" in q["reasons"] for q in r["founder_queue"])
    assert r["gate"]["conditions"]["exceptions_open"] >= 1
    assert r["gate"]["met"] is False


def test_unanswered_block_with_unknown_reference_blocks_an_otherwise_met_gate():
    w = _met_world()
    assert w.run()["gate"]["met"] is True
    w.add(21, cls="block", tier="A3", checks="unknown")
    r = w.run()
    assert pr(r, 21)["verification"] == "reference_unknown"
    assert r["gate"]["conditions"]["exceptions_open"] == 1 and r["gate"]["met"] is False


def _reclassified(w, n, later_cls, later_at="2026-11-19T00:00:00Z"):
    """Ledger where PR n's head gets a later qualifying occurrence of another class."""
    led = w.ledger()
    for e in led["entries"]:
        if e["pr_number"] == n:
            occ = dict(e["runs"][0]["occurrence_log"][0], classifier_class=later_cls,
                       created_at=later_at, event_key="later", escalated=later_cls != "green")
            e["runs"][0]["occurrence_log"].append(occ)
    return led


@pytest.mark.parametrize("target", [1, 2, 3, 4, 5])
def test_sample_selection_survives_a_later_reclassification(target):
    """Codex P2 (#23): samples are selected by each member's earliest
    qualifying class, so a later run reclassifying the sampled green PR
    cannot shrink or change the window's samples."""
    w = World()
    for n in range(1, 6):
        w.add(n, **({"cls": "green", "tier": "A0"} if n == target else {}))
    base = w.run()
    led = _reclassified(w, target, "red")
    readiness = w.readiness()
    for c in readiness["counted_prs"]:
        if c["pr_number"] == target:
            c["classifier_class"] = "red"
    r = qualification_v5(readiness, led, w.reference(), generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert [s["pr_number"] for s in r["windows"][0]["sampled"]] == [s["pr_number"] for s in base["windows"][0]["sampled"]]
    assert len(r["windows"][0]["sampled"]) == 2
    assert pr(r, target)["faraday_class"] == "red"
    assert {q["pr_number"] for q in r["founder_queue"]} >= {s["pr_number"] for s in r["windows"][0]["sampled"]}


def test_incomplete_outcome_is_unknown_reference():
    w = World().add(1, outcome="incomplete")
    r = w.run()
    assert pr(r, 1)["verification"] == "reference_unknown"
    assert pr(r, 1)["reference"]["reason"] == "outcome_incomplete"


def test_selection_class_does_not_change_when_a_digest_is_released_later():
    """r50d: selection uses only facts recorded once per occurrence. Whether
    the earliest occurrence's digest is released can change later; that must
    not change which members of a closed window are sampled."""
    w = World()
    for n in range(1, 6):
        w.add(n)
    led = w.ledger()
    for e in led["entries"]:
        if e["pr_number"] == 3:
            first = dict(e["runs"][0]["occurrence_log"][0], classifier_class="green",
                         created_at="2026-11-14T00:00:00Z", event_key="early", escalated=False)
            e["runs"].insert(0, {"occurrence_log": [first], "policy_digest_verified": True,
                                 "policy_bundle_digest": "sha256:" + "e" * 64})
    before = qualification_v5(w.readiness(), led, w.reference(), generated_at=TS, founder_node_id=FOUNDER,
                              evidence={})["windows"]
    released = w.readiness()
    released["gate"]["released_policy_digests"].append("sha256:" + "e" * 64)
    after = qualification_v5(released, led, w.reference(), generated_at=TS, founder_node_id=FOUNDER,
                             evidence={})["windows"]
    assert [x["sampled"] for x in after] == [x["sampled"] for x in before]
    assert {"repository": REPO, "pr_number": 3} in before[0]["sampled"]




def _ten_red():
    w = World()
    for n in range(1, 11):
        w.add(n)
    return w


def _samples(r):
    return [[s["pr_number"] for s in win["sampled"]] for win in r["windows"]]


def test_later_non_qualifying_head_does_not_shift_windows():
    """Codex P2 4581512015: marking PR 1 non-qualifying must not shift window
    boundaries or drop the other windows' samples."""
    base = _ten_red().run()
    w = _ten_red()
    w.prs[0]["status"] = "head_incomplete"
    r = w.run()
    assert _samples(r) == _samples(base)
    assert [x["members"] for x in r["windows"]] == [x["members"] for x in base["windows"]]
    assert pr(r, 1)["verification"] == "not_qualifying"


def test_disappearing_receipt_does_not_reroll_the_seed():
    """Codex P2 4581512018: PR 1's first-head receipt becoming unrecognized
    (receipt absent, first_head_receipt_id null) must not re-roll its window."""
    base = _ten_red().run()
    w = _ten_red()
    ref = w.reference()
    ref["prs"][0]["codex"].update(status="absent", first_head_receipt_id=None, receipt_ids=[])
    r = qualification_v5(w.readiness(), w.ledger(), ref, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert [x["seed"] for x in r["windows"]] == [x["seed"] for x in base["windows"]]
    assert _samples(r) == _samples(base)


def test_arrival_order_comes_from_the_ledger_not_live_reference_evidence():
    """r50d: window membership follows the ledger's recorded first occurrence,
    so rewritten or missing reference head timestamps cannot reorder windows."""
    base = _ten_red().run()
    w = _ten_red()
    ref = w.reference()
    for p in ref["prs"]:  # reverse the live reference's head timestamps
        p["heads"][0]["first_created_at"] = f"2026-11-15T{23 - p['pr_number']:02d}:00:00Z"
    r = qualification_v5(w.readiness(), w.ledger(), ref, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert [x["members"] for x in r["windows"]] == [x["members"] for x in base["windows"]]


def test_pr_whose_early_runs_failed_arrives_when_it_first_completes():
    """Codex P2 4581695210: PR 1 initially only failed, PRs 2-11 completed and
    their windows closed; when PR 1 later completes it must not be inserted
    ahead of the closed windows."""
    def world(include_pr1):
        w = World()
        for n in range(1, 12):
            if n == 1 and not include_pr1:
                continue
            w.add(n, heads=[(h(n), f"2026-11-15T{n:02d}:00:00Z")])
        return w
    base = world(False).run()
    w = world(True)
    led = w.ledger()
    for e in led["entries"]:
        if e["pr_number"] == 1:  # an early failed occurrence, then a late completion
            occ = e["runs"][0]["occurrence_log"][0]
            e["runs"][0]["occurrence_log"] = [
                dict(occ, final_status="failed", created_at="2026-11-15T01:00:00Z", event_key="f"),
                dict(occ, created_at="2026-11-15T23:00:00Z", event_key="c")]
    r = qualification_v5(w.readiness(), led, w.reference(), generated_at=TS, founder_node_id=FOUNDER, evidence={})
    closed_base = [x for x in base["windows"] if x["closed"]]
    assert [(x["members"], x["sampled"]) for x in r["windows"][:len(closed_base)]] == \
        [(x["members"], x["sampled"]) for x in closed_base]


def test_head_checked_earlier_on_another_branch_does_not_reverse_tenure():
    """Codex P2 4581695212: old head first seen Nov 15; new head pushed Nov 16
    whose commit was checked on another branch Nov 14. A founder label on
    Nov 16 binds to the new head."""
    from faraday_review.telemetry.qualification_v5 import founder_answer
    old, new = h(1), h(1, 2)
    ref = {"heads": [{"head_sha": old, "first_created_at": "2026-11-15T10:00:00Z", "first_suite_at": None},
                     {"head_sha": new, "first_created_at": "2026-11-16T10:00:00Z",
                      "first_suite_at": "2026-11-14T10:00:00Z"}],
           "founder_label_events": [{"event": "labeled", "label": "faraday/founder-red", "actor_node_id": FOUNDER,
                                     "created_at": "2026-11-16T12:00:00Z", "id": 1}]}
    assert founder_answer(ref, new, FOUNDER)["status"] == "answered"
    assert founder_answer(ref, old, FOUNDER)["status"] == "none"
    # A label while the old head was still current (Nov 15 12:00) binds to the old head.
    ref["founder_label_events"][0]["created_at"] = "2026-11-15T12:00:00Z"
    assert founder_answer(ref, old, FOUNDER)["status"] == "answered"
    assert founder_answer(ref, new, FOUNDER)["status"] == "none"


def test_missing_reference_keeps_the_ledger_first_head_in_the_seed():
    """Codex P2 4581743728: PR 1 is on a later head and its reference entry is
    missing; the seed must keep its original first head (from the ledger)."""
    def world():
        w = _ten_red()
        w.prs[0]["heads"] = [(h(1), "2026-11-15T01:00:00Z"), (h(1, 2), "2026-11-15T01:30:00Z")]
        w.prs[0]["head"] = h(1, 2)
        return w
    w = world()
    led = w.ledger()
    for e in led["entries"]:
        if e["pr_number"] == 1:  # the ledger also saw the first head
            led["entries"].append({**e, "head_sha": h(1), "current": False,
                                   "runs": [{**e["runs"][0], "occurrence_log": [dict(
                                       e["runs"][0]["occurrence_log"][0], created_at="2026-11-15T01:00:00Z",
                                       event_key="first")]}]})
            break
    full_ref = w.reference()
    base = qualification_v5(w.readiness(), led, full_ref, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    ref = copy.deepcopy(full_ref)
    ref["prs"] = [p for p in ref["prs"] if p["pr_number"] != 1]
    r = qualification_v5(w.readiness(), led, ref, generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert [x["seed"] for x in r["windows"]] == [x["seed"] for x in base["windows"]]
    assert _samples(r) == _samples(base)


def test_head_that_becomes_current_again_accepts_a_later_label():
    """Codex P2 4581743732: A -> B -> force-push back to A; a founder label
    applied after the return binds to A, and not to B."""
    from faraday_review.telemetry.qualification_v5 import founder_answer
    a, b = h(1), h(1, 2)
    ref = {"heads": [{"head_sha": a, "first_created_at": "2026-11-15T10:00:00Z", "first_suite_at": None},
                     {"head_sha": b, "first_created_at": "2026-11-16T10:00:00Z", "first_suite_at": None}],
           "founder_label_events": [{"event": "labeled", "label": "faraday/founder-red", "actor_node_id": FOUNDER,
                                     "created_at": "2026-11-17T12:00:00Z", "id": 1}]}
    points = [("2026-11-15T10:00:00Z", a), ("2026-11-16T10:00:00Z", b), ("2026-11-17T10:00:00Z", a)]
    assert founder_answer(ref, a, FOUNDER, points)["status"] == "answered"
    assert founder_answer(ref, b, FOUNDER, points)["status"] == "none"
    # Without the return, the label is outside A's tenure.
    assert founder_answer(ref, a, FOUNDER, points[:2])["status"] == "none"


def test_occurrence_timestamps_are_compared_chronologically():
    """Codex P2 4581743734: '2026-11-15T01:00:00+02:00' (23:00Z the day before)
    is OLDER than '2026-11-14T23:30:00Z'; the latter is the latest occurrence."""
    w = World().add(1, cls="red")
    led = w.ledger()
    occ = led["entries"][0]["runs"][0]["occurrence_log"][0]
    led["entries"][0]["runs"][0]["occurrence_log"] = [
        dict(occ, classifier_class="red", created_at="2026-11-15T01:00:00+02:00", event_key="a", escalated=True),
        dict(occ, classifier_class="green", created_at="2026-11-14T23:30:00Z", event_key="b", escalated=False)]
    r = qualification_v5(w.readiness(), led, w.reference(), generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert pr(r, 1)["faraday_class"] == "green" and pr(r, 1)["escalated"] is False


# ---- D3-1: head-specific adjudication-period model ---------------------------------------------

def _ref(heads, events):
    return {"heads": [{"head_sha": s, "first_created_at": t, "first_suite_at": None} for s, t in heads],
            "founder_label_events": [{"event": e[0], "label": f"faraday/founder-{e[1]}",
                                      "actor_node_id": e[3] if len(e) > 3 else FOUNDER, "created_at": e[2],
                                      "id": i + 1} for i, e in enumerate(events)]}


def test_stale_rerun_on_an_old_head_does_not_end_the_current_heads_tenure():
    """Codex P2 4581776085: fresh A, then fresh B, then a stale rerun on A.
    A founder label on the still-current B binds to B."""
    w = World().add(1, heads=[(h(1), "2026-11-15T01:00:00Z"), (h(1, 2), "2026-11-16T01:00:00Z")],
                    events=[("labeled", "red", "2026-11-17T12:00:00Z")])
    led = w.ledger()
    occ = led["entries"][0]["runs"][0]["occurrence_log"][0]
    led["entries"].append({"repository": REPO, "pr_number": 1, "head_sha": h(1), "current": False,
                           "runs": [{"occurrence_log": [
                               dict(occ, created_at="2026-11-15T01:00:00Z", event_key="a1"),
                               dict(occ, created_at="2026-11-17T00:00:00Z", event_key="a-rerun",
                                    stale_state="stale_before_publication")],
                               "policy_digest_verified": True, "policy_bundle_digest": DIGEST}]})
    r = qualification_v5(w.readiness(), led, w.reference(), generated_at=TS, founder_node_id=FOUNDER, evidence={})
    assert pr(r, 1)["founder_answer"] == {"status": "answered", "class": "red", "labeled_at": "2026-11-17T12:00:00Z"}


def test_removal_during_another_heads_tenure_is_replayed():
    """Codex P2 4581776088: A -> B -> A. Red applied during the first A tenure,
    removed during B, green applied after returning to A: the answer for A is
    green (not ambiguous)."""
    from faraday_review.telemetry.qualification_v5 import founder_answer
    a, b = h(1), h(1, 2)
    ref = _ref([(a, "2026-11-15T10:00:00Z"), (b, "2026-11-16T10:00:00Z")],
               [("labeled", "red", "2026-11-15T12:00:00Z"), ("unlabeled", "red", "2026-11-16T12:00:00Z"),
                ("labeled", "green", "2026-11-17T12:00:00Z")])
    points = [("2026-11-15T10:00:00Z", a), ("2026-11-16T10:00:00Z", b), ("2026-11-17T10:00:00Z", a)]
    assert founder_answer(ref, a, FOUNDER, points)["class"] == "green"
    assert founder_answer(ref, a, FOUNDER, points)["status"] == "answered"
    assert founder_answer(ref, b, FOUNDER, points)["status"] == "none"


def test_a_stale_label_left_from_another_head_makes_the_answer_ambiguous():
    """Fail closed: red applied on head A and never removed, green applied on
    the current head B -> two founder labels present -> ambiguous."""
    from faraday_review.telemetry.qualification_v5 import founder_answer
    a, b = h(1), h(1, 2)
    ref = _ref([(a, "2026-11-15T10:00:00Z"), (b, "2026-11-16T10:00:00Z")],
               [("labeled", "red", "2026-11-15T12:00:00Z"), ("labeled", "green", "2026-11-16T12:00:00Z")])
    assert founder_answer(ref, b, FOUNDER)["status"] == "ambiguous"


def test_label_history_is_replayed_chronologically_not_in_input_order():
    """Events arrive out of order and with UTC offsets; the replay uses time
    order: label 23:30Z, then unlabel at 01:00+02:00 (= 23:00Z) is EARLIER ->
    the label is still present."""
    from faraday_review.telemetry.qualification_v5 import founder_answer
    a = h(1)
    ref = _ref([(a, "2026-11-15T10:00:00Z")],
               [("labeled", "red", "2026-11-15T23:30:00Z"), ("unlabeled", "red", "2026-11-16T01:00:00+02:00"),
                ("labeled", "red", "2026-11-15T22:00:00Z")])
    ans = founder_answer(ref, a, FOUNDER)
    assert ans["status"] == "answered" and ans["labeled_at"] == "2026-11-15T23:30:00Z"


def test_answer_needs_a_datable_tenure_for_the_head():
    from faraday_review.telemetry.qualification_v5 import founder_answer
    ref = _ref([], [("labeled", "red", "2026-11-15T12:00:00Z")])
    assert founder_answer(ref, h(1), FOUNDER)["status"] == "none"
