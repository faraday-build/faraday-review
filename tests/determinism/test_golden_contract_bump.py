"""T25: the v0.4.0 re-vendor changed only `contract_version` in the goldens.

The golden inputs are v0.3.0 evidence and stay so. The only output that
moves with the pin is the Validator's own `contract_version`;
`evaluated_contract_minor` still records the evidence minor (3). Any other
change to a golden would be a behavior change the contract bump must not
cause.
"""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / "golden"


def test_goldens_carry_v040_and_still_evaluate_minor_3():
    cases = sorted(GOLDEN.iterdir())
    assert len(cases) == 13
    for case in cases:
        decision = json.loads((case / "expected-decision.json").read_text(encoding="utf-8"))
        evidence = json.loads((case / "evidence.json").read_text(encoding="utf-8"))
        assert decision["contract_version"] == "v0.4.0", case.name
        assert evidence["contract_version"] == "v0.3.0", case.name
        assert decision["evaluated_contract_minor"] == 3, case.name
