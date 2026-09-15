"""When a failed debit tells us what state the mandate is in.

A UPI Autopay debit that fails because the mandate was revoked, paused or has
expired says so in its reason — `mandate_cancelled`, `mandate_paused`,
`mandate_expired` (docs/taxonomy.md §12). That is the rail reporting a
transition that already happened elsewhere, and the record is moved to match,
through `MandateMachine`, citing Razorpay's reason and NPCI's code
(vasool/mandate/machine.py). Registered in docs/EVALUATION.md §10, 2026-09-15.

**The rail wins, and the disagreement is kept.** The record can say otherwise:
expiry reported before `valid_until`, cancellation "by user" on a mandate
created non-revocable (NPCI OC-125A), a pause on a card mandate or on one that
may not be paused. The rail is the authority on whether a debit can happen, so
the record moves anyway — a record that insisted a revoked mandate was live
would have every later debit refused by the rail and none refused by us — and
the disagreement is returned beside the transition, for whoever holds the
record to log as a finding. A record already in a terminal state does not move
(nothing leaves one); the report is kept as a finding if it names a different
state.

Pure: no clock, no store. The caller holds the record.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from vasool.mandate.machine import IllegalTransition, MandateMachine, Transition, Trigger
from vasool.mandate.record import MandateRail, MandateRecord
from vasool.mandate.states import TERMINAL, MandateState

__all__ = ["RAIL_EVIDENCE", "Observation", "apply_rail_evidence"]

RAIL_EVIDENCE: dict[str, tuple[Trigger, MandateState]] = {
    "mandate_cancelled": (Trigger.RAIL_REPORTED_REVOKED, MandateState.REVOKED),
    "mandate_paused": (Trigger.RAIL_REPORTED_PAUSED, MandateState.PAUSED),
    "mandate_expired": (Trigger.RAIL_REPORTED_EXPIRED, MandateState.EXPIRED),
}
"""Razorpay's UPI reasons that report a mandate's state. `mandate_not_active`
is not here: it says what the mandate is not, not what it is."""


@dataclass(frozen=True, slots=True)
class Observation:
    """What one failed debit did to a mandate record."""

    mandate_id: str
    at: datetime
    reason: str
    transition: Transition | None
    """Taken, or None when the record was already where the rail says, or
    could not leave a terminal state."""

    disagreement: str | None
    """What the record said that the rail contradicted, if anything."""


def _disagreement(record: MandateRecord, reported: MandateState, at: datetime) -> str | None:
    current = record.state_at(at)
    if current in TERMINAL and current is not reported:
        return f"the record says {current.value}, which nothing leaves, and the rail reports {reported.value}"
    if reported is MandateState.EXPIRED and at < record.valid_until:
        return f"the rail reports expiry before the record's valid_until, {record.valid_until.isoformat()}"
    if reported is MandateState.REVOKED and not record.revocable_by_payer:
        return "the rail reports cancellation by the user on a mandate created non-revocable (NPCI OC-125A)"
    if reported is MandateState.PAUSED and (record.rail is not MandateRail.UPI_AUTOPAY or not record.revocable_by_payer):
        return "the rail reports a pause on a mandate the sources say cannot be paused (NPCI OC-182, OC-125A)"
    return None


def apply_rail_evidence(
    record: MandateRecord, reason: str, *, at: datetime, machine: MandateMachine | None = None
) -> tuple[MandateRecord, Observation | None]:
    """Move `record` to the state a failed debit's `reason` reports.

    Returns the record — moved, or as it was — and an Observation, or None when
    the reason reports no mandate state at all, which is every reason but three.
    """
    evidence = RAIL_EVIDENCE.get(reason)
    if evidence is None:
        return record, None
    trigger, reported = evidence
    disagreement = _disagreement(record, reported, at)
    current = record.state_at(at)
    if current is reported or current in TERMINAL:
        return record, Observation(record.mandate_id, at, reason, None, disagreement)
    # A lapsed pause or an ended validity the record has not been told about
    # is applied first by `state_at`; the transition starts from there.
    moved_from = record if current is record.state else _at_state(record, current)
    try:
        updated, transition = (machine or MandateMachine()).apply(moved_from, trigger, at=at)
    except IllegalTransition as exc:
        return record, Observation(record.mandate_id, at, reason, None, f"{disagreement or ''} {exc}".strip())
    return updated, Observation(record.mandate_id, at, reason, transition, disagreement)


def _at_state(record: MandateRecord, state: MandateState) -> MandateRecord:
    return replace(record, state=state, paused_until=None if state is not MandateState.PAUSED else record.paused_until)
