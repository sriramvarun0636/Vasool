"""MandateMachine: the transitions a mandate may take, and nothing else.

No I/O, no clock — `at` is passed in — and no knowledge of debits. It answers
one question: from this record, may this happen, and what does it leave? A
debit is not a transition; whether one may be presented is the policy plane's
question (vasool/policy/guards/mandate_state.py), answered from the record this
machine produces.

**Every transition cites the clause that permits it.** The table below is the
whole lifecycle, and each row names clauses quoted in citations.py. A
precondition — a card mandate cannot be paused, a non-revocable loan mandate
cannot be revoked by its payer — is a refusal with its own citation, so a
rejected transition says which rule rejected it rather than only that it was
rejected.

**What is not here, deliberately.** Porting a mandate between UPI apps (NPCI
OC-223 §1(iii)-(v)) changes where it is managed and nothing about whether it
may be debited, so it is not a transition. Nor is a debit's outcome: a failed
execution leaves an ACTIVE mandate ACTIVE, and one declined because the
mandate was revoked is evidence of a REVOKED transition that already happened
elsewhere, reported through whichever channel reports it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from vasool.mandate.citations import cite
from vasool.mandate.record import MandateRail, MandateRecord
from vasool.mandate.states import TERMINAL, MandateState


class Trigger(StrEnum):
    """Everything that can happen to a mandate. Closed."""

    REGISTRATION_REQUESTED = "registration_requested"
    AUTHENTICATED = "authenticated"
    REGISTRATION_LAPSED = "registration_lapsed"
    REGISTRATION_DECLINED = "registration_declined"
    MODIFIED = "modified"
    PAUSED_BY_PAYER = "paused_by_payer"
    UNPAUSED_BY_PAYER = "unpaused_by_payer"
    PAUSE_ENDED = "pause_ended"
    REVOKED_BY_PAYER = "revoked_by_payer"
    REVOKED_BY_PAYEE = "revoked_by_payee"
    VALIDITY_ENDED = "validity_ended"
    RAIL_REPORTED_REVOKED = "rail_reported_revoked"
    RAIL_REPORTED_PAUSED = "rail_reported_paused"
    RAIL_REPORTED_EXPIRED = "rail_reported_expired"
    """A debit failed and the rail's reason says what state the mandate is in.
    These name no actor, because the reason names none reliably, and carry no
    precondition, because the rail is the authority on whether a debit can
    happen: where the record disagrees, the record moves and the disagreement
    is kept (vasool/mandate/evidence.py)."""


@dataclass(frozen=True, slots=True)
class Condition:
    """A precondition on a transition, and the rule that imposes it."""

    holds: Callable[[MandateRecord, datetime], bool]
    refusal: str
    citation: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Transition:
    frm: MandateState
    to: MandateState
    trigger: Trigger
    citation: tuple[str, ...]
    """REQUIRED — the clauses permitting this, as keys into citations.CLAUSES.
    `cite()` refuses an empty or unresolvable one when the table is built."""

    conditions: tuple[Condition, ...] = ()

    def __post_init__(self) -> None:
        if not self.citation:
            raise ValueError(f"{self.trigger}: a transition must cite the clause permitting it")
        if self.frm in TERMINAL:
            raise ValueError(f"{self.trigger}: nothing leaves the terminal state {self.frm}")


class IllegalTransition(ValueError):
    """A transition the table does not permit, or whose precondition fails.

    The message names the rule, because "not allowed" tells a caller nothing it
    can check.
    """


_UPI_ONLY = Condition(
    holds=lambda record, _at: record.rail is MandateRail.UPI_AUTOPAY,
    refusal=(
        "pause is a UPI Autopay operation: the sources gathered describe no pause of "
        "a card e-mandate"
    ),
    citation=cite("NPCI-OC-223 §1(i)", "NPCI-OC-182 §3.1"),
)

_PAYER_MAY = Condition(
    holds=lambda record, _at: record.revocable_by_payer,
    refusal=(
        "created non-revocable: the payer's app offers neither revoke nor pause, and "
        "revocation goes through the merchant, with its consent"
    ),
    citation=cite("NPCI-OC-125A ¶2", "NPCI-OC-125A ¶3", "NPCI-OC-125A ¶4"),
)

_VALIDITY_OVER = Condition(
    holds=lambda record, at: at >= record.valid_until,
    refusal="the validity period has not ended",
    citation=cite("RBI-EMF-2026 §4(b)"),
)

_PAUSE_OVER = Condition(
    holds=lambda record, at: record.paused_until is not None and at >= record.paused_until,
    refusal="the pause has no end date, or it has not been reached",
    citation=cite("RAZORPAY-TPAP-PAUSE request"),
)

_REVOKED_BY_PAYER = cite(
    "RBI-EMF-2026 §4(b)", "RBI-EMF-2026 §4(e)", "RBI-EMF-2026 §6(c)", "NPCI-UPI-CODES-2.9 §3.1 VA"
)
_REVOKED_BY_PAYEE = cite("NPCI-OC-182 §3.2", "NPCI-OC-125A ¶4", "NPCI-UPI-CODES-2.9 §3.1 VA")
_EXPIRED = cite("RBI-EMF-2026 §4(b)", "NPCI-UPI-CODES-2.9 §3.1 VU")
_UNPAUSED = cite("RAZORPAY-TPAP-PAUSE request")
"""The one citation resting on platform documentation: no rule document found
says how a paused mandate resumes (docs/EVALUATION.md §10, 2026-09-15)."""

_RAIL_REVOKED = cite("RAZORPAY-UPI-SUBSEQUENT mandate_cancelled", "NPCI-UPI-CODES-2.9 §3.1 VA")
_RAIL_PAUSED = cite("RAZORPAY-UPI-SUBSEQUENT mandate_paused", "NPCI-UPI-CODES-2.9 §3.1 VT")
_RAIL_EXPIRED = cite("RAZORPAY-UPI-SUBSEQUENT mandate_expired", "NPCI-UPI-CODES-2.9 §3.1 VU")
"""Razorpay's reason is the evidence a merchant receives; NPCI's code is what
the state means on the rail. Neither alone: the first is a platform's
description, the second never reaches a Razorpay merchant."""

_S, _T = MandateState, Trigger

TRANSITIONS: tuple[Transition, ...] = (
    Transition(_S.CREATED, _S.PENDING_AUTH, _T.REGISTRATION_REQUESTED, cite("RBI-EMF-2026 §4(a)")),
    Transition(
        _S.PENDING_AUTH,
        _S.ACTIVE,
        _T.AUTHENTICATED,
        cite("RBI-EMF-2026 §4(a)", "RBI-EMF-2026 §5(a)"),
    ),
    Transition(
        _S.PENDING_AUTH,
        _S.EXPIRED,
        _T.REGISTRATION_LAPSED,
        cite("RBI-EMF-2026 §4(a)", "NPCI-UPI-CODES-2.9 §4.4 U69"),
    ),
    Transition(_S.PENDING_AUTH, _S.REVOKED, _T.REGISTRATION_DECLINED, cite("RBI-EMF-2026 §4(a)")),
    Transition(
        _S.ACTIVE,
        _S.ACTIVE,
        _T.MODIFIED,
        cite("RBI-EMF-2026 §4(b)", "RBI-EMF-2026 §4(c)", "RBI-EMF-2026 §4(e)"),
    ),
    Transition(
        _S.ACTIVE,
        _S.PAUSED,
        _T.PAUSED_BY_PAYER,
        cite(
            "NPCI-OC-223 §1(i)",
            "NPCI-OC-223 §1(ii)",
            "NPCI-OC-182 §3.1",
            "NPCI-UPI-CODES-2.9 §3.1 VT",
        ),
        conditions=(_UPI_ONLY, _PAYER_MAY),
    ),
    Transition(_S.PAUSED, _S.ACTIVE, _T.UNPAUSED_BY_PAYER, _UNPAUSED),
    Transition(_S.PAUSED, _S.ACTIVE, _T.PAUSE_ENDED, _UNPAUSED, conditions=(_PAUSE_OVER,)),
    Transition(_S.ACTIVE, _S.REVOKED, _T.REVOKED_BY_PAYER, _REVOKED_BY_PAYER, conditions=(_PAYER_MAY,)),
    Transition(_S.PAUSED, _S.REVOKED, _T.REVOKED_BY_PAYER, _REVOKED_BY_PAYER, conditions=(_PAYER_MAY,)),
    Transition(_S.ACTIVE, _S.REVOKED, _T.REVOKED_BY_PAYEE, _REVOKED_BY_PAYEE),
    Transition(_S.PAUSED, _S.REVOKED, _T.REVOKED_BY_PAYEE, _REVOKED_BY_PAYEE),
    Transition(_S.ACTIVE, _S.EXPIRED, _T.VALIDITY_ENDED, _EXPIRED, conditions=(_VALIDITY_OVER,)),
    Transition(_S.PAUSED, _S.EXPIRED, _T.VALIDITY_ENDED, _EXPIRED, conditions=(_VALIDITY_OVER,)),
    # -- the rail's evidence, from a failed debit. No conditions: see Trigger.
    Transition(_S.ACTIVE, _S.REVOKED, _T.RAIL_REPORTED_REVOKED, _RAIL_REVOKED),
    Transition(_S.PAUSED, _S.REVOKED, _T.RAIL_REPORTED_REVOKED, _RAIL_REVOKED),
    Transition(_S.ACTIVE, _S.PAUSED, _T.RAIL_REPORTED_PAUSED, _RAIL_PAUSED),
    Transition(_S.ACTIVE, _S.EXPIRED, _T.RAIL_REPORTED_EXPIRED, _RAIL_EXPIRED),
    Transition(_S.PAUSED, _S.EXPIRED, _T.RAIL_REPORTED_EXPIRED, _RAIL_EXPIRED),
)

_CHANGES: dict[Trigger, frozenset[str]] = {
    Trigger.MODIFIED: frozenset({"valid_until"}),
    Trigger.PAUSED_BY_PAYER: frozenset({"paused_until"}),
}
"""The fields a transition may set. §4(b) lets the customer change the validity
period; a pause may carry an end. Nothing else changes except the state."""


class MandateMachine:
    def __init__(self, transitions: tuple[Transition, ...] = TRANSITIONS) -> None:
        self._table: dict[tuple[MandateState, Trigger], Transition] = {}
        for transition in transitions:
            key = (transition.frm, transition.trigger)
            if key in self._table:
                raise ValueError(f"two transitions for {transition.trigger} from {transition.frm}")
            self._table[key] = transition

    def permitted(self, state: MandateState) -> tuple[Transition, ...]:
        """Every transition the table allows out of `state`."""
        return tuple(t for (frm, _), t in self._table.items() if frm is state)

    def apply(
        self, record: MandateRecord, trigger: Trigger, *, at: datetime, **changes: object
    ) -> tuple[MandateRecord, Transition]:
        """Take `trigger` from `record`'s state, or refuse and say why.

        Returns the new record and the transition taken, so a caller can log
        the citation beside the change.
        """
        transition = self._table.get((record.state, trigger))
        if transition is None:
            raise IllegalTransition(f"{trigger} is not a transition out of {record.state}")
        for condition in transition.conditions:
            if not condition.holds(record, at):
                raise IllegalTransition(
                    f"{trigger} refused: {condition.refusal} ({'; '.join(condition.citation)})"
                )
        unexpected = set(changes) - _CHANGES.get(trigger, frozenset())
        if unexpected:
            raise IllegalTransition(f"{trigger} cannot change {sorted(unexpected)}")

        if transition.to is not MandateState.PAUSED:
            changes.setdefault("paused_until", None)
        return replace(record, state=transition.to, **changes), transition
