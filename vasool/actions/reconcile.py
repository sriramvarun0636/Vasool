"""Did the money already arrive by some other route?

Attack A01, open since the suite was written: a customer pays through a channel
this agent cannot see — another card on the merchant's checkout, a bank
transfer, the shop counter — and the `payment.captured` it produces carries no
`vasool_entity_id` and no id any `RetryIndex` knows. `settle_from_webhook`
correctly declines to attribute it (docs/taxonomy.md §9.10), so the episode
stays open and the agent goes on chasing money the merchant already has. That
is the expensive direction to be wrong in: the harm is a second collection from
somebody who has already paid.

**Measured, before this existed** (docs/EVALUATION.md §10, 2026-09-21): an
out-of-band payment lands on **108,599 of Vasool's 354,788 evaluated
episodes**, and Vasool takes **34,666 actions after the money had arrived**.

**What this does, and the line it will not cross.** It asks the rail what it
captured for this customer since the failure, discards every payment id the
agent itself originated, and reports what is left. A remaining payment whose
amount matches the episode's, inside the window, is **evidence that the episode
should stop** — and nothing more. It is not a settlement. The episode is halted
and handed to a person; it is never marked RECOVERED, and no receipt here says
money was recovered.

**Why the asymmetry decides it.** An amount match from the same customer in the
same window is not a join key: a second purchase at the same price collides
with it exactly. Acting on that as if it were proof would put "this episode was
paid" in a ledger whose whole claim is that every figure is derivable from
evidence. The two ways to be wrong are not the same size — continuing to chase
paid money risks collecting it twice, while stopping and asking a human costs
at most a delayed recovery — so the rule takes the cheap error and refuses the
expensive one. It is the same argument `vasool/identity/resolver.py` makes for
joining only on exact matches, and the same one `NullStatusCheck` makes when
the rail cannot say.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

DEFAULT_WINDOW = timedelta(days=14)
"""How far back a candidate payment may sit and still belong to this failure.

Long enough to cover the whole of `docs/taxonomy.md` §6's ladder — three
salary-aware rungs plus an escalation link, roughly forty days at its longest,
but two weeks covers the span in which a customer who has just seen a failed
charge pays it another way. Short enough that an unrelated purchase a month
later is not offered as evidence. Registered as a guess, and the direction of
error is stated: a wider window halts more episodes that were not paid, a
narrower one keeps chasing more that were.
"""

AMOUNT_TOLERANCE_PAISE = 0
"""Exact, in paise. A tolerance would let a differently-priced purchase stand
in as evidence for this one, and the whole design here is that the evidence is
weak enough already."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A payment on this customer's account that this agent did not make."""

    payment_id: str
    amount_paise: int
    captured_at: datetime

    def matches(self, *, amount_paise: int, since: datetime, now: datetime) -> bool:
        return (
            abs(self.amount_paise - amount_paise) <= AMOUNT_TOLERANCE_PAISE
            and since <= self.captured_at <= now
        )


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the lookup found, and what the machine should do about it."""

    candidate: Candidate | None
    detail: str

    @property
    def should_halt(self) -> bool:
        """Whether the episode must stop. Never "whether it was recovered"."""
        return self.candidate is not None


class SettlementLookup(Protocol):
    """What the rail says this customer paid, whoever collected it."""

    def captured_since(self, *, customer_id: str, since: datetime) -> tuple[Candidate, ...]: ...


class NullSettlementLookup:
    """No lookup is wired, so nothing is known and nothing changes.

    The same shape as `NullStatusCheck` and `NullIdentityResolver`: production
    behaves exactly as it did before this module existed until a deployment
    wires a real lookup, and A01 stays open for anyone who does not. That is
    deliberate — reconciliation reads a merchant's whole payment stream, which
    is a decision a merchant takes rather than a default they inherit.
    """

    def captured_since(self, *, customer_id: str, since: datetime) -> tuple[Candidate, ...]:
        return ()


def reconcile(
    lookup: SettlementLookup,
    *,
    customer_id: str,
    amount_paise: int,
    failed_at: datetime,
    now: datetime,
    ours: frozenset[str],
    window: timedelta = DEFAULT_WINDOW,
) -> Reconciliation:
    """Ask whether this episode's money arrived by another route.

    `ours` is every payment id this agent originated, from the executor's own
    `RetryIndex`. Excluding it is what stops the agent's own successful retry
    being read back as an out-of-band payment — which would halt the episode
    that had just succeeded, and report the reason wrongly.
    """
    since = failed_at - window
    for candidate in lookup.captured_since(customer_id=customer_id, since=since):
        if candidate.payment_id in ours:
            continue
        if candidate.matches(amount_paise=amount_paise, since=since, now=now):
            return Reconciliation(
                candidate=candidate,
                detail=(
                    f"payment {candidate.payment_id} for the same amount was captured on "
                    f"this customer's account at {candidate.captured_at.isoformat()} and "
                    "was not made by this agent. The money may already have arrived, so "
                    "this episode stops and a person decides; it is not recorded as "
                    "recovered, because an amount match is evidence and not a join key"
                ),
            )
    return Reconciliation(
        candidate=None,
        detail="no payment on this customer's account matches this episode",
    )
