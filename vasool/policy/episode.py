"""The recovery episode: the thing the state machine actually operates on.

The design spec has no aggregate. Its FSM diagram implies the state belongs to
an event, and it cannot: a single recovery spans several `payment.failed`
webhooks — one per attempt — and every counter that matters is a property of the
sequence rather than of any one message.

  - `attempts_used` has to be the count Razorpay counts, which is consecutive
    failures against the mandate, not failures reported in a webhook.
  - `contacts_sent` is capped per *episode* (two), which is not a quantity an
    event can hold.
  - Consent withdrawal has to purge everything in flight for a customer (A12),
    which needs something to enumerate.

So: one episode per entity, opened by the first failure and closed by a terminal
state.

Kept deliberately small this session — an in-memory store behind a protocol.
Durable episodes belong with the ledger in stage 5, and building a schema now
would be building it twice.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from vasool.events.schemas import FailureEvent


class State(StrEnum):
    """Where an episode is. Closed.

    Two corrections to the spec's diagram are baked in here.

    **SCHEDULED exists.** The spec runs DIAGNOSED -> GATED -> EXECUTING with no
    state for "diagnosed, waiting for its time", which would put a 48-hour timed
    retry's compliance check two days before its money movement. Gating happens
    immediately before execution instead, and this is where an action waits.

    **DEFERRED is a resting state, not a transition.** The spec's ASCII art
    draws the deferral arrow into AWAITING while its label says "re-enter at
    GATED"; neither is right. A deferred action goes back on the queue and is
    gated again when it comes due — but the episode stays in DEFERRED while it
    waits, rather than sliding back into SCHEDULED. Waiting because §4 chose a
    time and waiting because a guard moved the action are different situations,
    "how much is currently held by compliance" is a number the report card
    needs, and collapsing the two makes it unrecoverable from the episode alone.

    **One caveat, honestly stated.** An episode can have two proposals in flight
    at once — the LIQUIDITY fan-out sends a nudge now and re-presents on payday
    — so a single state field cannot describe both. It holds the most recent
    transition. Per-proposal state is the right model and is not what this is;
    the transition log carries the per-proposal history in the meantime.
    """

    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    SCHEDULED = "SCHEDULED"
    GATED = "GATED"
    DEFERRED = "DEFERRED"
    EXECUTING = "EXECUTING"
    AWAITING = "AWAITING"

    BLOCKED = "BLOCKED"
    ESCALATED = "ESCALATED"
    RECOVERED = "RECOVERED"
    EXHAUSTED = "EXHAUSTED"


TERMINAL: frozenset[State] = frozenset(
    {State.BLOCKED, State.ESCALATED, State.RECOVERED, State.EXHAUSTED}
)
"""Once here, an episode absorbs everything. A further failure webhook for a
settled or abandoned payment must not reopen the chase."""


@dataclass(frozen=True, slots=True)
class Episode:
    """One recovery, from first failure to terminal state. Immutable; the store
    replaces it rather than mutating, which keeps every version of it a value
    the ledger could record."""

    entity_id: str
    customer_id: str
    merchant_id: str
    state: State
    opened_at: datetime
    attempts_used: int = 0
    """Instrument re-presentations executed. Counted across the episode, because
    that is what Razorpay counts before it halts a subscription."""

    contacts_sent: int = 0
    executed_keys: frozenset[str] = frozenset()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL


class EpisodeStore(Protocol):
    def get(self, entity_id: str) -> Episode | None: ...
    def put(self, episode: Episode) -> None: ...
    def for_customer(self, customer_id: str) -> tuple[Episode, ...]: ...


class InMemoryEpisodeStore:
    """Session 3's store. Durable episodes land with the ledger in stage 5."""

    def __init__(self) -> None:
        self._by_entity: dict[str, Episode] = {}

    def get(self, entity_id: str) -> Episode | None:
        return self._by_entity.get(entity_id)

    def put(self, episode: Episode) -> None:
        self._by_entity[episode.entity_id] = episode

    def for_customer(self, customer_id: str) -> tuple[Episode, ...]:
        return tuple(e for e in self._by_entity.values() if e.customer_id == customer_id)

    def open(self, event: FailureEvent, *, now: datetime) -> Episode:
        existing = self.get(event.entity_id)
        if existing is not None:
            return existing
        episode = Episode(
            entity_id=event.entity_id,
            customer_id=event.customer_id,
            merchant_id=event.merchant_id,
            state=State.DETECTED,
            opened_at=now,
        )
        self.put(episode)
        return episode

    def advance(self, episode: Episode, state: State, **changes) -> Episode:
        updated = replace(episode, state=state, **changes)
        self.put(updated)
        return updated
