#!/usr/bin/env python3
"""Deterministic authorization-boundary guards for the Faraday Review Plane.

Exit 1 on any finding. No network, no model, no config outside this file.

Usage:
    python tools/guards/authorization_boundary.py src/
    python tools/guards/authorization_boundary.py src/ --format github
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# Fields that constitute an authorization decision. Only the governance
# package may produce or mutate them, and only from Validator output.
AUTH_FIELDS = frozenset(
    {
        "approval_authorized",
        "merge_authorized",
        "founder_review_required",
        "risk_class",
        "policy_digest",
        "policy_version",
        "contract_version",
    }
)

PRODUCER_ALLOWLIST = ("faraday_review/governance/",)

CONSUMER_ROOTS = (
    "faraday_review/providers/",
    "faraday_review/review/",
    "faraday_review/evidence/",
    "faraday_review/github/",
    "faraday_review/telemetry/",
    "faraday_review/architecture/",
)

HTTP_CALLS = frozenset({"get", "post", "put", "patch", "delete", "request"})
HTTP_MODULES = frozenset({"requests", "httpx", "session", "client", "_client"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    severity: str
    message: str

    def as_text(self) -> str:
        return f"{self.path}:{self.line}: [{self.severity}] {self.rule} {self.message}"

    def as_github(self) -> str:
        level = "error" if self.severity == "P0" else "warning"
        return (
            f"::{level} file={self.path},line={self.line},"
            f"title={self.rule}::{self.message}"
        )


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Subscript):
        return _name_of(node.slice)
    return None


def _receiver_of(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return _name_of(node.func.value)
    return None


class GuardVisitor(ast.NodeVisitor):
    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.findings: list[Finding] = []
        self.is_producer = any(p in relpath for p in PRODUCER_ALLOWLIST)
        self.is_consumer = any(p in relpath for p in CONSUMER_ROOTS)

    def _add(self, node: ast.AST, rule: str, severity: str, message: str) -> None:
        self.findings.append(
            Finding(self.relpath, getattr(node, "lineno", 0), rule, severity, message)
        )

    def _check_auth_write(self, node: ast.AST, target: ast.AST) -> None:
        if not self.is_consumer or self.is_producer:
            return
        name = _name_of(target)
        if name in AUTH_FIELDS:
            self._add(
                node,
                "FR-P0-001",
                "P0",
                f"'{name}' assigned outside faraday_review/governance/. "
                "Providers and review passes emit findings only.",
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_auth_write(node, target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_auth_write(node, node.target)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            if key is not None:
                self._check_auth_write(node, key)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self._add(
                node,
                "FR-P0-002",
                "P0",
                "bare 'except:' — authorization paths must fail closed with an "
                "explicit reason recorded.",
            )
        elif all(isinstance(stmt, ast.Pass) for stmt in node.body):
            self._add(
                node,
                "FR-P0-002",
                "P0",
                "swallowed exception ('pass' body) — must set approval=False, "
                "merge=False, founder_review_required=True.",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = _name_of(node.func)

        if func_name == "get" and node.args:
            key = _name_of(node.args[0])
            if key in AUTH_FIELDS and len(node.args) > 1:
                default = node.args[1]
                if isinstance(default, ast.Constant) and default.value is True:
                    self._add(
                        node,
                        "FR-P0-003",
                        "P0",
                        f"'.get({key!r}, True)' defaults an authorization field "
                        "to permissive. Default must be False.",
                    )

        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                self._add(
                    node,
                    "FR-P0-004",
                    "P0",
                    "shell=True with PR-derived input in scope. Use an argv list.",
                )

        receiver = _receiver_of(node)
        if (
            func_name in HTTP_CALLS
            and receiver is not None
            and receiver.lower() in HTTP_MODULES
            and not any(kw.arg == "timeout" for kw in node.keywords)
        ):
            self._add(
                node,
                "FR-P1-005",
                "P1",
                f"'{receiver}.{func_name}(...)' has no timeout. Unbounded calls "
                "stall the review and defeat fail-closed timing.",
            )

        self.generic_visit(node)


def scan(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "node_modules", "build", "dist"} for part in path.parts):
            continue
        relpath = str(path).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            findings.append(
                Finding(relpath, exc.lineno or 0, "FR-P0-000", "P0", f"unparseable: {exc.msg}")
            )
            continue
        visitor = GuardVisitor(relpath)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return findings


SKIP_PARTS = {".venv", "node_modules", "build", "dist"}


def _tree_required(root: Path, override: bool) -> bool:
    """Arm enforcement once the package marker exists on disk."""
    return override or (root / "faraday_review" / "__init__.py").exists()


def _runtime_sources(root: Path) -> list[Path]:
    """Python sources excluding package markers.

    __init__.py is still SCANNED for violations; it merely does not count
    toward proving the tree is non-empty. A marker-only package must not
    attest as implemented.
    """
    return [
        path
        for path in root.rglob("*.py")
        if path.name != "__init__.py"
        and not any(part in SKIP_PARTS for part in path.parts)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory to scan")
    parser.add_argument("--format", choices=("text", "github"), default="text")
    parser.add_argument(
        "--require-tree",
        action="store_true",
        help="force non-empty-tree enforcement (auto-enabled once "
             "src/faraday_review/__init__.py exists)",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"authorization_boundary: root {args.root} missing", file=sys.stderr)
        return 1

    if _tree_required(args.root, args.require_tree) and not _runtime_sources(args.root):
        print(
            f"authorization_boundary: no runtime Python sources under {args.root}. "
            "Guard cannot attest a marker-only package.",
            file=sys.stderr,
        )
        return 1

    findings = scan(args.root)
    render = (lambda f: f.as_github()) if args.format == "github" else (lambda f: f.as_text())
    for finding in findings:
        print(render(finding), file=sys.stderr)

    p0 = sum(1 for f in findings if f.severity == "P0")
    print(f"authorization_boundary: {p0} P0, {len(findings) - p0} P1", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
