"""Protected-path classification.

Gitignore-style globs against repo-relative POSIX paths, case-sensitive,
locale-independent. `**` crosses directory separators; `*` and `?` do not.
The PR's path-derived class is the maximum matched class; unmatched paths
take the policy default (yellow — unknown surface fails toward review).
"""

from __future__ import annotations

import re

from ..models import CLASS_ORDER, DeterministicFinding

_SEVERITY_BY_CLASS = {"green": "info", "yellow": "warning", "red": "warning"}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:[^/]+/)*")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


class PathClassifier:
    def __init__(self, classes: dict[str, list[str]], default_class: str) -> None:
        if default_class not in CLASS_ORDER:
            raise ValueError(f"unknown default_class {default_class!r}")
        self._default = default_class
        self._compiled: list[tuple[str, str, re.Pattern[str]]] = []
        # Deterministic iteration: sorted by class rank then pattern text.
        for cls in sorted(classes, key=lambda c: CLASS_ORDER[c]):
            for pattern in sorted(classes[cls]):
                self._compiled.append((cls, pattern, _glob_to_regex(pattern)))

    def classify_path(self, path: str) -> tuple[str, str | None]:
        """Return (class, matched_pattern). Highest matched class wins; the
        default class applies only when no pattern matches at all."""
        best: tuple[str, str] | None = None
        for cls, pattern, regex in self._compiled:
            if regex.match(path) and (best is None or CLASS_ORDER[cls] > CLASS_ORDER[best[0]]):
                best = (cls, pattern)
        return best if best is not None else (self._default, None)

    def classify(self, paths: list[str]) -> tuple[str, list[DeterministicFinding]]:
        """Classify a changed-path list; emit one finding per non-green path."""
        overall = "green"
        findings: list[DeterministicFinding] = []
        for path in sorted(paths):
            cls, pattern = self.classify_path(path)
            if CLASS_ORDER[cls] > CLASS_ORDER[overall]:
                overall = cls
            if cls == "green":
                continue
            findings.append(
                DeterministicFinding(
                    rule_id=pattern or "default-class",
                    check="protected_paths",
                    severity=_SEVERITY_BY_CLASS[cls],
                    path=path,
                    message=f"path classified {cls}"
                    + (f" by pattern {pattern!r}" if pattern else " by default (unmatched)"),
                )
            )
        return overall, findings
