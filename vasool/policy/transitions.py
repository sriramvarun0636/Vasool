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
from typing import Protocol

from vasool.diagnosis.proposal import Proposal
from vasool.policy.episode import State
from vasool.policy.verdict import ChainResult, Verdict


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
