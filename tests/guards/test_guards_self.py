"""Guards must prove themselves against seeded defects before any runtime exists."""

import subprocess
import sys
import textwrap
from pathlib import Path

BAD = textwrap.dedent('''
    import requests

    def review(payload):
        result = {}
        result["approval_authorized"] = True
        try:
            requests.get("https://x")
        except:
            pass
        return payload.get("merge_authorized", True), result
''')


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "tools/guards/authorization_boundary.py", *args],
        capture_output=True,
        text=True,
    )


def test_detects_seeded_defects(tmp_path: Path):
    src = tmp_path / "faraday_review" / "providers"
    src.mkdir(parents=True)
    (src / "seeded.py").write_text(BAD)
    proc = _run(str(tmp_path))
    assert proc.returncode == 1
    for rule in ("FR-P0-001", "FR-P0-002", "FR-P0-003", "FR-P1-005"):
        assert rule in proc.stderr


def test_clean_governance_tree_passes(tmp_path: Path):
    gov = tmp_path / "faraday_review" / "governance"
    gov.mkdir(parents=True)
    (gov / "authorization.py").write_text(
        'def authorize(decision):\n'
        '    approval_authorized = decision["approval_authorized"]\n'
        '    merge_authorized = False\n'
        '    return approval_authorized, merge_authorized\n'
    )
    proc = _run(str(tmp_path))
    assert proc.returncode == 0


def test_missing_root_fails(tmp_path: Path):
    assert _run(str(tmp_path / "nope")).returncode == 1


def test_bootstrap_dir_passes_before_package_exists(tmp_path: Path):
    """Guard PR state: an existing empty directory, no package marker."""
    assert _run(str(tmp_path)).returncode == 0


def test_bootstrap_dir_fails_under_explicit_override(tmp_path: Path):
    assert _run(str(tmp_path), "--require-tree").returncode == 1


def test_marker_only_package_fails(tmp_path: Path):
    """__init__.py alone must not attest as an implemented tree."""
    pkg = tmp_path / "faraday_review"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    proc = _run(str(tmp_path))
    assert proc.returncode == 1
    assert "marker-only package" in proc.stderr


def test_marker_plus_runtime_module_passes(tmp_path: Path):
    """The package marker and the first real module must land together."""
    gov = tmp_path / "faraday_review" / "governance"
    gov.mkdir(parents=True)
    (tmp_path / "faraday_review" / "__init__.py").write_text("")
    (gov / "__init__.py").write_text("")
    (gov / "authorization.py").write_text(
        'def authorize(decision):\n'
        '    approval_authorized = decision["approval_authorized"]\n'
        '    return approval_authorized\n'
    )
    assert _run(str(tmp_path)).returncode == 0


def test_init_files_are_still_scanned(tmp_path: Path):
    """Excluded from the emptiness count, not from violation scanning."""
    prov = tmp_path / "faraday_review" / "providers"
    prov.mkdir(parents=True)
    (tmp_path / "faraday_review" / "__init__.py").write_text("")
    (prov / "__init__.py").write_text('risk_class = "GREEN"\n')
    (prov / "router.py").write_text("def route():\n    return []\n")
    proc = _run(str(tmp_path))
    assert proc.returncode == 1
    assert "FR-P0-001" in proc.stderr
    assert "providers/__init__.py" in proc.stderr
