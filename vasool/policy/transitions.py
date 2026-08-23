"""The append-only record of everything the state machine decided.

Stage 5 wraps these into hash-chained Receipts. They are separate because a
Receipt is about a money action and a Transition is about a decision, and most
decisions here are decisions *not* to act — taxonomy.md §5 is explicit that the
audit trail has to show restraint, not only action, since on the RISK_BLOCK path
correct behaviour is otherwise indistinguishable from the agent being broken.

Append-only is enforced by omission, the same way vasool/events/store.py does
it: there is no update and no delete, and a test asserts that stays true.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from vasool.diagnosis.proposal import Proposal
from vasool.policy.episode import State
from vasool.policy.verdict import ChainResult, Verdict


class Closure(StrEnum):
    """Why an episode closed without anyone having proposed the closure.

    Closed, and it exists so the ledger never has to *infer* the cause from
    the shape of a transition. Most terminal transitions are rulings on a
    Proposal: the guard chain refused it, or escalated it, and the verdicts
    say why. These are the others — the episode was closed by a fact about
    the world, with no action gated and no chain run — and an
    empty-verdicts BLOCKED is otherwise indistinguishable from a guard chain
    that returned nothing, which is a bug.

    "Proposal-less BLOCKED means a withdrawal" happens to be a total rule
    today, and would be silently wrong the first day a fourth such path
    exists. Naming it here means a future one is added to a closed set on
    purpose rather than mislabelled at the call site — the same discipline
    vasool/ledger/receipts.py::Outcome already holds to, and it is what makes
    EVALUATION.md §2a's "no action after consent withdrawal" a ledger scan
    for a stated fact rather than a shape match.
    """

    CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
    """DPDP, and adversary attack A12 — see PolicyMachine.consent_withdrawn."""

    SETTLED = "SETTLED"
    """The money arrived or went back — A07 and A14. The one closure that
    also carries an amount (`settled_amount_paise`)."""

    CLOCK_SKEW = "CLOCK_SKEW"
    """A18: the event's timestamp was too far ahead to believe, so nothing
    was ever scheduled from it — see PolicyMachine.MAX_CLOCK_SKEW."""


@dataclass(frozen=True, slots=True)
class Transition:
    """One move, and everything needed to explain it later."""

    at: datetime
    entity_id: str
    from_state: State
    to_state: State
    note: str
    proposal: Proposal | None = None
    chain: ChainResult | None = None
    """Every verdict, not just the deciding one."""

    causes: tuple[Verdict, ...] = ()
    """The accumulated deferral history, on a transition that ends one."""

    customer_id: str | None = None
    """Who the episode belongs to. Set on every transition, from the episode
    itself.

    Carried because EVALUATION.md §2a's claims are per *customer* — "no
    action after consent withdrawal", "contacts per customer per 7 days ≤ 3"
    — and are scanned from a ledger that is otherwise per *entity*. A
    transition with a Proposal already carries it there, but the closures
    above do not, and an episode closed by a withdrawal with nothing ever
    gated is the common case rather than the edge one: without this there is
    nothing in the ledger connecting that withdrawal to the customer's other
    episodes.

    Optional only because a Transition can be constructed in a test without
    one; PolicyMachine._log always sets it.
    """

    closure: Closure | None = None
    """Why this transition closed the episode, when nothing proposed the
    closure. None on every transition that is a ruling on a Proposal —
    which is all of them except the three Closure names."""

    settled_amount_paise: int | None = None
    """Set only on a transition to State.RECOVERED — what
    PolicyMachine.settled() was told the customer actually paid. None on every
    other transition, including EXECUTING: dispatching a retry or a link is
    not confirmation money moved, only settled() is (see receipts.py's module
    docstring on why amount_recovered_paise lives here rather than on
    whatever proposal happened to be executing when the money arrived — often
    none, since an out-of-band payment can settle an episode nothing was ever
    proposed for yet)."""


class TransitionLog(Protocol):
    def append(self, transition: Transition) -> None: ...
    def __iter__(self): ...


@dataclass
class InMemoryTransitionLog:
    """Session 3's log. No update, no delete, by construction."""

    _entries: list[Transition] = field(default_factory=list)

    def append(self, transition: Transition) -> None:
        self._entries.append(transition)

    def __iter__(self):
        return iter(tuple(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, index: int) -> Transition:
        return self._entries[index]
