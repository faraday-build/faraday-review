"""JSON Schema validation against the vendored agent-contracts release.

Schemas are vendored from tomrivera-ops/agent-contracts v0.4.0 (see
src/faraday_review/schemas/README.md for provenance). No network fetch:
the validator works offline and its inputs cannot vary with registry
availability.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    path = SCHEMA_DIR / f"{schema_name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validation_errors(schema_name: str, instance: Any) -> list[str]:
    """Sorted, deterministic error strings; empty list means valid."""
    errors = _validator(schema_name).iter_errors(instance)
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in errors
    )
