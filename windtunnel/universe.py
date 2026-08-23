"""500 customers and the failures they generate, from one seed.

EVALUATION.md §3d registers the customer count and the failure-reason mix and
stops there. Everything else about the world's shape — how many episodes a
customer has, when they arrive, how large they are, who consented, who is on a
mandate — is registered under §10 and lives in windtunnel/parameters.py, with
its reasoning attached. Nothing in this module picks a number.

**Why the customer is the unit.** §3a: episodes from one customer share a
payment instrument, a consent record, a DND status, a contact history and a
frequency-cap budget, and `FrequencyCapGuard` and `PromiseToPayGuard`
explicitly couple them. So a customer is built once, with their episodes
hanging off them, and §3b's stratification key — the failure class of their
*first* episode — is a property of that whole object.

**Every event comes off disk.** `windtunnel/payloads.py` stamps identity onto
a real captured envelope; nothing here writes an error string. That matters
more than it looks: every file in `data/stubbed_payloads/` carries the same
`payment.entity.id`, contact and amount, so a universe built from unmodified
fixtures would be 500 copies of one customer with one episode between them.

**No wall clock.** Every instant is computed from the universe epoch, which is
a constant. tests/test_no_wallclock.py already scans this package.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import FailureClass, lookup
from vasool.events.schemas import FailureEvent, derive_customer_id
from vasool.policy.facts import CONSENT_PURPOSE_RECOVERY, ConsentRecord
from windtunnel import payloads
from windtunnel.outcome import OutcomeModel
from windtunnel.parameters import (
    PAYMENT_FAILED_SOURCE_MIX,
    REASON_MIX,
    WORLD_PARAMETERS,
    Parameter,
)
from windtunnel.rng import bernoulli, choose, exponential, integer, lognormal, poisson, uniform

CUSTOMER_COUNT = 500
"""§3d, registered: "500 customers, seeded"."""

EPOCH = datetime(2026, 9, 1, 0, 0, tzinfo=IST).astimezone(timezone.utc)
"""When every universe starts.

Fixed rather than swept, and it is the one §10 entry that is not a magnitude:
scaling a calendar anchor by ±50% is meaningless. What it has to be is a date
whose following two months exercise both halves of taxonomy §6's salary window
— two month-ends and two 1st-7th windows — which any epoch at a month boundary
satisfies. The arrival window's *length* is swept in its place.

Anchored just after the payloads in data/ were captured (2026-08-21) so the
simulated calendar does not predate the envelopes it stamps.
"""

MAX_EPISODES_PER_CUSTOMER = 6
"""Truncation on the Poisson tail. Bounds the run without meaningfully
reshaping the distribution: at the registered lambda of 1.0 fewer than one
customer in 2000 is affected."""

CONSENT_AGE = timedelta(days=365)
"""How long before the epoch consent was granted.

