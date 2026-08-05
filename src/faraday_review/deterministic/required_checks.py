"""Required-check verification over the observed check states in context.

The collector records every policy-required check context with its state at
collection time; 'missing' means no run exists for the head SHA. Anything
other than 'success' fails closed.
"""

from __future__ import annotations

from ..models import DeterministicFinding, ObservedCheck

FAILING_STATES = frozenset({"failure", "error"})
MISSING_STATES = frozenset({"missing", "pending"})


def evaluate_checks(observed: list[ObservedCheck]) -> list[DeterministicFinding]:
    findings: list[DeterministicFinding] = []
    for check in sorted(observed, key=lambda c: (c.context, c.state)):
        if check.state == "success":
            continue
        if check.state in FAILING_STATES:
            rule_id, msg = "required-check-failing", f"required check {check.context!r} is {check.state}"
        elif check.state in MISSING_STATES:
            rule_id, msg = "required-check-missing", (
                f"required check {check.context!r} has no passing run for this head "
                f"(state: {check.state})"
            )
        else:
            rule_id, msg = "required-check-missing", (
                f"required check {check.context!r} in unrecognized state {check.state!r}; failing closed"
            )
        findings.append(
            DeterministicFinding(
                rule_id=rule_id,
                check="required_checks",
                severity="blocking",
                rule_class=check.state,
                message=msg,
            )
        )
    return findings
