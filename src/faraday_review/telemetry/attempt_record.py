"""Rerun-durable attempt records (S6 F-2).

GitHub deletes a workflow run's earlier-attempt artifacts when the run is
re-run, so the per-attempt `faraday-review-pr<N>-<head>-a<attempt>` artifact
of attempt 1 is gone once attempt 2 starts. Check runs, job logs and their
outputs survive. Every shadow attempt therefore also publishes one check run
named ATTEMPT_RECORD_CHECK_NAME on the PR head, with
`external_id = faraday-review-attempt:<run_id>:<run_attempt>`, conclusion
neutral, whose `output.text` carries the attempt's own aggregation files
(telemetry.jsonl, and decision.json / run-binding.json when present)
verbatim, each with its sha256, inside one marker block.

Value-safe by construction: only the files `collect` already archives from
the artifact (ARCHIVED_FILES), which already go to the same audience; no
path-bearing file (pr-context.json, gitleaks-report.json, evidence) is ever
included.

`collect` reads these records back only for attempts whose artifact is
gone, and verifies every byte (see github/collection.py). This module is
pure: no network, no clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..canonical import canonical_bytes

ATTEMPT_RECORD_CHECK_NAME = "faraday-review / attempt-record"
RECORD_FORMAT = "faraday-review/attempt-record/v1"
EXTERNAL_ID_PREFIX = "faraday-review-attempt:"
EXTERNAL_ID_RE = re.compile(r"^faraday-review-attempt:([1-9][0-9]*):([1-9][0-9]*)$")
# The files a record may carry, in this order. telemetry.jsonl is required.
RECORD_FILES = ("telemetry.jsonl", "decision.json", "run-binding.json")
REQUIRED_FILE = "telemetry.jsonl"
BEGIN_MARKER = "<!-- faraday-review-attempt-record:begin -->"
END_MARKER = "<!-- faraday-review-attempt-record:end -->"
# GitHub caps check-run output.text at 65535 characters. A record that does
# not fit is never truncated: publication fails closed.
MAX_TEXT_CHARS = 65535

_PREAMBLE = (
    "Faraday Review attempt record (shadow; authorizes nothing). A durable copy of "
    "this attempt's telemetry for qualification collection: GitHub deletes a run's "
    "earlier-attempt artifacts when the run is re-run."
)


class AttemptRecordError(ValueError):
    """A record that cannot be built or does not verify. `reason` is one of
    the machine-readable rejection reasons the collection manifest records."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def external_id(run_id: int, run_attempt: int) -> str:
    return f"{EXTERNAL_ID_PREFIX}{int(run_id)}:{int(run_attempt)}"


def _canonical_line(obj: Any) -> str:
    """Canonical JSON (canonical.py) as one line, without its trailing LF."""
    return canonical_bytes(obj).decode("utf-8")[:-1]


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def build_envelope(run_id: int, run_attempt: int, files: dict[str, bytes]) -> dict[str, Any]:
    unknown = sorted(set(files) - set(RECORD_FILES))
    if unknown:
        raise AttemptRecordError("malformed", f"attempt record cannot carry {unknown}")
    if REQUIRED_FILE not in files:
        raise AttemptRecordError("malformed", f"attempt record needs {REQUIRED_FILE}")
    entries = []
    for name in RECORD_FILES:
        if name not in files:
            continue
        raw = files[name]
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttemptRecordError("malformed", f"{name} is not UTF-8") from exc
        entries.append({"name": name, "sha256": _sha256(raw), "size": len(raw),
                        "content": content})
    return {"format": RECORD_FORMAT, "run_id": int(run_id), "run_attempt": int(run_attempt),
            "files": entries}


def build_record_text(run_id: int, run_attempt: int, files: dict[str, bytes]) -> str:
    """The check-run output.text. Fails closed (AttemptRecordError
    'oversize') when it would exceed GitHub's limit."""
    envelope = _canonical_line(build_envelope(run_id, run_attempt, files))
    text = "\n".join([_PREAMBLE, "", BEGIN_MARKER, "```json", envelope, "```", END_MARKER, ""])
    if len(text) > MAX_TEXT_CHARS:
        raise AttemptRecordError("oversize", f"attempt record is {len(text)} characters; "
                                             f"GitHub allows {MAX_TEXT_CHARS}; not published")
    return text


def parse_record_text(text: Any) -> dict[str, Any]:
    """Envelope from a check-run output.text, with every byte verified:
    exactly one marker block, canonical JSON, known format, allowed unique
    file names, sha256 and size of each file. Returns
    {"run_id", "run_attempt", "files": {name: bytes}}."""
    if not isinstance(text, str):
        raise AttemptRecordError("malformed", "attempt record has no output text")
    if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise AttemptRecordError("malformed", "attempt record needs exactly one marker block")
    body = text.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0]
    lines = body.strip("\n").split("\n")
    if len(lines) != 3 or lines[0] != "```json" or lines[2] != "```":
        raise AttemptRecordError("malformed", "attempt record block is not one fenced JSON line")
    try:
        envelope = json.loads(lines[1])
    except json.JSONDecodeError as exc:
        raise AttemptRecordError("malformed", "attempt record is not JSON") from exc
    if not isinstance(envelope, dict) or _canonical_line(envelope) != lines[1]:
        raise AttemptRecordError("malformed", "attempt record is not canonical JSON")
    if set(envelope) != {"format", "run_id", "run_attempt", "files"} \
            or envelope["format"] != RECORD_FORMAT:
        raise AttemptRecordError("malformed", "attempt record has an unknown format")
    run_id, run_attempt = envelope["run_id"], envelope["run_attempt"]
    if not all(type(v) is int and v >= 1 for v in (run_id, run_attempt)):
        raise AttemptRecordError("malformed", "attempt record run identity is not two integers")
    entries = envelope["files"]
    if not isinstance(entries, list):
        raise AttemptRecordError("malformed", "attempt record files is not a list")
    files: dict[str, bytes] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "sha256", "size", "content"}:
            raise AttemptRecordError("malformed", "attempt record file entry is malformed")
        name = entry["name"]
        if name not in RECORD_FILES or name in files or not isinstance(entry["content"], str):
            raise AttemptRecordError("malformed", "attempt record file name is unknown or repeated")
        raw = entry["content"].encode("utf-8")
        if entry["sha256"] != _sha256(raw) or entry["size"] != len(raw):
            raise AttemptRecordError("sha256_mismatch",
                                     f"attempt record {name}: sha256/size does not match content")
        files[name] = raw
    if REQUIRED_FILE not in files:
        raise AttemptRecordError("malformed", f"attempt record lacks {REQUIRED_FILE}")
    return {"run_id": run_id, "run_attempt": run_attempt, "files": files}
