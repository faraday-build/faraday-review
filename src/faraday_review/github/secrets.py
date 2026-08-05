"""GitHub secret-scanning alerts for the PR head, when available.

Discovery: secret scanning is disabled estate-wide, so this path is
expected-absent (404/403) and gitleaks (deterministic/secret_scan.py) is
the primary detector. Alert payloads never include the secret value in the
finding we emit — only rule/type metadata and location.
"""

from __future__ import annotations

from ..errors import GitHubApiError
from ..models import DeterministicFinding
from .client import GitHubClient


def collect_secret_alerts(client: GitHubClient, repository: str) -> list[DeterministicFinding]:
    try:
        alerts = client.paginate(f"/repos/{repository}/secret-scanning/alerts", params={"state": "open"})
    except GitHubApiError:
        # Scanning unavailable on this repo/plan: recorded as absent, and
        # gitleaks remains the detector of record. Never a pass/fail signal.
        return []
    findings: list[DeterministicFinding] = []
    for alert in alerts:
        findings.append(
            DeterministicFinding(
                rule_id=str(alert.get("secret_type", "unknown-secret-type")),
                check="secret_scan",
                severity="blocking",
                rule_class="github-secret-scanning",
                message=(
                    f"open GitHub secret-scanning alert #{alert.get('number', '?')} "
                    f"({alert.get('secret_type_display_name', 'secret')}) — value withheld"
                ),
            )
        )
    return findings
