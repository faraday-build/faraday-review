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
    _occurrence_exclusions,
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


def _latest_qualifying(ledger: dict[str, Any], repository: str, pr: int, head: str,
                       released: list[str] | None) -> tuple[str | None, bool | None]:
    """Classification and recorded escalation, taken together from the
    latest *qualifying* occurrence of the current-head entry:
    identity-eligible, completed, fresh and free of every v4 qualification
    exclusion (policy digest, blocking-reason category), exactly as
    readiness judges it (Codex P2, #19). Both values come from the same
    occurrence, so a newer non-qualifying run cannot lend its class to an
    older qualifying one (Codex P2 r2, #19). (None, None) if no qualifying
    occurrence exists; escalation is None if not recorded as a boolean."""
    best = None
    for entry in ledger["entries"]:
        if _key(entry["repository"], entry["pr_number"]) != _key(repository, pr) or \
                entry["head_sha"] != head or not entry.get("current"):
            continue
        for run in entry["runs"]:
            for occ in run["occurrence_log"]:
                if (occ.get("eligible") and occ.get("final_status") == "completed"
                        and occ.get("stale_state") == "fresh"
                        and occ.get("classifier_class") in CLASS_ORDER
                        and not _occurrence_exclusions(occ, run.get("policy_digest_verified", False),
                                                       run.get("policy_bundle_digest"), released)):
                    rank = (str(occ.get("created_at")), str(occ.get("event_key")))
                    if best is None or rank > best[0]:
                        best = (rank, occ.get("classifier_class"), occ.get("escalated"))
    if best is None:
        return None, None
    return best[1], best[2] if isinstance(best[2], bool) else None


def _selection_class(ledger: dict[str, Any], repository: str, pr: int,
                     first_head: str) -> str | None:
    """The class used to select samples: that of the EARLIEST eligible,
    completed, fresh occurrence on the PR's first head (else on any of its
    heads). Only facts recorded once per occurrence are used. Policy-digest
    qualification is deliberately NOT consulted: a later release can change
    which occurrences qualify, and that must not change which members of a
    closed window were sampled (Codex P2 #23; r50d stability correction).
    None if the PR has no such occurrence."""
    best = None
    for entry in ledger["entries"]:
        if _key(entry["repository"], entry["pr_number"]) != _key(repository, pr):
            continue
        on_first = entry["head_sha"] == first_head
        for run in entry["runs"]:
            for occ in run["occurrence_log"]:
                if (occ.get("eligible") and occ.get("final_status") == "completed"
                        and occ.get("stale_state") == "fresh"
                        and occ.get("classifier_class") in CLASS_ORDER):
                    rank = (0 if on_first else 1, str(occ.get("created_at")), str(occ.get("event_key")))
                    if best is None or rank < best[0]:
                        best = (rank, occ["classifier_class"])
    return best[1] if best else None


def _arrival(ledger: dict[str, Any], repository: str, pr: int) -> str:
    """Earliest recorded occurrence time of the PR in the ledger: a recorded,
    never-rewritten fact, so arrival order does not depend on live reference
    evidence (r50d)."""
    times = [str(occ.get("created_at")) for entry in ledger["entries"]
             if _key(entry["repository"], entry["pr_number"]) == _key(repository, pr)
             for run in entry["runs"] for occ in run["occurrence_log"] if occ.get("created_at")]
    return min(times) if times else ""


def _seen_at(h: dict[str, Any]) -> str | None:
    """When the head became current: GitHub's earliest check suite for the
    commit (created on push), else its first shadow-run telemetry. The suite
    time precedes the run, so a label applied while the run was still
    queued or running is bound to the right head (Codex P2, #19)."""
    times = [t for t in (h.get("first_suite_at"), h.get("first_created_at")) if t]
    return min(times) if times else None


def _tenure(ref: dict[str, Any], head: str) -> tuple[str | None, str | None]:
    """[start, end) during which `head` was the PR head (a later head's
    arrival ends it)."""
    dated = sorted(((_seen_at(h), h["head_sha"]) for h in ref["heads"] if _seen_at(h)))
    for i, (start, sha) in enumerate(dated):
        if sha == head:
            nxt = next((t for t, s in dated[i + 1:] if s != head), None)
            return start, nxt
    return None, None


def founder_answer(ref: dict[str, Any], head: str, founder_node_id: str) -> dict[str, Any]:
    start, end = _tenure(ref, head)
    if start is None:
        return {"status": "none", "class": None, "labeled_at": None}
    present: dict[str, str] = {}
    ignored = 0
    for ev in ref["founder_label_events"]:
        if ev["actor_node_id"] != founder_node_id:
            ignored += 1
            continue
        if ev["created_at"] < start or (end is not None and ev["created_at"] >= end):
            continue
        cls = ev["label"].removeprefix(LABEL_PREFIX)
        if cls not in CLASS_ORDER:
            continue
        if ev["event"] == "labeled":
            present[cls] = ev["created_at"]
        else:
            present.pop(cls, None)
    out: dict[str, Any] = {"status": "none", "class": None, "labeled_at": None}
    if len(present) == 1:
        (cls, at), = present.items()
        out = {"status": "answered", "class": cls, "labeled_at": at}
    elif len(present) > 1:
        out = {"status": "ambiguous", "class": None, "labeled_at": None}
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


