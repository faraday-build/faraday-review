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


def test_non_qualifying_prs_are_ignored():
    r = World().add(1, status="policy_digest_unverified").run()
    assert r["prs"] == [] and r["counts"]["qualifying"] == 0


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