A constant rather than a distribution, and deliberately not registered as a
swept parameter: ConsentGuard has no expiry rule — `ConsentRecord.covers`
only asks whether the action is after the grant and inside the purpose — so
any age at all behaves identically. Registering it would add a knob whose
sweep is guaranteed to show nothing, which is worse than not having it.
"""


@dataclass(frozen=True, slots=True)
class Customer:
    """One person, and every fact about them the guards can read."""

    index: int
    contact: str
    email: str
    customer_id: str
    """The pseudonymous id vasool/events/schemas.py derives. Held here so the
    simulator can key its own bookkeeping the same way the agent does, rather
    than keeping a second identity of its own."""

    is_mandate: bool
    dnd_listed: bool
    consent: ConsentRecord | None
    consent_withdrawn_at: datetime | None
    """When this customer withdraws, if they do. The withdrawal is already on
    the ConsentRecord, so ConsentGuard blocks from that instant with no help
    from the runner; the runner additionally tells the machine, because a
    withdrawal has to purge work already queued (A12) and not merely stop new
    work being allowed."""


@dataclass(frozen=True, slots=True)
class PlannedEpisode:
    """One failure, and everything the world has already decided about it."""

    entity_id: str
    customer: Customer
    reason: str
    source: str
    amount_paise: int
    arrives_at: datetime
    promise_to_pay: date | None
    out_of_band_at: datetime | None
    """When this customer pays through some other channel, if they do.

    Decided here, before any arm runs, precisely because it must not depend on
    what the agent did — see OutcomeModel.out_of_band_at.
    """

    event: FailureEvent

    @property
    def failure_class(self) -> FailureClass:
        """§3b's stratification key when this is a customer's first episode."""
        return lookup(self.reason, self.source)[1].failure_class


@dataclass(frozen=True, slots=True)
class Universe:
    """One seeded world. Immutable: the runner mutates its own bookkeeping,
    never this."""

    seed: int
    epoch: datetime
    horizon: datetime
    customers: tuple[Customer, ...]
    episodes: tuple[PlannedEpisode, ...]
    """Every episode in the universe, ascending by arrival. Sorted here so
    that the runner never has to sort, and so two runs cannot differ by a
    tie-break."""

    def stratum_of(self, customer: Customer) -> FailureClass | None:
        """§3b: a customer's stratum is their first episode's failure class.
        None for a customer whose episodes all fell past the arrival window."""
        for episode in self.episodes:
            if episode.customer.index == customer.index:
                return episode.failure_class
        return None


def _digits(seed: int, *coordinates: object) -> str:
    """Ten digits for a mobile number, with the leading one fixed to 9.

    docs/VERIFIED.md: Payment Links reject a contact with repeated digits
    (`+919999999999`). Nothing in windtunnel/ ever calls Razorpay, so the
    constraint cannot bite here — but generating numbers a real Payment Link
    would refuse means the universe could not be replayed against test mode by
    anyone who wanted to check it, so the run avoids them.
    """
    body = "".join(str(integer(0, 9, seed, *coordinates, i)) for i in range(9))
    if len(set(body)) == 1:
        body = body[:-1] + str((int(body[-1]) + 1) % 10)
    return "9" + body


def _entity_id(seed: int, customer_index: int, episode_index: int) -> str:
    """A Razorpay-shaped payment id, unique per episode.

    Derived rather than counted so it does not depend on generation order, and
    long enough that §2a's "every receipt id unique across the run" is not
    resting on luck — receipt ids are keyed on entity_id
    (vasool/ledger/receipts.py::_receipt_id).
    """
    basis = f"{seed}|{customer_index}|{episode_index}"
    return "pay_" + hashlib.sha256(basis.encode()).hexdigest()[:14]


def _customer(
    seed: int, index: int, parameters: dict[str, Parameter], horizon: datetime, pepper: str
) -> Customer:
    def value(name: str) -> float:
        return parameters[name].value

    contact = "+91" + _digits(seed, "customer", index, "contact")
    email = f"customer{index}@simulated.invalid"

    has_consent = bernoulli(value("consent_on_file_rate"), seed, "customer", index, "consent")
    withdraws = has_consent and bernoulli(
        value("consent_withdrawn_rate"), seed, "customer", index, "withdraws"
    )
    withdrawn_at = None
    if withdraws:
        span = (horizon - EPOCH).total_seconds()
        offset = uniform(0.0, span, seed, "customer", index, "withdrawn_at")
        withdrawn_at = EPOCH + timedelta(seconds=offset)

    return Customer(
        index=index,
        contact=contact,
        email=email,
        customer_id=derive_customer_id(contact, email, pepper=pepper),
        is_mandate=bernoulli(value("mandate_share"), seed, "customer", index, "mandate"),
        dnd_listed=bernoulli(value("dnd_listed_rate"), seed, "customer", index, "dnd"),
        consent=(
            ConsentRecord(
                granted_at=EPOCH - CONSENT_AGE,
                purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
                withdrawn_at=withdrawn_at,
            )
            if has_consent
            else None
        ),
        consent_withdrawn_at=withdrawn_at,
    )


def _reason_and_source(seed: int, entity_id: str) -> tuple[str, str]:
    """§3d's registered mix, drawn in two stages.

    The generic reason branches on source because taxonomy §3 says it is the
    only reason that carries signal there — and §3d's 70/25/5 split is what
    makes one registered reason exercise three different failure classes.
    Every other reason takes the source its own payload on disk carries, which
    is why this returns a pair rather than picking a source independently.
    """
    reason = choose(REASON_MIX, seed, entity_id, "reason")
    if reason == "payment_failed":
        return reason, choose(PAYMENT_FAILED_SOURCE_MIX, seed, entity_id, "source")
    source = next(s for r, s in payloads.available_pairs() if r == reason)
    return reason, source


def build_universe(
    seed: int,
    *,
    pepper: str,
    outcome: OutcomeModel,
    parameters: dict[str, Parameter] = WORLD_PARAMETERS,
) -> Universe:
    """One world, from one seed. Pure: no clock, no I/O beyond reading the
    payload envelopes off disk.

    `outcome` is required because out-of-band settlement is a fact about the
    world rather than about any arm's behaviour, so it has to be decided here,
    once, and seen identically by every arm (EVALUATION.md §5).

    `pepper` is passed in rather than read from the process environment,
    which nothing in this package ever reads: a secret's value belongs to the
    caller, and a test must be able to build a universe without one being
    configured. tests/windtunnel/test_runner.py scans for it.
    """

    def value(name: str) -> float:
        return parameters[name].value

    arrival_window = timedelta(days=value("episode_arrival_window_days"))
    horizon = EPOCH + arrival_window + timedelta(days=value("settlement_drain_days"))
    oob_horizon_days = int((horizon - EPOCH).days) + 1

    customers: list[Customer] = []
    episodes: list[PlannedEpisode] = []

    for index in range(CUSTOMER_COUNT):
        customer = _customer(seed, index, parameters, horizon, pepper)
        customers.append(customer)

        count = 1 + min(
            poisson(value("episodes_per_customer_lambda"), seed, "customer", index, "episodes"),
            MAX_EPISODES_PER_CUSTOMER - 1,
        )
        arrival = EPOCH + timedelta(
            seconds=uniform(0.0, arrival_window.total_seconds(), seed, "customer", index, "first_arrival")
        )

        for episode_index in range(count):
            if arrival >= EPOCH + arrival_window:
                # An episode whose spacing pushed it past the arrival window
                # is dropped rather than clamped to the boundary. Clamping
                # would pile every long-spaced customer's later episodes onto
                # the last instant of the window, which is a spike the world
                # has no reason to contain.
                break

            entity_id = _entity_id(seed, index, episode_index)
            reason, source = _reason_and_source(seed, entity_id)
            amount_rupees = lognormal(
                value("amount_median_rupees"), value("amount_sigma_log"), seed, entity_id, "amount"
            )
            amount_paise = max(100, int(round(amount_rupees)) * 100)

            promise = None
            if bernoulli(value("promise_to_pay_rate"), seed, entity_id, "promise"):
                days = integer(1, int(value("promise_horizon_days")), seed, entity_id, "promise_day")
                promise = (arrival + timedelta(days=days)).astimezone(IST).date()

            episodes.append(
                PlannedEpisode(
                    entity_id=entity_id,
                    customer=customer,
                    reason=reason,
                    source=source,
                    amount_paise=amount_paise,
                    arrives_at=arrival,
                    promise_to_pay=promise,
                    out_of_band_at=outcome.out_of_band_at(
                        entity_id, arrived_at=arrival, horizon_days=oob_horizon_days
                    ),
                    event=payloads.failure_event(
                        reason=reason,
                        source=source,
                        entity_id=entity_id,
                        contact=customer.contact,
                        email=customer.email,
                        amount_paise=amount_paise,
                        occurred_at=arrival,
                        pepper=pepper,
                    ),
                )
            )

            arrival = arrival + timedelta(
                days=exponential(
                    value("inter_episode_gap_mean_days"), seed, entity_id, "next_episode_gap"
                )
            )

    episodes.sort(key=lambda e: (e.arrives_at, e.entity_id))
    return Universe(
        seed=seed,
        epoch=EPOCH,
        horizon=horizon,
        customers=tuple(customers),
        episodes=tuple(episodes),
    )
