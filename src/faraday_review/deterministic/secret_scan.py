"""Secret detection findings.

gitleaks over the diff range is the primary detector (GitHub secret
scanning is disabled estate-wide per discovery; the alerts path in
github/secrets.py is included but expected-absent). Detection is a Block
condition, never a Green-with-warning.

THE MATCHED SECRET VALUE NEVER APPEARS IN A FINDING. Only rule ID, file
path, line number, and entropy/rule class survive parsing; every value-
bearing field gitleaks emits is dropped here, at the boundary.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..models import DeterministicFinding

# gitleaks report fields that can carry the matched value. Dropped on parse.
_VALUE_BEARING_FIELDS = ("Secret", "Match", "Line", "line", "secret", "match")

GITLEAKS_TIMEOUT_SECONDS = 300


def run_gitleaks(repo_dir: Path, base_sha: str, head_sha: str, report_path: Path) -> int:
    """Run gitleaks over the commit range. Argv list, never shell=True."""
    cmd = [
        "gitleaks",
        "detect",
        "--source", str(repo_dir),
        "--log-opts", f"{base_sha}..{head_sha}",
        "--report-format", "json",
        "--report-path", str(report_path),
        "--exit-code", "2",
        "--redact",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=GITLEAKS_TIMEOUT_SECONDS)
    return proc.returncode


def parse_gitleaks_report(report_path: Path) -> list[DeterministicFinding]:
    if not report_path.exists():
        return []
    raw = json.loads(report_path.read_text(encoding="utf-8") or "[]")
    findings: list[DeterministicFinding] = []
    for leak in raw:
        entropy = leak.get("Entropy")
        findings.append(
            DeterministicFinding(
                rule_id=str(leak.get("RuleID", "unknown-rule")),
                check="secret_scan",
                severity="blocking",
                path=str(leak.get("File")) if leak.get("File") else None,
                line=int(leak["StartLine"]) if leak.get("StartLine") else None,
                rule_class=f"entropy={entropy:.2f}" if isinstance(entropy, (int, float)) else "rule-match",
                message=(
                    f"secret detected by rule {leak.get('RuleID', 'unknown-rule')!r} "
                    "(value withheld by contract)"
                ),
            )
        )
    return findings


def assert_no_value_leak(findings: list[DeterministicFinding], forbidden_values: list[str]) -> None:
    """Test hook: prove no forbidden value survived into any finding field."""
    for finding in findings:
        blob = json.dumps(finding.to_json())
        for value in forbidden_values:
            if value and value in blob:
                raise AssertionError("secret value leaked into a finding")
