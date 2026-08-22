"""Rebuild state from the ledger.

Two things are provably replayable from what transitions.py and receipts.py
actually record:

1. **Episode state trajectory.** Every Transition already carries
   from_state / to_state / entity_id / at (vasool/policy/transitions.py), so
   folding the log in order reproduces each episode's state at every point in
   its history, including its terminal state, without touching an
   EpisodeStore or re-running the machine.

2. **Receipt-chain integrity.** receipts.py hashes every field of a Receipt
   except itself, chained on prev_hash; recomputing every hash from its own
   fields and checking the links is exactly what a tamper on any single
   receipt breaks (vasool/ledger/receipts.py::verify_chain).

**What this module cannot do, and why.** The session brief's ambition for
this file is that "the chain is already a pure function of (facts, proposal,
effective_at), so replay should be able to re-derive every compliance
decision from the recorded facts digest without touching a store." That is
true of vasool/policy/registry.py::evaluate_all in isolation — but it is not
achievable with what the ledger actually records this session: a Transition
carries the ChainResult a guard chain *produced*
(vasool/policy/transitions.py), not the PolicyFacts snapshot that produced it
(vasool/policy/facts.py::GuardContext). Re-deriving a verdict from facts
would need either that snapshot or a digest of it attached to every
Transition, and vasool/policy/machine.py — the one file this session was told
not to touch — is the only thing that ever sees a GuardContext.

So: this module verifies that the recorded decisions happened and chain
together untampered. It cannot yet prove they were the *correct* decisions
from first principles. Closing that gap means teaching machine.py to log a
facts digest per Transition, which belongs to whichever session next touches
the policy plane, not to this one.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from vasool.ledger.receipts import Receipt, verify_chain
from vasool.policy.episode import State
from vasool.policy.transitions import Transition


class TamperDetected(Exception):
    """A receipt's recomputed hash, or a prev_hash link, doesn't match what
    the ledger recorded."""


@dataclass(frozen=True, slots=True)
class EpisodeReplay:
    entity_id: str
    trajectory: tuple[State, ...]
    final_state: State


def replay_episodes(transitions: Iterable[Transition]) -> dict[str, EpisodeReplay]:
    """Fold the transition log into each episode's state trajectory, purely
    from what's recorded — no store, no re-diagnosis. (machine.py's own
    docstring already makes the re-diagnosis argument: classify() is a pure
    function of inputs that provably have not changed while an action
    waited, so re-running it on replay would only ever reproduce what it
    already produced.)
    """
    by_entity: dict[str, list[State]] = {}
    for t in transitions:
        by_entity.setdefault(t.entity_id, []).append(t.to_state)
    return {
        entity_id: EpisodeReplay(entity_id=entity_id, trajectory=tuple(states), final_state=states[-1])
        for entity_id, states in by_entity.items()
    }


def replay_receipts(receipts: Iterable[Receipt]) -> tuple[Receipt, ...]:
    """Verify the hash chain and hand back the receipts if it holds. Raises
    TamperDetected the instant a single hash or link fails to recompute —
    see receipts.py::verify_chain for what "fails" means."""
    ordered = tuple(receipts)
    if not verify_chain(ordered):
        raise TamperDetected(
            "receipt hash chain does not verify — either a receipt was "
            "edited after being written, or the chain was reordered"
        )
    return ordered
