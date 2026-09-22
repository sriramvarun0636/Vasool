"""The only module in this system allowed to read the environment.

Everything else takes what it needs as an argument. That rule is not style: a
module that reads `os.environ` is a module whose behaviour depends on a machine
nobody can see from the code, and this repository has already paid for that
once — `tools/evaluate.py` existed to keep `windtunnel/` away from the pepper,
and §10's row of 2026-09-14 records what happened when the pepper came from an
author's `.env` (every published figure was a function of a value on one
laptop). The composition root is where configuration stops being ambient and
becomes an argument.

**What it refuses to start without.** `VASOOL_ID_PEPPER` keys the HMAC that
makes a `customer_id`, which is the join key for the ledger, the contact
history and the identity resolver. A process that boots with the wrong pepper
writes receipts nobody can join to anything, and a process that boots with the
*public test* pepper writes them under a value published in this repository.
Both are refused here, loudly, at startup — not at the first webhook, by which
time something is already in the ledger.

**What it does not do.** It does not run anything. Building the system and
driving it are separate so that a test can build the whole thing, hold it, and
step it by hand (`vasool/runtime/driver.py`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vasool.clock import RealClock
from vasool.events.store import EventStore
from vasool.policy.facts import MerchantPolicy
from vasool.policy.machine import PolicyMachine
from vasool.actions.retry_store import SqlRetryIndex
from vasool.policy.sql_store import SqlFactStore

PEPPER_VAR = "VASOOL_ID_PEPPER"

PUBLIC_TEST_PEPPER = "test-pepper-do-not-use-in-prod"
"""The value the test suite uses, published in this repository. A deployment
that starts under it derives the same `customer_id` as every reader's laptop,
so the pseudonymisation is not pseudonymisation."""

MIN_PEPPER_LENGTH = 32
"""Short enough to type by accident is short enough to guess. The check is a
floor on carelessness, not a cryptographic claim."""


class StartupRefused(RuntimeError):
    """Configuration the process will not start with.

    Raised before anything is built, so a refused start leaves no database
    file, no connection and no half-wired machine behind.
    """


@dataclass(frozen=True, slots=True)
class Runtime:
    """Everything a deployment needs, wired and held together.

    Frozen because a running system that can be re-pointed at another store
    mid-flight is a system whose ledger spans two worlds.
    """

    machine: PolicyMachine
    facts: SqlFactStore
    events: EventStore
    merchant: MerchantPolicy
    pepper: str
    retries: SqlRetryIndex | None = None
    """The durable retry index the executor, the receiver and reconciliation
    share. None only for a runtime built without an executor — a dry run."""

    def close(self) -> None:
        self.facts.close()
        if self.retries is not None:
            self.retries.close()


def read_pepper(environ: dict[str, str] | None = None) -> str:
    """The pepper, or a refusal that says which of the three problems it is.

    Takes the environment as an argument so the check itself is testable
    without setting a variable in the test process — which would leak into
    every other test in the same run.
    """
    env = os.environ if environ is None else environ
    pepper = (env.get(PEPPER_VAR) or "").strip()
    if not pepper:
        raise StartupRefused(
            f"{PEPPER_VAR} is not set. It keys the HMAC behind every customer_id, "
            "so a process without it writes a ledger that joins to nothing. Set it "
            "from your secret store; it is never read anywhere else in this system."
        )
    if pepper == PUBLIC_TEST_PEPPER:
        raise StartupRefused(
            f"{PEPPER_VAR} is the public test pepper, which is printed in this "
            "repository's own test suite. Every customer_id derived under it is "
            "reproducible by anyone who cloned the code, so it pseudonymises nothing."
        )
    if len(pepper) < MIN_PEPPER_LENGTH:
        raise StartupRefused(
            f"{PEPPER_VAR} is {len(pepper)} characters; {MIN_PEPPER_LENGTH} is the "
            "floor. This is a check against a placeholder being left in, not a "
            "strength claim."
        )
    return pepper


def build(
    *,
    db_path: str | Path,
    events_path: str | Path,
    merchant: MerchantPolicy,
    environ: dict[str, str] | None = None,
    executor=None,
    retries: SqlRetryIndex | None = None,
) -> Runtime:
    """Wire the system. Reads the environment exactly once, here.

    `executor` is an argument rather than a default because dispatching money
    is the one thing that must never be wired by accident: a caller that does
    not pass one gets a machine that can propose, gate and record but cannot
    execute, which is the safe shape for a dry run.
    """
    pepper = read_pepper(environ)
    # One retry index, or none. The executor writes it, the receiver reads it
    # to recognise a capture as ours, and reconciliation subtracts it from what
    # the rail reports; three components holding two indexes would put the
    # restart gap back between them (docs/EVALUATION.md §10, 2026-09-22).
    if executor is not None and retries is None:
        raise StartupRefused(
            "an executor was passed without a durable retry index. Build the executor "
            "with retry_index=SqlRetryIndex(path) and pass the same object as retries, "
            "or a restart will lose which payments this agent made."
        )
    if executor is not None and getattr(executor, "retry_index", None) is not retries:
        raise StartupRefused(
            "the executor and the runtime hold different retry indexes; pass the one "
            "the executor was built with, so that what it writes is what is read."
        )
    events = EventStore(events_path)
    # No resolver is wired, and that is the registered default: the cap counts
    # one record as one human, which is what it counted before
    # `vasool/identity/` existed. A deployment that wants the cap to count
    # people passes its own customer_id -> identity_id mapping here, which is a
    # deliberate change to the unit a safety rule counts and belongs in that
    # deployment's configuration rather than in a default
    # (docs/EVALUATION.md §10, 2026-09-17).
    facts = SqlFactStore(db_path, merchant=merchant)
    machine = PolicyMachine(
        clock=RealClock(),
        facts=facts,
        executor=executor,
        **({"ours": retries.payment_ids} if retries is not None else {}),
    )
    return Runtime(
        machine=machine, facts=facts, events=events, merchant=merchant, pepper=pepper,
        retries=retries,
    )
