"""Dataclass mirrors of the agent-contracts v0.3.0 review-plane contracts.

Construction happens wherever data is assembled, but the authorization
fields (approval_authorized, merge_authorized, founder_review_required,
risk_class, policy_digest, policy_version, contract_version) are only ever
POPULATED into decision documents inside faraday_review.governance — the
authorization-boundary guard enforces that at CI time. This module defines
shapes; it decides nothing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

RISK_CLASSES = ("green", "yellow", "red", "block")
CLASS_ORDER = {"green": 0, "yellow": 1, "red": 2, "block": 3}


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Drop None values so optional absent fields stay absent, not null."""
    return {k: v for k, v in d.items() if v is not None}


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    additions: int | None = None
    deletions: int | None = None
    previous_path: str | None = None


@dataclass(frozen=True)
class ObservedCheck:
    context: str
    state: str  # success | failure | pending | error | missing


@dataclass(frozen=True)
class PullRequestContext:
    contract_version: str
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    collected_at: str
    base_ref: str | None = None
    head_ref: str | None = None
    draft: bool | None = None
    changed_file_count: int | None = None
    diff_bytes: int | None = None
    changed_files: list[ChangedFile] = field(default_factory=list)
    required_checks_observed: list[ObservedCheck] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out = _clean(asdict(self))
        out["changed_files"] = [_clean(asdict(f)) for f in self.changed_files]
        out["required_checks_observed"] = [asdict(c) for c in self.required_checks_observed]
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "PullRequestContext":
        data = dict(raw)
        data["changed_files"] = [ChangedFile(**f) for f in raw.get("changed_files", [])]
        data["required_checks_observed"] = [
            ObservedCheck(**c) for c in raw.get("required_checks_observed", [])
        ]
        return cls(**data)


@dataclass(frozen=True)
class DeterministicFinding:
    rule_id: str
    check: str
    severity: str  # info | warning | blocking
    message: str
    path: str | None = None
    line: int | None = None
    rule_class: str | None = None

    def to_json(self) -> dict[str, Any]:
        return _clean(asdict(self))


@dataclass(frozen=True)
class ReviewEvidence:
    """PullRequestReviewEvidence (Correction 4).

    provider_results and required_providers ship as EMPTY ARRAYS, not
    absent keys, so Phase 2 populates them without a schema migration.
    """

    contract_version: str
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    collected_at: str
    reviewer_version: str | None = None
    evidence_class: str | None = None
    deterministic_findings: list[DeterministicFinding] = field(default_factory=list)
    provider_results: list[dict[str, Any]] = field(default_factory=list)
    required_providers: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out = _clean(asdict(self))
        out["deterministic_findings"] = [f.to_json() for f in self.deterministic_findings]
        out["provider_results"] = list(self.provider_results)
        out["required_providers"] = list(self.required_providers)
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "ReviewEvidence":
        data = dict(raw)
        data["deterministic_findings"] = [
            DeterministicFinding(**f) for f in raw.get("deterministic_findings", [])
        ]
        return cls(**data)
