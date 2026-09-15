"""The port a TRAI DND scrub arrives through, and its null adapter.

No scrub is built. A real one needs a DLT registration and operator access this
project does not have, and simulating one would be the first fabricated
capability in the repository. What exists is the seam, written before any real
adapter, with a null adapter that is safe rather than permissive: a registry
that cannot answer says *cannot tell*, and cannot-tell leaves `dnd_listed`
None — which `DNDGuard`, through the guard base's fail-closed rule, blocks.
"Unknown, therefore blocked", never "unknown, therefore fine"
(docs/EVALUATION.md §10, 2026-09-15).

The simulator does not use this: its universe draws each customer's registry
status itself (`windtunnel/universe.py`, `dnd_listed_rate`) and states it as a
fresh scrub, which is the one thing a simulation can honestly say about a
registry it also invented.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = ["DNDFacts", "DNDRegistry", "NullDNDRegistry", "dnd_facts"]


class DNDRegistry(Protocol):
    def is_listed(self, contact: str) -> bool | None:
        """True or False when the registry answered; None when it could not."""
        ...


class NullDNDRegistry:
    """Answers None for everyone. Wired where no scrub exists, so that every
    contact under DNDGuard's jurisdiction is blocked until one does."""

    def is_listed(self, contact: str) -> bool | None:
        return None


@dataclass(frozen=True, slots=True)
class DNDFacts:
    dnd_listed: bool | None
    dnd_checked_at: datetime | None


def dnd_facts(registry: DNDRegistry, contact: str, now: datetime) -> DNDFacts:
    """The two facts DNDGuard requires, from one registry lookup.

    A registry that could not answer yields no check time as well as no
    answer: a timestamp beside a missing answer would read as a scrub that
    happened, and a failed call must not be indistinguishable from a clean one.
    """
    listed = registry.is_listed(contact)
    return DNDFacts(dnd_listed=listed, dnd_checked_at=now if listed is not None else None)
