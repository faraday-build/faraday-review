import os
from pathlib import Path

import pytest

from faraday_review.canonical import canonical_bytes, read_canonical

GOLDEN = Path(__file__).parent / "golden"
VOLATILE_FIELDS = frozenset({"evaluated_at"})


def _strip_volatile(decision: dict) -> dict:
    return {k: v for k, v in decision.items() if k not in VOLATILE_FIELDS}


def test_volatile_list_stays_minimal():
    """The exclusion list must not become a drain for real nondeterminism."""
    assert VOLATILE_FIELDS == {"evaluated_at"}


@pytest.mark.parametrize("case", sorted(p.name for p in GOLDEN.iterdir() if p.is_dir()))
def test_golden_decision_matches(case: str, evaluate):
    root = GOLDEN / case
    expected = read_canonical(root / "expected-decision.json")

    first = evaluate(root)
    second = evaluate(root)

    # same-process repeat: catches mutable state and memoization carryover
    assert canonical_bytes(_strip_volatile(first)) == canonical_bytes(_strip_volatile(second))
    assert canonical_bytes(_strip_volatile(first)) == canonical_bytes(_strip_volatile(expected))


def test_hash_seed_is_applied():
    """Fails loudly if the harness is run without seed variation."""
    assert os.environ.get("PYTHONHASHSEED") is not None, (
        "run via: for s in 0 1 7 19 43 101; do PYTHONHASHSEED=$s pytest tests/determinism; done"
    )
