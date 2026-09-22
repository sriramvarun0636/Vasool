"""What advances time in a deployment, and what stops two workers colliding.

`PolicyMachine.observe` reacts to a webhook; `PolicyMachine.tick` does
everything that is due *later* — a retry scheduled for the next salary window,
a deferred proposal whose guard may now allow it, an escalation whose horizon
has passed. In the simulator `windtunnel/runner.py` calls `tick` as it advances
virtual time. In production nothing called it at all, which meant the half of
the agent that waits had no runtime: every schedule the taxonomy registers was
exercised only in the wind tunnel.

**Ownership, not locking.** `IdempotencyGuard` asks whether a key is in
`ctx.facts.executed_keys` and then acts on the answer. Check-then-act is
correct today because exactly one worker exists, and it stops being correct the
moment a second one runs. The answer registered on 2026-09-17 is ownership:
this driver takes only the episodes whose `identity_id` hashes into its own
share, so two workers never hold one human's episodes and the race is
unreachable rather than guarded against. `vasool/policy/partition.py` is the
whole of that decision; this module just obeys it.

**One writer, one machine, still.** The store underneath is SQLite
(docs/EVALUATION.md §10, 2026-09-21), so running two of these against one
database is not supported today — the partition is what makes the *shape*
right, so that moving the store to Postgres is a row lock rather than a
redesign. A driver started with `workers > 1` says so rather than implying a
concurrency it does not have.

**Nothing here decides anything.** The driver chooses *when* the machine is
asked and *which* episodes it is asked about. What happens then is the policy
plane's, exactly as in the simulator — which is why the same `tick` runs in
both and there is no second copy of the schedule to drift.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from vasool.policy.partition import owns
from vasool.runtime.composition import Runtime

log = logging.getLogger("vasool.driver")

DEFAULT_INTERVAL_SECONDS = 60.0
"""How often due work is looked for. A minute because the finest schedule the
taxonomy registers is five minutes (`docs/taxonomy.md` §6's first retry rung),
so a minute cannot be the reason an action is late, and a shorter loop would
spend a deployment's IO budget asking a question whose answer changes at most
every five."""


@dataclass
class Driver:
    """Advances the agent's scheduled work, for the episodes this worker owns.

    `worker` and `workers` are the partition: worker *i* of *n*. The defaults
    are the honest ones for the store underneath — one worker, everything.
    """

    runtime: Runtime
    worker: int = 0
    workers: int = 1
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    ticks: int = field(default=0, init=False)
    """How many times the loop has run. Read by the ops endpoint and by tests;
    a driver whose tick count stops moving is the symptom INC-002 would have
    shown early — a system that is up, and doing nothing."""

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError("a partition needs at least one worker")
        if not 0 <= self.worker < self.workers:
            raise ValueError(f"worker {self.worker} is not one of {self.workers}")
        if self.workers > 1:
            log.warning(
                "driver started as worker %d of %d against a SQLite store, which "
                "supports one writer: the partition is correct but the store is not "
                "shared. See docs/EVALUATION.md §10, 2026-09-21.",
                self.worker,
                self.workers,
            )

    def owns_episode(self, identity_id: str | None) -> bool:
        """Whether this worker is the one that may act on this human.

        An episode with no identity — nothing has resolved one yet — is owned
        by worker 0. Dropping it would be worse: an unowned episode is one
        nobody advances, which is the silent-stall failure this repository has
        already had once (`POSTMORTEM.md` INC-002).
        """
        if identity_id is None:
            return self.worker == 0
        return owns(self.worker, identity_id, workers=self.workers)

    def tick_once(self) -> int:
        """One pass. Returns how many due items this worker gated.

        The share is expressed as a predicate handed to `PolicyMachine.tick`
        rather than as a filtered list built here: what is due is the
        machine's own schedule, and a driver that re-derived it would be a
        second copy of `docs/taxonomy.md` §6 free to drift from the first.
        """
        self.ticks += 1
        return self.runtime.machine.tick(owned=self._owns_customer)

    def _owns_customer(self, customer_id: str) -> bool:
        """The predicate `tick` gates with. Keyed on the customer id the
        proposal carries; a deployment that resolves identities maps that to a
        human first, which is why `owns_episode` takes the resolved id."""
        return self.owns_episode(customer_id)

    def run_forever(self, *, sleep=time.sleep, until=None) -> int:
        """Loop until `until()` says stop. Returns the number of passes made.

        `sleep` and `until` are arguments so a test can drive this loop without
        waiting a minute per pass and without a thread — the same reason the
        clock is injected everywhere else in this system.
        """
        passes = 0
        while until is None or not until():
            self.tick_once()
            passes += 1
            if until is None:
                sleep(self.interval_seconds)
            elif not until():
                sleep(self.interval_seconds)
        return passes
