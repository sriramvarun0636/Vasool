"""What a guard returns, and how thirteen of them resolve into one decision.

The design spec models a ruling as `allowed: bool` plus an optional
`defer_until`. That cannot express what the thirteen guards actually do:
`AFAThresholdGuard` and `HumanApprovalGuard` neither permit nor forbid — they
hand the action to a human — and a guard with nothing to say about a proposal
(DLT template rules, on a silent retry that sends nothing) is making a different
claim from one that examined the action and passed it. Collapsing those into a
boolean loses exactly the distinctions the report card exists to print.

So: a closed Decision enum, ordered by severity, and a resolution rule that runs
every guard rather than stopping at the first refusal.

**Why evaluate all thirteen.** The spec orders the chain cheap-first "so a
blocked action short-circuits before you spend an API call". No guard spends an
API call — they are dictionary lookups over a snapshot materialised in one pass
— so the saving is imaginary, while the cost is real: the spec's own ordering
puts three blocking guards after four deferring ones, which schedules actions
that were never going to be allowed. Running all of them makes registry order a
presentation detail, lets a receipt name every violated clause instead of
whichever guard happened to run first, and turns order-independence into a
property test (tests/test_registry.py).
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, model_validator


class Decision(StrEnum):
    """A guard's ruling. Closed.

    Ordered least to most severe; SEVERITY below is the authority on the
    ordering, and every member must appear in it.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """This guard has no jurisdiction over this proposal.

    Deliberately distinct from ALLOW. "The DLT template rule does not apply to a
    silent retry" and "the DLT template rule examined this message and passed
    it" are different compliance claims, and a report card that renders both as
    a green tick is overstating what was checked.
    """

    ALLOW = "ALLOW"
    """Examined and permitted."""

    DEFER = "DEFER"
    """Not now, but at a named instant. Requires defer_until — see below."""

    ESCALATE = "ESCALATE"
    """Not for an automated system to decide. Hand to a human.

    More severe than DEFER because an action heading for a human queue should
    never first be scheduled for automated execution.
    """

    BLOCK = "BLOCK"
    """Never, for this proposal. Terminal."""


SEVERITY: dict[Decision, int] = {
    Decision.NOT_APPLICABLE: 0,
    Decision.ALLOW: 1,
    Decision.DEFER: 2,
    Decision.ESCALATE: 3,
    Decision.BLOCK: 4,
}
"""Resolution order when thirteen guards disagree. The chain's decision is the
most severe verdict any guard returned."""


def resolve(decisions: Iterable[Decision]) -> Decision:
    """The most severe decision in the chain, or ALLOW if the chain is empty."""
    return max(decisions, key=lambda d: SEVERITY[d], default=Decision.ALLOW)


class ObligationKind(StrEnum):
    """Things a guard can require the state machine to do. Closed, for the same
    reason InterventionType is closed: a guard must not be able to invent an
    action at the call site."""

    SEND_PRE_DEBIT_NOTICE = "SEND_PRE_DEBIT_NOTICE"
    """RBI e-mandate: the customer must be notified 24h before a mandate debit.
    The notice is itself a customer contact, so the machine turns this into a
    Proposal that goes through the guard chain in its own right — a notice
    generated at 03:00 does not get to skip the contact window."""


class Obligation(BaseModel):
    """Something that must happen before the deferred action can proceed.

    Inert. A guard is a pure function and cannot send a notice; it says one is
    owed and the machine acts. Exactly the discipline the diagnosis plane
    follows one layer up — describe the action, never perform it.
    """

    kind: ObligationKind
    not_before: datetime
    reason: str

    model_config = {"frozen": True}


class Verdict(BaseModel):
    """One guard's ruling on one proposal."""

    guard: str
    decision: Decision
    reason: str | None = None
    """Why. Lands verbatim in the receipt, so it is required for anything that
    isn't a pass."""

    statute: str | None = None
    """The clause enforced — "RBI Fair Practices Code ¶55". Printed in the
    report card. None for self-imposed rules, which must not be dressed up as
    regulation."""

    defer_until: datetime | None = None
    """When the blocking condition expires. Set if and only if DEFER."""

    obligations: tuple[Obligation, ...] = ()

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _check_defer_coupling(self) -> Self:
        """DEFER if and only if defer_until.

        This is the whole anti-forever-deferral argument, enforced structurally.
        A guard may defer only when the condition it is enforcing is a function
        of time guaranteed to change *and* it can name the instant — the daily
        spend cap resets at midnight, the contact window opens at 08:00, the
        oldest contact ages out of the seven-day window on a computable date. A
        guard that cannot name that instant is enforcing a condition that may
        never expire, and must block instead.

        Getting this wrong is not a cosmetic error: a defer with no expiry is an
        action that is rescheduled forever and never executed or refused, which
        looks like a working system and recovers nothing.
        """
        if self.decision is Decision.DEFER and self.defer_until is None:
            raise ValueError(
                "DEFER requires defer_until — a guard that cannot name when the "
                "condition expires must BLOCK instead"
            )
        if self.decision is not Decision.DEFER and self.defer_until is not None:
            raise ValueError(f"defer_until is meaningless on {self.decision}")
        if self.defer_until is not None and self.defer_until.tzinfo is None:
            raise ValueError("defer_until must be timezone-aware")
        return self

    @model_validator(mode="after")
    def _check_reason_present(self) -> Self:
        if self.decision not in (Decision.ALLOW, Decision.NOT_APPLICABLE) and not self.reason:
            raise ValueError(f"{self.decision} requires a reason — it goes in the receipt")
        return self


class ChainResult(BaseModel):
    """The whole chain's ruling: one decision, and every verdict behind it."""

    decision: Decision
    defer_until: datetime | None
    verdicts: tuple[Verdict, ...]
    obligations: tuple[Obligation, ...]

    model_config = {"frozen": True}

    @classmethod
    def of(cls, verdicts: Sequence[Verdict]) -> ChainResult:
        """Resolve a full set of verdicts.

        `defer_until` is the *latest* of the deferrals, and only survives when
        DEFER is the chain's decision. Latest, because satisfying the earliest
        would wake the action into a guard that is still going to defer it —
        burning deferral budget to make no progress. And dropped on a block,
        because an action that is going to be refused must never be scheduled;
        that is precisely the failure the spec's guard ordering produces.
        """
        decision = resolve(v.decision for v in verdicts)
        defer_until = (
            max(v.defer_until for v in verdicts if v.defer_until is not None)
            if decision is Decision.DEFER
            else None
        )
        return cls(
            decision=decision,
            defer_until=defer_until,
            verdicts=tuple(verdicts),
            obligations=tuple(ob for v in verdicts for ob in v.obligations),
        )

    def deciding(self) -> tuple[Verdict, ...]:
        """The verdicts that produced the chain's decision."""
        return tuple(v for v in self.verdicts if v.decision is self.decision)

    def blocking(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.decision is Decision.BLOCK)

    def statutes(self) -> tuple[str, ...]:
        """Every clause actually violated, in chain order, de-duplicated.

        Only non-passing verdicts contribute: a guard that permitted an action
        did not apply its statute to it, and listing it would overstate the
        enforcement in the one artefact a compliance reader will scrutinise.
        """
        seen: dict[str, None] = {}
        for v in self.verdicts:
            if v.decision in (Decision.ALLOW, Decision.NOT_APPLICABLE):
                continue
            if v.statute:
                seen.setdefault(v.statute)
        return tuple(seen)
