"""QA-1 qualification gate v5 (GHO-AUTONOMOUS-PRODUCTION-011, Q3).

PROPOSED, NOT AUTHORITATIVE. The founder approved QA-1 in principle on
2026-11-15; it replaces the v4 gate (`readiness`) only after formal founder
adoption and passing tests. Until then this report is computed alongside v4
and never changes it. Pure: no network, no clock, no RNG, canonical output.

Inputs: the v4 readiness report (which PRs qualify), the ledger (Faraday's
scored escalation), and the `reference` document (independent L1-L4
evidence and founder-label events, all re-derived from GitHub).

Per qualifying real PR at its exact head:
- reference unknown (any layer unknown, or a head mismatch): never counts;
- Faraday vs reference concordance; material disagreements become founder
  exceptions X1 (under-escalation by tier or failing checks), X2 (Codex
  P0/P1), X3 (post-merge revert); X4 every Block;
- deterministic sampling: candidates in arrival order, windows of 5; a full
  window samples its lowest-ranked green PR plus the lowest-ranked other.
  Ranks are sha256 over GitHub-assigned identifiers (first head SHA, first
  Codex receipt id): reproducible, never operator-chosen, not re-rolled by
  later pushes. PRs in a window that is not yet full do not count yet;
- a founder answer is the founder's (node_id) single `faraday/founder-*`
  label applied while that exact head was current. Labels carried over
  from an earlier head are ignored.

Automated verdicts are `reference` fields, never founder adjudications. The
report authorizes nothing (HR-1).
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..canonical import canonical_bytes
from .qualification import (
    GATE_THRESHOLD_FLOOR,
    QualificationError,
    FACTS_VERSION,
    _sort_key_ts,
    persisted_local_diagnostics,
    repo_identity,
)

V5_FORMAT = "faraday-review/qualification-v5/v1"
CRITERION = ("verified_qualifying_real_pr_count >= threshold AND escalation pass AND sampling pass "
             "AND exceptions_open == 0 AND confirmed_material_disagreements == 0 "
             "AND verified_green >= green_minimum")
GREEN_MINIMUM = 5
WINDOW = 5
CLASS_ORDER = ("green", "yellow", "red", "block")
# Founder classes that require founder review (governance-released table
# for the released digest: green no; yellow, red and block yes).
FOUNDER_REVIEW_CLASSES = ("yellow", "red", "block")
LABEL_PREFIX = "faraday/founder-"
QUALIFYING = ("adjudicated_qualifying", "unadjudicated")
TIER_ORDER = ("A0", "A1", "A2", "A3", "A4")


def _sha(data: Any) -> str:
    return hashlib.sha256(canonical_bytes(data)).hexdigest()


def _key(repository: str, pr: int) -> tuple[str, int]:
    return (repo_identity(repository), int(pr))


def _intervals(ref: dict[str, Any] | None, head: str,
               transitions: list[list[str]] = ()) -> list[tuple[Any, Any]]:
    """Every [start, end) adjudication period (chronological sort keys) during
    which `head` was the PR head. Head history comes ONLY from the canonical
    v4 facts (`head_transitions`: identity-eligible fresh occurrences); v5
    never re-derives it. The one consumer rule: a reference check-suite time
    may move a head's FIRST start earlier (a label applied before its shadow
    run), but never before the previous transition."""
    changes = [[_sort_key_ts(t), sha] for t, sha in transitions]
    suites = {h["head_sha"]: h.get("first_suite_at") for h in (ref or {}).get("heads", [])}
    seen: set[str] = set()
    for i, (_, sha) in enumerate(changes):
        if sha in seen:
            continue
        seen.add(sha)
        suite = suites.get(sha)
        if suite:
            key = _sort_key_ts(suite)
            if key < changes[i][0] and (i == 0 or key > changes[i - 1][0]):
                changes[i][0] = key
    return [(changes[i][0], changes[i + 1][0] if i + 1 < len(changes) else None)
            for i in range(len(changes)) if changes[i][1] == head]


def founder_answer(ref: dict[str, Any], head: str, founder_node_id: str,
                   transitions: list[list[str]] = ()) -> dict[str, Any]:
    """The founder's classification for `head` (D3-1 head-specific
    adjudication-period model).

    1. Replay EVERY founder `faraday/founder-<class>` label event, applications
       and removals on any head, in chronological order (then event id), to get
       the labels present now with each one's latest application time.
    2. More than one class present: `ambiguous` (fail closed; the founder
       removes the stale label).
    3. Exactly one class present: `answered` only if its latest application
       falls inside an adjudication period (tenure interval) of `head`;
       otherwise `none`. A label applied while another head was current never
       answers for this head.
    Non-founder and unknown-class events never count. A head without any
    datable tenure has no answer."""
    intervals = _intervals(ref, head, transitions)
    events = sorted(ref["founder_label_events"],
                    key=lambda ev: (_sort_key_ts(ev["created_at"]), int(ev.get("id") or 0)))
    present: dict[str, str] = {}
    ignored = 0
    for ev in events:
        if ev["actor_node_id"] != founder_node_id:
            ignored += 1
            continue
        cls = ev["label"].removeprefix(LABEL_PREFIX)
        if cls not in CLASS_ORDER:
            continue
        if ev["event"] == "labeled":
            present[cls] = ev["created_at"]
        elif ev["event"] == "unlabeled":
            present.pop(cls, None)
    out: dict[str, Any] = {"status": "none", "class": None, "labeled_at": None}
    if len(present) > 1:
        out = {"status": "ambiguous", "class": None, "labeled_at": None}
    elif len(present) == 1 and intervals:
        (cls, at), = present.items()
        key = _sort_key_ts(at)
        if any(start <= key and (end is None or key < end) for start, end in intervals):
            out = {"status": "answered", "class": cls, "labeled_at": at}
    if ignored:
        out["non_founder_label_events"] = ignored
    return out


def _reference_view(ref: dict[str, Any] | None, head: str) -> dict[str, Any]:
    if ref is None:
        return {"status": "unknown", "reason": "reference_missing"}
    reasons = []
    if ref["head_sha"] != head or not ref["exact_head"]:
        reasons.append("head_mismatch")
    tier = ref["path_tier"]["tier"]
    if tier not in TIER_ORDER:
        reasons.append("path_tier_unknown")
    if ref["codex"]["status"] == "absent":
        reasons.append("codex_receipt_absent")
    if ref["required_checks"]["status"] == "unknown":
        reasons.append("required_checks_unknown")
    if ref["outcome"]["status"] == "incomplete":
        # The post-merge evidence could not be read completely (#22): never
        # treated as clear.
        reasons.append("outcome_incomplete")
    view = {
        "tier": tier,
        "codex": ref["codex"]["status"],
        "required_checks": ref["required_checks"]["status"],
        "outcome": ref["outcome"]["status"],
    }
    if reasons:
        return {"status": "unknown", "reason": sorted(reasons)[0], "reasons": sorted(reasons), **view}
    tier_review = TIER_ORDER.index(tier) >= TIER_ORDER.index("A2")
    view["review_required"] = bool(tier_review or view["codex"] == "findings"
                                   or view["required_checks"] == "not_success"
                                   or view["outcome"] == "adverse")
    return {"status": "known", **view}


def _exceptions(cls: str, ref: dict[str, Any]) -> list[str]:
    if cls == "block":
        return ["X4"]
    if ref["status"] != "known" or cls not in ("green", "yellow"):
        return []
    found = set()
    tier = TIER_ORDER.index(ref["tier"])
    if (cls == "green" and tier >= TIER_ORDER.index("A2")) or (cls == "yellow" and tier >= TIER_ORDER.index("A3")):
        found.add("X1")
    if ref["required_checks"] == "not_success":
        found.add("X1")
    if ref["codex"] == "findings":
        found.add("X2")
    if ref["outcome"] == "adverse":
        found.add("X3")
    return sorted(found)


FROZEN_FORMAT = "faraday-review/qualification-v5-frozen/v1"


def frozen_record_errors(doc: Any) -> list[str]:
    """Structural validation of a frozen-window record (fail closed on any
    problem): {format, repository, windows: {"<i>": {seed, members:
    [[pr, class, escalated]] x 5, sampled: [pr, ...]}}} with indices
    contiguous from 0, unique members, and samples drawn from members."""
    errs: list[str] = []
    if not isinstance(doc, dict) or doc.get("format") != FROZEN_FORMAT:
        return ["format"]
    if not isinstance(doc.get("repository"), str) or "/" not in doc["repository"]:
        errs.append("repository")
    wins = doc.get("windows")
    if not isinstance(wins, dict):
        return errs + ["windows"]
    if not all(isinstance(k, str) and k.isdigit() for k in wins) or \
            sorted(int(k) for k in wins) != list(range(len(wins))):
        return errs + ["window indices must be contiguous from 0"]
    seen: set[int] = set()
    for k, rec in wins.items():
        if not isinstance(rec, dict) or set(rec) != {"seed", "members", "sampled"}:
            errs.append(f"window {k}: shape"); continue
        if not (isinstance(rec["seed"], str) and len(rec["seed"]) == 64):
            errs.append(f"window {k}: seed")
        ms = rec["members"]
        if not (isinstance(ms, list) and len(ms) == WINDOW and all(
                isinstance(m, list) and len(m) == 3 and isinstance(m[0], int) and not isinstance(m[0], bool)
                and m[1] in CLASS_ORDER and (m[2] is None or isinstance(m[2], bool)) for m in ms)):
            errs.append(f"window {k}: members"); continue
        prs = [m[0] for m in ms]
        if len(set(prs)) != WINDOW or seen & set(prs):
            errs.append(f"window {k}: repeated member")
        seen |= set(prs)
        sm = rec["sampled"]
        if not (isinstance(sm, list) and 1 <= len(sm) <= 2 and len(set(sm)) == len(sm)
                and all(x in prs for x in sm)):
            errs.append(f"window {k}: sampled")
    return errs


def frozen_record(report: dict[str, Any], repository: str) -> dict[str, Any]:
    """The frozen record of every CLOSED window of a v5 report, for the
    status-issue marker. Members keep the class and escalation they had at
    closure."""
    key = repo_identity(repository)
    out = {}
    for w in report["windows"]:
        if not w["closed"]:
            continue
        out[str(w["index"])] = {
            "seed": w["seed"],
            "members": [[m["pr_number"], m["faraday_class"], m["escalated"]] for m in w["members"]
                        if repo_identity(m["repository"]) == key],
            "sampled": [s["pr_number"] for s in w["sampled"] if repo_identity(s["repository"]) == key],
        }
    return {"format": FROZEN_FORMAT, "repository": repository, "windows": out}


def window_digest(record: dict[str, Any]) -> str:
    """16-hex digest of one frozen window record (stored next to it in the
    marker, so a tampered or corrupted record is detected)."""
    return _sha(record)[:16]


def _rank(seed: str, item: dict[str, Any]) -> str:
    return hashlib.sha256(f"{seed}\n{repo_identity(item['repository'])}\n{item['pr_number']}\n"
                          f"{item['first_head_sha']}".encode()).hexdigest()


def qualification_v5(readiness: dict[str, Any], ledger: dict[str, Any], reference: dict[str, Any], *,
                     generated_at: str, founder_node_id: str, evidence: dict[str, str],
                     threshold: int = GATE_THRESHOLD_FLOOR,
                     frozen: dict[str, dict[str, Any]] | None = None,
                     frozen_status: str = "none") -> dict[str, Any]:
    """`frozen`: verified closed-window records by index (from the status-issue
    marker; see frozen_windows_from_marker). `frozen_status`: "none" (no prior
    record, e.g. first run), "loaded", "unavailable" or "invalid". The last two
    fail closed: sampling cannot pass."""
    if frozen_status not in ("none", "loaded", "unavailable", "invalid"):
        raise QualificationError("invalid frozen_status")
    if threshold < GATE_THRESHOLD_FLOOR:
        raise QualificationError(f"threshold {threshold} is below the hard floor {GATE_THRESHOLD_FLOOR}")
    errors = persisted_local_diagnostics("qualification-reference", reference)
    if errors:
        raise QualificationError(f"reference fails schema: {errors[:3]}")
    refs = {}
    for ref in reference["prs"]:
        k = _key(ref["repository"], ref["pr_number"])
        if k in refs:
            raise QualificationError("reference repeats a PR")
        refs[k] = ref

    items = []
    for c in readiness["counted_prs"]:
        # Canonical facts come from v4 readiness (single source of truth). v5
        # never re-derives countability, freshness, qualifying runs or head
        # history. Readiness without them is refused (fail closed).
        facts = c.get("facts")
        if not isinstance(facts, dict) or facts.get("facts_version") != FACTS_VERSION:
            raise QualificationError("readiness lacks canonical facts (facts_version 1); refusing to "
                                     "re-derive them")
        # Every counted real PR is a window member, qualifying or not; a PR
        # that later stops qualifying is only ineligible (r50d).
        qualifying = c["qualification"]["status"] in QUALIFYING
        k = _key(c["repository"], c["pr_number"])
        ref = refs.get(k)
        first = facts["first_countable"]
        latest = facts["latest_qualifying"]
        transitions = facts["head_transitions"]
        contradictory = (first is None or not transitions or transitions[-1][1] != c["head_sha"]
                         or (qualifying and latest is None))
        view = _reference_view(ref, c["head_sha"])
        if contradictory:
            view = {"status": "unknown", "reason": "facts_contradictory", "reasons": ["facts_contradictory"],
                    **{k2: v for k2, v in view.items() if k2 not in ("status", "reason", "reasons",
                                                                       "review_required")}}
        cls = latest["classifier_class"] if latest else c["classifier_class"]
        items.append({
            "qualifying": qualifying,
            "repository": c["repository"], "pr_number": int(c["pr_number"]), "head_sha": c["head_sha"],
            "faraday_class": cls,
            "escalated": latest["escalated"] if latest else None,
            "reference": view,
            "founder_answer": (founder_answer(ref, c["head_sha"], founder_node_id, transitions) if ref
                               else {"status": "none", "class": None, "labeled_at": None}),
            "first_head_sha": first["head_sha"] if first else c["head_sha"],
            "selection_class": (first or {}).get("classifier_class") or cls,
            "_first_seen": _sort_key_ts(first["created_at"]) if first else (0, ""),
        })
    by_key = {_key(i["repository"], i["pr_number"]): i for i in items}

    # Frozen windows: a closed window's record (seed, members with their class
    # and escalation at closure, samples) from the status-issue marker is reused
    # verbatim and never recomputed. Classifications and mandatory escalations
    # are preserved after closure. The records must be contiguous from window 0;
    # anything else, or an unreadable record, fails closed (the gate cannot pass).
    frozen_windows: dict[str, dict[str, Any]] = {}
    frozen_repo = ""
    frozen_ok = frozen_status in ("none", "loaded")
    if frozen_ok and frozen is not None:
        if frozen_record_errors(frozen):
            frozen_ok, frozen_status = False, "invalid"
        else:
            frozen_windows, frozen_repo = frozen["windows"], frozen["repository"]
    frozen = frozen_windows
    windows = []
    departed_sampled = 0
    in_frozen: set[tuple[str, int]] = set()
    for w in (range(len(frozen)) if frozen_ok else []):
        rec = frozen[str(w)]
        repo = frozen_repo
        members = []
        for pr_number, cls_at, esc_at in rec["members"]:
            key = _key(repo, pr_number)
            in_frozen.add(key)
            item = by_key.get(key)
            if item is not None:
                item["faraday_class"], item["escalated"] = cls_at, esc_at
                item["window"], item["window_closed"] = w, True
                item["sampled"] = pr_number in rec["sampled"]
            elif pr_number in rec["sampled"]:
                departed_sampled += 1   # its sample review can no longer be answered: stays pending
            members.append({"repository": repo, "pr_number": pr_number, "faraday_class": cls_at,
                            "escalated": esc_at, "present": item is not None})
        windows.append({"index": w, "closed": True, "frozen": True, "seed": rec["seed"], "members": members,
                        "sampled": [{"repository": repo, "pr_number": n} for n in rec["sampled"]]})

    # New windows over every counted PR not already in a frozen window, in
    # canonical arrival order (first countable occurrence).
    candidates = sorted((i for i in items if _key(i["repository"], i["pr_number"]) not in in_frozen),
                        key=lambda i: (i["_first_seen"], repo_identity(i["repository"]), i["pr_number"]))
    base = len(windows)
    for w in range(len(candidates) // WINDOW + (1 if len(candidates) % WINDOW else 0)):
        members = candidates[w * WINDOW:(w + 1) * WINDOW]
        closed = len(members) == WINDOW
        # The seed binds only immutable identities (repository, PR number, first
        # countable head SHA).
        seed = _sha([[repo_identity(m["repository"]), m["pr_number"], m["first_head_sha"]] for m in members])
        sampled = []
        if closed:
            ranked = sorted(members, key=lambda m: _rank(seed, m))
            green = next((m for m in ranked if m["selection_class"] == "green"), None)
            if green is not None:
                sampled.append(green)
            other = next((m for m in ranked if m not in sampled), None)
            if other is not None:
                sampled.append(other)
        for m in members:
            m["window"] = base + w
            m["window_closed"] = closed
            m["sampled"] = any(m is s for s in sampled)
        windows.append({"index": base + w, "closed": closed, "frozen": False, "seed": seed,
                        "members": [{"repository": m["repository"], "pr_number": m["pr_number"],
                                     "faraday_class": m["faraday_class"], "escalated": m["escalated"],
                                     "present": True} for m in members],
                        "sampled": [{"repository": s["repository"], "pr_number": s["pr_number"]}
                                    for s in sampled]})
    for i in items:
        i["exceptions"] = _exceptions(i["faraday_class"], i["reference"])

    counts = {"qualifying": sum(1 for i in items if i["qualifying"]), "not_qualifying": 0,
              "reference_unknown": 0, "exception_open": 0, "sample_pending": 0,
              "window_open": 0, "verified": 0, "verified_green": 0}
    confirmed: list[dict[str, Any]] = []
    sampled_disagreements = 0
    founder_queue = []
    # Unanswered samples, counted independently of reference eligibility: a
    # sampled PR whose reference is unknown is never verified, but its
    # founder review stays outstanding (Codex P2, #23).
    samples_outstanding = departed_sampled
    # Likewise unanswered exceptions (an X4 Block is raised whatever the
    # reference says): they stay in the founder queue and block the gate even
    # when the reference is unknown (Codex P2, #23).
    exceptions_outstanding = 0
    esc = {"mandatory_total": 0, "missed": 0, "unknown": 0}
    out_prs = []
    for i in sorted(items, key=lambda i: (repo_identity(i["repository"]), i["pr_number"])):
        ans = i["founder_answer"]
        answered = ans["status"] == "answered"
        if not i["qualifying"]:
            # A window member for stability only: never verified, never queued,
            # never counted toward any gate condition (r50d).
            counts["not_qualifying"] += 1
            out_prs.append({
                "repository": i["repository"], "pr_number": i["pr_number"], "head_sha": i["head_sha"],
                "faraday_class": i["faraday_class"], "escalated": i["escalated"],
                "reference": i["reference"], "exceptions": [],
                "window": i.get("window"), "sampled": bool(i.get("sampled")),
                "founder_answer": ans, "truth_review_needed": None, "verification": "not_qualifying",
            })
            continue
        if answered and CLASS_ORDER.index(ans["class"]) > CLASS_ORDER.index(i["faraday_class"]):
            # The founder's class is more severe than Faraday's: a confirmed
            # under-classification, wherever the founder answered (queued or
            # voluntary; Codex P2, #19). Over-escalation by Faraday is safe.
            confirmed.append({"repository": i["repository"], "pr_number": i["pr_number"],
                              "faraday_class": i["faraday_class"], "founder_class": ans["class"]})
            if i.get("sampled"):
                sampled_disagreements += 1
        if i["reference"]["status"] != "known":
            status = "reference_unknown"
        elif i["exceptions"] and not answered:
            status = "exception_open"
        elif i.get("sampled") and not answered:
            status = "sample_pending"
        elif not i.get("window_closed"):
            status = "window_open"
        else:
            status = "verified"
        counts[status] += 1
        sample_open = bool(i.get("sampled")) and not answered
        if sample_open:
            samples_outstanding += 1
        exception_open = bool(i["exceptions"]) and not answered
        if exception_open:
            exceptions_outstanding += 1
        if status in ("exception_open", "sample_pending") or (
                status == "reference_unknown" and (sample_open or exception_open)):
            founder_queue.append({"repository": i["repository"], "pr_number": i["pr_number"],
                                  "head_sha": i["head_sha"],
                                  "reasons": i["exceptions"] + (["sample"] if i.get("sampled") else []),
                                  "action": f"apply exactly one {LABEL_PREFIX}<green|yellow|red|block> "
                                            f"label while the head is {i['head_sha']}"})
        truth = None
        if status == "verified":
            if i["faraday_class"] == "green":
                counts["verified_green"] += 1
            truth = (ans["class"] in FOUNDER_REVIEW_CLASSES) if answered else i["reference"]["review_required"]
            if truth:
                esc["mandatory_total"] += 1
            if i["escalated"] is None:
                esc["unknown"] += 1
            elif truth and not i["escalated"]:
                esc["missed"] += 1
        out_prs.append({
            "repository": i["repository"], "pr_number": i["pr_number"], "head_sha": i["head_sha"],
            "faraday_class": i["faraday_class"], "escalated": i["escalated"],
            "reference": i["reference"], "exceptions": i["exceptions"],
            "window": i.get("window"), "sampled": bool(i.get("sampled")),
            "founder_answer": ans, "truth_review_needed": truth, "verification": status,
        })

    if esc["missed"] > 0:
        esc_status = "fail"
    elif esc["unknown"] > 0 or esc["mandatory_total"] == 0:
        esc_status = "unknown"
    else:
        esc_status = "pass"
    sampling_status = ("fail" if sampled_disagreements else
                       "unknown" if not frozen_ok else
                       "pending" if samples_outstanding else
                       "pass" if any(w["closed"] for w in windows) else "unknown")
    conditions = {
        "verified_count": {"value": counts["verified"], "threshold": threshold,
                           "ok": counts["verified"] >= threshold},
        "escalation": {"status": esc_status, **esc},
        "sampling": {"status": sampling_status, "sampled_material_disagreements": sampled_disagreements,
                     "closed_windows": sum(1 for w in windows if w["closed"]),
                     "pending": samples_outstanding,
                     "frozen_windows": {"status": frozen_status, "count": len(frozen) if frozen_ok else 0}},
        "exceptions_open": exceptions_outstanding,
        "confirmed_material_disagreements": len(confirmed),
        "green_coverage": {"value": counts["verified_green"], "minimum": GREEN_MINIMUM,
                           "ok": counts["verified_green"] >= GREEN_MINIMUM},
    }
    met = (conditions["verified_count"]["ok"] and esc_status == "pass" and sampling_status == "pass"
           and exceptions_outstanding == 0 and not confirmed and conditions["green_coverage"]["ok"])
    mandatory = [{"repository": p["repository"], "pr_number": p["pr_number"], "faraday_class": p["faraday_class"]}
                 for p in out_prs if p["truth_review_needed"]]
    return {
        "format": V5_FORMAT,
        "generated_at": generated_at,
        "status": "proposed",
        "authoritative": False,
        "adopted": False,
        "note": ("QA-1 proposed gate; not authoritative until formally adopted by the founder. The v4 "
                 "readiness gate is unchanged. Qualification evidence only; authorizes nothing."),
        "criterion": CRITERION,
        "threshold": threshold,
        "evidence": evidence,
        "reference_policy": reference["reference_policy"],
        "counts": counts,
        "gate": {"met": met, "conditions": conditions},
        "windows": windows,
        "founder_queue": founder_queue,
        "mandatory_escalations_by_tier": mandatory,
        "confirmed_material_disagreements": confirmed,
        "prs": out_prs,
    }
