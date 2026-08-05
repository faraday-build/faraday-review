"""Append-only JSONL telemetry under artifacts/reviews/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

DEFAULT_ROOT = Path("artifacts/reviews")


def append_event(event: dict[str, Any], root: Path = DEFAULT_ROOT) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "telemetry.jsonl"
    line = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def read_events(root: Path = DEFAULT_ROOT) -> Iterator[dict[str, Any]]:
    path = root / "telemetry.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
