"""Which worker owns an episode, and why that is what makes the guard safe.

`IdempotencyGuard` asks `if key in ctx.facts.executed_keys` and then acts on
the answer. That is check-then-act, and it is correct today for one reason
only: exactly one worker exists. `EventStore.append` has no such dependency —
it inserts and catches the integrity error, so the check and the decision are
one atomic operation — which makes the guard the weaker of the system's two
idempotency layers (docs/EVALUATION.md §10, 2026-09-17).

The answer registered is ownership rather than locking: a worker owns a hash
range of `identity_id`, every episode of one human is gated by one worker, and
two workers therefore never hold the same idempotency key at the same instant.
No lock is taken and none is needed, which is the point — a lock would be a
second thing to get right under failure, and the partition is a property of
the key.

`identity_id` rather than `customer_id` because the frequency cap counts
humans: two records of one person must land on the same worker or the cap is
read from two half-histories, and the bug A07 named would come back wearing a
concurrency hat.
"""
from __future__ import annotations

import hashlib

__all__ = ["owner_of", "owns"]


def owner_of(identity_id: str, *, workers: int) -> int:
    """Which of `workers` workers gates this human's episodes.

    A hash of the id rather than its bytes: `identity_id` is already an HMAC
    digest, so any slice of it is uniform, but slicing would tie ownership to
    the digest's encoding. Hashing again costs nothing and survives a change
    of key derivation.
    """
    if workers < 1:
        raise ValueError("a partition needs at least one worker")
    digest = hashlib.sha256(identity_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") % workers


def owns(worker: int, identity_id: str, *, workers: int) -> bool:
    """Whether this worker should gate this human at all."""
    return owner_of(identity_id, workers=workers) == worker
