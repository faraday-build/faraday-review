"""Load and validate the six-file governance policy bundle.

Definitions-only input. Any defect in the bundle resolves to a Block
reason, never a permissive default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

BUNDLE_FILES = (
    "approval-policy.yaml",
    "protected-paths.yaml",
    "evidence-requirements.yaml",
    "provider-requirements.yaml",
)
BUNDLE_SCHEMA = "policy.schema.json"
SUPPORTED_POLICY_MAJOR = 1


@dataclass(frozen=True)
class PolicyBundle:
    root: Path
    policy_version: str
    approval_policy: dict[str, Any]
    protected_paths: dict[str, Any]
    evidence_requirements: dict[str, Any]
    provider_requirements: dict[str, Any]
    load_reasons: list[str] = field(default_factory=list)


def load_bundle(root: Path) -> tuple[PolicyBundle | None, list[str]]:
    """Return (bundle, reasons). Any reason means the bundle is unusable;
    the caller Blocks. Reasons are drawn from the ratified block-condition
    vocabulary only."""
    if not root.is_dir():
        return None, ["policy_missing"]

    docs: dict[str, Any] = {}
    for name in BUNDLE_FILES:
        path = root / name
        if not path.is_file():
            return None, ["policy_missing"]
        try:
            docs[name.removesuffix(".yaml")] = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return None, ["policy_missing"]

    schema_path = root / BUNDLE_SCHEMA
    if not schema_path.is_file():
        return None, ["policy_missing"]
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            e.message for e in Draft202012Validator(schema).iter_errors(docs)
        )
    except Exception:
        # Unusable schema (bad JSON, invalid metaschema, resolver failure):
        # the bundle cannot attest anything — fail closed as missing policy.
        return None, ["policy_missing"]
    if errors:
        return None, ["policy_missing"]

    versions = {docs[k]["policy_version"] for k in docs}
    if len(versions) != 1:
        return None, ["policy_version_unsupported"]
    policy_version = docs["approval-policy"]["policy_version"]
    major = int(policy_version.split(".", 1)[0])
    if major != SUPPORTED_POLICY_MAJOR:
        return None, ["policy_version_unsupported"]

    return (
        PolicyBundle(
            root=root,
            policy_version=policy_version,
            approval_policy=docs["approval-policy"],
            protected_paths=docs["protected-paths"],
            evidence_requirements=docs["evidence-requirements"],
            provider_requirements=docs["provider-requirements"],
        ),
        [],
    )
