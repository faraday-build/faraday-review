"""Qualification ledger + readiness are byte-identical across hash seeds,
process repeats, and input creation order."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from qualification_support import DIGEST_B, write_label, write_real_prs, write_run  # noqa: E402

SEEDS = ("0", "1", "7", "19", "43", "101")
TS = "2026-11-12T00:00:00Z"


def _build(root: Path, reverse: bool = False) -> None:
    steps = [
        lambda: write_real_prs(root, 21),
        lambda: write_run(root, "rerun-3", pr=3, head="h3", created_at="2026-11-01T12:00:00Z"),
        lambda: write_run(root, "push-4", pr=4, head="h4b", created_at="2026-11-02T09:00:00Z"),
        lambda: write_run(root, "drift-5", pr=5, head="h5", digest=DIGEST_B,
                          created_at="2026-11-02T09:00:00Z"),
        # Occurrence-level paths: failed retry, budget exhaustion then
        # success, a partial copy without decision.json, a later failed head.
        lambda: write_run(root, "retry-6", pr=6, head="h6", created_at="2026-11-01T09:00:00Z",
                          final_status="failed", failure_mode="runner_lost"),
        lambda: write_run(root, "budget-8", pr=8, head="h8", created_at="2026-11-01T09:00:00Z",
                          cls="block", reasons=("github_api_budget_exceeded",)),
        lambda: write_run(root, "partial-7", pr=7, head="h7", created_at="2026-11-01T10:07:00Z",
                          with_decision=False),
        lambda: write_run(root, "failed-head-9", pr=9, head="h9b",
                          created_at="2026-11-03T09:00:00Z", final_status="failed",
                          failure_mode="runner_lost"),
        lambda: write_label(root, "label-1", pr=1, head="h1", label="faraday/founder-green"),
        lambda: write_label(root, "label-2", pr=2, head="h2", label="faraday/founder-red",
                            labeled_at="2026-11-01T13:00:00Z"),
        lambda: write_label(root, "label-x", pr=2, head="h2", node_id="U_other",
                            login="tomrivera-PSE", label="faraday/founder-green"),
    ]
    for step in (reversed(steps) if reverse else steps):
        step()


def _run(tree: Path, out: Path, seed: str) -> bytes:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = out / f"seed-{seed}"  # identical file names: evidence records names
    out.mkdir(parents=True, exist_ok=True)
    ledger, report, synth = out / "ledger.json", out / "readiness.json", out / "s.json"
    synth.write_text('[{"repository": "faraday-build/pilot", "pr_number": 21, "reason": "seeded"}]\n')
    for argv in (
        ["aggregate", "--input-dir", str(tree), "--output", str(ledger), "--generated-at", TS],
        ["readiness", "--ledger", str(ledger), "--synthetic", str(synth), "--generated-at", TS,
         "--output", str(report)],
    ):
        proc = subprocess.run([sys.executable, "-m", "faraday_review.cli", *argv], env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    return ledger.read_bytes() + b"\x00" + report.read_bytes()


def test_qualification_outputs_byte_identical_across_seeds(tmp_path):
    tree = tmp_path / "in"
    _build(tree)
    outputs = {seed: _run(tree, tmp_path, seed) for seed in SEEDS}
    assert len(set(outputs.values())) == 1
    # Sanity: the scenario exercises the interesting paths.
    assert b'"policy_digest_drift":true' in outputs["0"]
    assert b'"superseded":true' in outputs["0"]
    assert b'"api_budget_exceeded_runs":1' in outputs["0"]
    assert b'"failed_runs":2' in outputs["0"]


def test_qualification_independent_of_creation_order(tmp_path):
    forward, backward = tmp_path / "f" / "in", tmp_path / "b" / "in"
    _build(forward)
    _build(backward, reverse=True)
    seed = os.environ.get("PYTHONHASHSEED", "0")
    assert _run(forward, tmp_path / "f", seed) == _run(backward, tmp_path / "b", seed)
