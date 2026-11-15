"""QA-1 L1: the vendored ADR-0014 reference path tier (agent-contracts #15)."""

from __future__ import annotations

import hashlib
import json

import pytest

from faraday_review.telemetry import reference_tier as rt


def test_vendored_files_match_their_pin():
    pin = json.loads((rt.POLICY_DIR / "VENDORED.json").read_text(encoding="utf-8"))
    assert pin["source"] == "tomrivera-ops/agent-contracts"
    assert pin["status"] == "proposed"
    assert set(pin["files"]) == {rt.POLICY_FILE, rt.VECTORS_FILE}
    for name, digest in pin["files"].items():
        assert hashlib.sha256((rt.POLICY_DIR / name).read_bytes()).hexdigest() == digest


def test_every_vendored_path_vector_passes():
    vectors = rt.vectors()
    assert len(vectors["paths"]) >= 30
    for path, want in vectors["paths"]:
        assert rt.path_tier(path) == want, path


def test_every_vendored_pr_vector_passes():
    for case in rt.vectors()["prs"]:
        assert rt.pr_tier(case["paths"], case["floor"]) == case["tier"], case


@pytest.mark.parametrize("floor", ["", "A1", "A2", "unknown", None])
def test_unknown_floor_is_unknown(floor):
    assert rt.pr_tier(["README.md"], floor) == rt.UNKNOWN


def test_vendored_file_is_path_patterns_only_and_proposed():
    ref = json.loads((rt.POLICY_DIR / rt.POLICY_FILE).read_text(encoding="utf-8"))
    assert ref["status"] == "proposed" and ref["default"] == "A2"
    assert set(ref) <= {"policy", "status", "purpose", "evaluation", "glob", "a0_path_set",
                        "a0_path_exclusions", "a3_paths", "a1_paths", "default", "note"}
    raw = "".join((rt.POLICY_DIR / n).read_text(encoding="utf-8") for n in (rt.POLICY_FILE, rt.VECTORS_FILE))
    for token in ("SHA256:", "allowed-signers", "faraday-merge-go", "grant", "node_id", "U_kgD"):
        assert token not in raw


def test_output_provenance_names_no_repository_or_commit():
    prov = rt.load_policy()["provenance"]
    assert prov == {"name": "adr-0014-reference-path-tiers", "sha256": prov["sha256"], "status": "proposed"}


def test_altered_vendored_policy_fails_closed(tmp_path, monkeypatch):
    for name in ("VENDORED.json", rt.POLICY_FILE, rt.VECTORS_FILE):
        (tmp_path / name).write_bytes((rt.POLICY_DIR / name).read_bytes())
    policy = json.loads((tmp_path / rt.POLICY_FILE).read_text())
    policy["default"] = "A0"
    (tmp_path / rt.POLICY_FILE).write_text(json.dumps(policy))
    monkeypatch.setattr(rt, "POLICY_DIR", tmp_path)
    rt.load_policy.cache_clear()
    try:
        with pytest.raises(rt.ReferencePolicyError, match="does not match its pin"):
            rt.load_policy()
    finally:
        rt.load_policy.cache_clear()


def test_glob_semantics_match_agent_contracts():
    assert rt._glob_re("**/tests/**").match("tests/a.py")
    assert rt._glob_re("**/tests/**").match("x/y/tests/a.py")
    assert not rt._glob_re("*.md").match("docs/a.md")
    assert rt._glob_re("**/*.md").match("a.md")
    assert not rt._glob_re("tests/**").match("xtests/a")
