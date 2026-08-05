"""Read/write evidence and context documents in canonical form."""

from __future__ import annotations

from pathlib import Path

from ..canonical import read_canonical, write_canonical
from ..models import PullRequestContext, ReviewEvidence


def write_context(path: Path, context: PullRequestContext) -> None:
    write_canonical(path, context.to_json())


def write_evidence(path: Path, evidence: ReviewEvidence) -> None:
    write_canonical(path, evidence.to_json())


def read_json(path: Path) -> dict:
    doc = read_canonical(path)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return doc
