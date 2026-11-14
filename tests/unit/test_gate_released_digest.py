"""Gate counts only runs evaluated under a released policy digest
(GHO-005 A2 gap G1). "Verified" alone proves only that decision.json
reproduces the run fingerprint; it does not prove the digest is the
governance release the founder baseline refers to."""

from __future__ import annotations

import json

from qualification_support import DIGEST_A, DIGEST_B, write_label, write_run

from faraday_review.cli import main
from faraday_review.github.preflight import RELEASED_POLICY_DIGEST
from faraday_review.telemetry.qualification import aggregate, local_validation_errors, readiness

TS = "2026-11-12T00:00:00Z"
EV = {"name": "x.json", "sha256": "sha256:" + "0" * 64}


def _prs(root, digest, n=20):
    for pr in range(1, n + 1):
        write_run(root, f"r{pr}", pr=pr, head=f"h{pr}", digest=digest,
                  created_at=f"2026-11-01T10:{pr:02d}:00Z")
        write_label(root, f"l{pr}", pr=pr, head=f"h{pr}")


def _report(root, released):
    report = readiness(aggregate(root, generated_at=TS), [], generated_at=TS,
                       ledger_evidence=EV, synthetic_evidence=EV,
                       released_policy_digests=released)
    assert local_validation_errors("qualification-readiness", report) == []
    return report


def test_unreleased_digest_is_misconfigured_and_never_meets_gate(tmp_path):
    _prs(tmp_path, DIGEST_B)
    report = _report(tmp_path, [DIGEST_A])
    assert report["adjudicated_qualifying_real_pr_count"] == 0
    assert report["qualification"]["exclusions"]["misconfigured"] == 20
    assert report["gate"]["met"] is False
    assert report["gate"]["released_policy_digests"] == [DIGEST_A]


def test_released_digest_still_meets_gate(tmp_path):
    _prs(tmp_path, DIGEST_A)
    report = _report(tmp_path, [DIGEST_A])
    assert report["gate"]["met"] is True


def test_cli_defaults_to_the_governance_release(tmp_path):
    _prs(tmp_path / "in", DIGEST_B)
    ledger, out, synth = tmp_path / "l.json", tmp_path / "r.json", tmp_path / "s.json"
    synth.write_text("[]\n")
    assert main(["aggregate", "--input-dir", str(tmp_path / "in"), "--output", str(ledger),
                 "--generated-at", TS]) == 0
    assert main(["readiness", "--ledger", str(ledger), "--synthetic", str(synth),
                 "--generated-at", TS, "--output", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["gate"]["met"] is False
    assert report["gate"]["released_policy_digests"] == [RELEASED_POLICY_DIGEST]
