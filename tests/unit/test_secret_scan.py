import json
from pathlib import Path

import pytest

from faraday_review.deterministic.secret_scan import (
    assert_no_value_leak,
    parse_gitleaks_report,
)

SECRET_VALUE = "AKIAIOSFODNN7EXAMPLEDONOTLEAK"


def _report(tmp_path: Path) -> Path:
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            [
                {
                    "RuleID": "generic-api-key",
                    "File": "src/settings.py",
                    "StartLine": 47,
                    "Secret": SECRET_VALUE,
                    "Match": f"KEY = '{SECRET_VALUE}'",
                    "Line": f"KEY = '{SECRET_VALUE}'",
                    "Entropy": 4.31,
                }
            ]
        )
    )
    return path


def test_findings_never_contain_the_matched_value(tmp_path: Path):
    findings = parse_gitleaks_report(_report(tmp_path))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == "blocking"
    assert finding.check == "secret_scan"
    assert finding.rule_id == "generic-api-key"
    assert finding.path == "src/settings.py"
    assert finding.line == 47
    assert finding.rule_class == "entropy=4.31"
    blob = json.dumps(finding.to_json())
    assert SECRET_VALUE not in blob
    assert_no_value_leak(findings, [SECRET_VALUE])


def test_value_leak_hook_detects_leaks(tmp_path: Path):
    findings = parse_gitleaks_report(_report(tmp_path))
    poisoned = findings + [
        type(findings[0])(
            rule_id="x",
            check="secret_scan",
            severity="blocking",
            message=f"leaked {SECRET_VALUE}",
        )
    ]
    with pytest.raises(AssertionError):
        assert_no_value_leak(poisoned, [SECRET_VALUE])


def test_missing_report_is_empty(tmp_path: Path):
    assert parse_gitleaks_report(tmp_path / "absent.json") == []