def _rank(seed: str, item: dict[str, Any]) -> str:
    return hashlib.sha256(f"{seed}\n{repo_identity(item['repository'])}\n{item['pr_number']}\n"
                          f"{item['first_head_sha']}".encode()).hexdigest()


def qualification_v5(readiness: dict[str, Any], ledger: dict[str, Any], reference: dict[str, Any], *,
                     generated_at: str, founder_node_id: str, evidence: dict[str, str],
                     threshold: int = GATE_THRESHOLD_FLOOR) -> dict[str, Any]:
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
        # Every counted real PR is a window member, qualifying or not, so a PR
        # that later stops qualifying (an incomplete or excluded new head)
        # never shifts window boundaries; it is only ineligible (r50d).
        qualifying = c["qualification"]["status"] in QUALIFYING
        k = _key(c["repository"], c["pr_number"])
        ref = refs.get(k)
        view = _reference_view(ref, c["head_sha"])
        q_cls, q_escalated = _latest_qualifying(ledger, c["repository"], c["pr_number"], c["head_sha"],
                                                readiness.get("gate", {}).get("released_policy_digests"))
        cls = q_cls if q_cls is not None else c["classifier_class"]
        items.append({
            "qualifying": qualifying,
            "repository": c["repository"], "pr_number": int(c["pr_number"]), "head_sha": c["head_sha"],
            "faraday_class": cls,
            "escalated": q_escalated,
            "reference": view,
            "exceptions": _exceptions(cls, view),
            "founder_answer": (founder_answer(ref, c["head_sha"], founder_node_id) if ref
                               else {"status": "none", "class": None, "labeled_at": None}),
            "first_head_sha": ref["first_head_sha"] if ref else c["head_sha"],
            "selection_class": _selection_class(
                ledger, c["repository"], c["pr_number"], ref["first_head_sha"] if ref else c["head_sha"]) or cls,
            "_first_seen": _arrival(ledger, c["repository"], c["pr_number"]),
        })

    # Sampling over every qualifying PR in arrival order. A member whose
    # reference is unknown stays in its window (it is ineligible for
    # verification, not removed), so a later push or a missing receipt on one
    # PR never shifts windows or re-rolls seeds for others (Codex P2 r2, #19).
    candidates = sorted(items,
                        key=lambda i: (i["_first_seen"], repo_identity(i["repository"]), i["pr_number"]))
    windows = []
    for w in range(len(candidates) // WINDOW + (1 if len(candidates) % WINDOW else 0)):
        members = candidates[w * WINDOW:(w + 1) * WINDOW]
        closed = len(members) == WINDOW
        # The seed binds only immutable, GitHub/git-assigned identities (repository,
        # PR number, first head SHA): a deleted or edited Codex receipt can no
        # longer re-roll a window (Codex P2 #23, r50d).
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
            m["window"] = w
            m["window_closed"] = closed
            m["sampled"] = any(m is s for s in sampled)
        windows.append({"index": w, "closed": closed, "seed": seed,
                        "members": [{"repository": m["repository"], "pr_number": m["pr_number"]}
                                    for m in members],
                        "sampled": [{"repository": s["repository"], "pr_number": s["pr_number"]}
                                    for s in sampled]})

    counts = {"qualifying": sum(1 for i in items if i["qualifying"]), "not_qualifying": 0,
              "reference_unknown": 0, "exception_open": 0, "sample_pending": 0,
              "window_open": 0, "verified": 0, "verified_green": 0}
    confirmed: list[dict[str, Any]] = []
    sampled_disagreements = 0
    founder_queue = []
    # Unanswered samples, counted independently of reference eligibility: a
    # sampled PR whose reference is unknown is never verified, but its
    # founder review stays outstanding (Codex P2, #23).
    samples_outstanding = 0
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
                       "pending" if samples_outstanding else
                       "pass" if any(w["closed"] for w in windows) else "unknown")
    conditions = {
        "verified_count": {"value": counts["verified"], "threshold": threshold,
                           "ok": counts["verified"] >= threshold},
        "escalation": {"status": esc_status, **esc},
        "sampling": {"status": sampling_status, "sampled_material_disagreements": sampled_disagreements,
                     "closed_windows": sum(1 for w in windows if w["closed"]),
                     "pending": samples_outstanding},
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
