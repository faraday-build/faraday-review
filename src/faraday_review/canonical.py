"""Canonical serialization for digests and golden-file comparison.

Load-bearing in two places:

1. Ruling 12 — the policy digest is computed over this form, so that
   formatting-only changes to a bundle do not produce false mismatches.
2. The determinism harness — decisions are compared as canonical bytes,
   never as Python objects. Object equality hides ordering defects that
   reach disk.

No network. No clock. No config.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CANONICAL_ENCODING = "utf-8"


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic UTF-8 bytes for any JSON-representable object.

    - keys sorted at every depth
    - no insignificant whitespace
    - LF only, single trailing newline
    - non-ASCII preserved rather than escaped, so the form is stable
      across producers that differ on ensure_ascii
    """
    text = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return (text + "\n").encode(CANONICAL_ENCODING)


def digest(obj: Any) -> str:
    """sha256 of the canonical form, prefixed with the algorithm."""
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def bundle_digest(bundle_root: Path) -> str:
    """Digest of a policy bundle directory.

    Computed over a sorted manifest of {relative_path: file_digest} so that
    file ordering, mtimes, and directory traversal order cannot affect the
    result. Relative paths use forward slashes on every platform.
    """
    manifest: dict[str, str] = {}
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_root).as_posix()
        raw = path.read_bytes()
        # Normalize line endings so CRLF checkouts do not shift the digest.
        normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        manifest[rel] = "sha256:" + hashlib.sha256(normalized).hexdigest()
    return digest({"bundle": manifest})


def write_canonical(path: Path, obj: Any) -> None:
    path.write_bytes(canonical_bytes(obj))


def read_canonical(path: Path) -> Any:
    return json.loads(path.read_bytes().decode(CANONICAL_ENCODING))
