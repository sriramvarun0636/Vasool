"""What ownership buys, and what its absence costs.

`IdempotencyGuard` asks whether a key is in `ctx.facts.executed_keys` and then
acts on the answer. Check-then-act is correct today because exactly one worker
exists; `EventStore.append` needs no such assumption, which makes the guard the
weaker of the two idempotency layers (docs/EVALUATION.md §10, 2026-09-17).

The registered answer is ownership: a worker owns a hash range of `identity_id`,
so two workers never hold one human's episodes and the race is unreachable
rather than guarded against. These tests demonstrate both halves — that two
workers sharing a human execute the same key twice, and that the partition
makes that arrangement impossible.

**Deliberately not threaded.** A thread test would interleave two machines and
sometimes catch the race, which makes a flaky test out of a property that is
not about timing at all: what is claimed is that an owned key reaches one
worker. That is decidable, so it is decided here rather than sampled.
"""
from __future__ import annotations

from datetime import datetime, timezone

from vasool.clock import VirtualClock
from vasool.diagnosis.rules import IST
from vasool.policy.machine import PolicyMachine
from vasool.policy.partition import owner_of, owns
from tests.payloads import event_for
from tests.policy.strategies import permissive_facts
from tests.test_machine import RecordingExecutor

NOON = datetime(2026, 8, 25, 12, 0, tzinfo=IST).astimezone(timezone.utc)
WORKERS = 4


class OneHumansFacts:
    """Every episode here belongs to one human, whatever the payment id says.

    Which is the case the partition exists for: two records of one person must
    be gated by one worker, or the frequency cap is read from two half
    histories and A07 comes back wearing a concurrency hat.
    """

    def __init__(self, identity: str) -> None:
        self.identity = identity

    def snapshot(self, *, event, proposal, now):
        return permissive_facts(identity_id=self.identity)


def _event(entity_id: str):
    """One captured failure, restamped so each episode is its own."""
    event = event_for("card_expired")
    return event.model_copy(update={"entity_id": entity_id, "event_id": f"evt_{entity_id}"})


def _worker(identity: str) -> tuple[PolicyMachine, RecordingExecutor]:
    executor = RecordingExecutor()
    clock = VirtualClock(NOON)
    return PolicyMachine(clock=clock, facts=OneHumansFacts(identity), executor=executor), executor


def _keys(executor: RecordingExecutor) -> list[str]:
    return [p.idempotency_key for p in executor.executed]


class TestWithoutOwnership:
    def test_two_workers_that_share_a_human_execute_the_same_key_twice(self):
        """The race the guard cannot close on its own, made deterministic: each
        worker's `executed_keys` holds only what *it* executed, so each one
        checks, finds nothing, and acts."""
        identity = "the-same-human"
        event = _event("pay_shared")

        first, first_exec = _worker(identity)
        second, second_exec = _worker(identity)
        for machine in (first, second):
            machine.observe(event)
            machine.tick()

        assert _keys(first_exec), "the first worker executed something"
        assert _keys(first_exec) == _keys(second_exec)
        assert len(_keys(first_exec) + _keys(second_exec)) == 2 * len(_keys(first_exec))


class TestWithOwnership:
    def test_one_human_reaches_exactly_one_worker(self):
        identity = "the-same-human"
        event = _event("pay_owned")

        executed: list[str] = []
        for worker in range(WORKERS):
            if not owns(worker, identity, workers=WORKERS):
                continue
            machine, executor = _worker(identity)
            machine.observe(event)
            machine.tick()
            executed += _keys(executor)

        assert len(executed) == len(set(executed)) == 1

    def test_every_key_is_executed_once_across_a_population(self):
        """The registered claim, over many humans and many episodes at once:
        partition, run each worker over what it owns, and count."""
        humans = [f"human-{i:03d}" for i in range(40)]
        executed: list[str] = []
        seen_by: dict[str, set[int]] = {}

        for worker in range(WORKERS):
            for index, identity in enumerate(humans):
                if not owns(worker, identity, workers=WORKERS):
                    continue
                seen_by.setdefault(identity, set()).add(worker)
                machine, executor = _worker(identity)
                machine.observe(_event(f"pay_p{index}"))
                machine.tick()
                executed += _keys(executor)

        assert len(executed) == len(set(executed)), "a key executed twice"
        assert all(len(workers) == 1 for workers in seen_by.values())
        assert set().union(*seen_by.values()) == set(range(WORKERS)), "every worker did some work"

    def test_ownership_is_by_human_and_not_by_payment(self):
        """Over a population of split identities: keying on the human always
        gives one owner, keying on the payment identifier does not.

        A single pair would prove nothing either way — two ids land on the same
        worker one time in four by luck — so this asks the question of forty
        humans. Keying on the id would scatter some of them across workers,
        each reading the frequency cap from a fraction of that person's
        history, which is A07 arriving through the door the partition was
        supposed to close.
        """
        from vasool.events.schemas import derive_customer_id
        from vasool.identity.resolver import UnionFindResolver

        pepper = "test-pepper-do-not-use-in-prod"
        resolver = UnionFindResolver(pepper)
        pairs = []
        for i in range(40):
            contact = f"+9198765{i:05d}"
            records = [(contact, f"a{i}@x.invalid"), (contact, f"b{i}@x.invalid")]
            for record in records:
                resolver.add(*record)
            pairs.append(records)

        split_by_id = 0
        for records in pairs:
            ids = {derive_customer_id(c, e, pepper=pepper) for c, e in records}
            assert len(ids) == 2, "two payment identifiers for one human"
            by_identity = {owner_of(resolver.identity_for(c, e), workers=WORKERS) for c, e in records}
            assert len(by_identity) == 1, "one human, one worker"
            if len({owner_of(i, workers=WORKERS) for i in ids}) > 1:
                split_by_id += 1

        assert split_by_id > 0, "keying on the payment identifier would scatter nobody?"
