"""Hypothesis strategies for the policy plane.

Every Proposal here descends from a real captured payload — the pool is built by
running the real classifier over every (reason, source) pair on disk, so no test
in this package ever types an error string by hand (the project rules).

The facts are generated freely, which is the point of the snapshot design: a
PolicyFacts is a value, so hypothesis can produce thousands of adversarial
worlds without a database, a fake, or a single line of I/O.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hypothesis import strategies as st

from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import Proposal, proposals_from, template_ids
from vasool.diagnosis.rules import classify
from vasool.events.schemas import FailureEvent
from vasool.policy.facts import (
    CONSENT_PURPOSE_RECOVERY,
    ConsentRecord,
    GuardContext,
    MerchantPolicy,
    PolicyFacts,
)
from tests.payloads import one_event_per_pair

POOL_NOW = datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc)
"""Tue 25 Aug 2026, 10:00 IST. The instant tests/test_rules.py pins, and the
worst day of the month to retry an insufficient-funds failure."""

# Bounded so date arithmetic stays in a range a human can reason about, and so
# the salary-window search in rules.py is never handed a year it must scan.
MIN_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
MAX_TIME = datetime(2027, 12, 31, tzinfo=timezone.utc)

utc_datetimes = st.datetimes(
    min_value=MIN_TIME.replace(tzinfo=None),
    max_value=MAX_TIME.replace(tzinfo=None),
    timezones=st.just(timezone.utc),
)


def _pool() -> list[tuple[FailureEvent, Proposal]]:
    """Every proposal the real classifier emits, over every row on disk and
    every attempt in and past budget."""
    out: list[tuple[FailureEvent, Proposal]] = []
    for event in one_event_per_pair():
        for attempt in (1, 2, 3, 4, 5):
            diagnosis = classify(event, clock=VirtualClock(POOL_NOW), attempt=attempt)
            out.extend((event, p) for p in proposals_from(diagnosis, event, now=POOL_NOW))
    return out


POOL: list[tuple[FailureEvent, Proposal]] = _pool()

merchant_policies = st.builds(
    MerchantPolicy,
    merchant_id=st.just("acc_test"),
    daily_retry_cap_paise=st.integers(min_value=0, max_value=100_000_000),
    human_approval_threshold_paise=st.integers(min_value=0, max_value=100_000_000),
    kill_switch=st.booleans(),
)


@st.composite
def consent_records(draw, now: datetime) -> ConsentRecord:
    granted = draw(st.datetimes(
        min_value=MIN_TIME.replace(tzinfo=None),
        max_value=MAX_TIME.replace(tzinfo=None),
        timezones=st.just(timezone.utc),
    ))
    withdrawn = draw(
        st.one_of(
            st.none(),
            st.integers(min_value=-30, max_value=30).map(lambda d: now + timedelta(days=d)),
        )
    )
    purposes = draw(
        st.sets(st.sampled_from([CONSENT_PURPOSE_RECOVERY, "marketing", "analytics"]))
    )
    return ConsentRecord(
        granted_at=granted, purposes=frozenset(purposes), withdrawn_at=withdrawn
    )


@st.composite
def policy_facts(draw, now: datetime) -> PolicyFacts:
    """A world, generated around `now` so the time-relative facts are reachable
    rather than uniformly ancient."""
    near = st.integers(min_value=-14, max_value=14)
    return PolicyFacts(
        merchant=draw(merchant_policies),
        executed_keys=frozenset(draw(st.sets(st.text(min_size=1, max_size=8), max_size=4))),
        spent_today_paise=draw(st.integers(min_value=0, max_value=100_000_000)),
        attempts_used=draw(st.integers(min_value=0, max_value=6)),
        episode_contacts=draw(st.integers(min_value=0, max_value=4)),
        contact_history=tuple(
            sorted(
                draw(
                    st.lists(
                        near.map(lambda d: now + timedelta(days=d)),
                        max_size=6,
                    )
                )
            )
        ),
        consent=draw(st.one_of(st.none(), consent_records(now))),
        dnd_listed=draw(st.one_of(st.none(), st.booleans())),
        dnd_checked_at=draw(
            st.one_of(st.none(), near.map(lambda d: now + timedelta(days=d)))
        ),
        promise_to_pay=draw(
            st.one_of(st.none(), near.map(lambda d: (now + timedelta(days=d)).date()))
        ),
        is_mandate=draw(st.booleans()),
        pre_debit_notice_sent_at=draw(
            st.one_of(st.none(), near.map(lambda d: now + timedelta(days=d)))
        ),
        registered_templates=draw(
            st.sampled_from([frozenset(), template_ids(), frozenset({"VASOOL_REAUTH"})])
        ),
    )


@st.composite
def guard_contexts(draw) -> GuardContext:
    """An arbitrary decision: a real proposal, an arbitrary moment, an arbitrary
    world.

    `execute_at` is drawn independently of `now` so that contexts where the
    action lands well after the decision are generated too — that gap is
    adversary attack A04, and a strategy that always set them equal would never
    produce it.
    """
    event, proposal = draw(st.sampled_from(POOL))
    now = draw(utc_datetimes)
    execute_at = draw(st.one_of(st.just(now), utc_datetimes))
    return GuardContext(
        now=now,
        effective_at=max(now, execute_at),
        event=event,
        proposal=proposal.model_copy(update={"execute_at": execute_at}),
        facts=draw(policy_facts(now)),
    )


def permissive_facts(**overrides) -> PolicyFacts:
    """A world in which nothing is wrong. The baseline a unit test perturbs one
    fact at a time from.

    Not a default: PolicyFacts' own defaults are the *unknown* world, which
    blocks. This is the known-good one, and it has to be written out explicitly
    so that a test asserting a guard allows something is asserting it about a
    world someone deliberately described.
    """
    base = dict(
        merchant=MerchantPolicy(merchant_id="acc_test"),
        consent=ConsentRecord(
            granted_at=MIN_TIME, purposes=frozenset({CONSENT_PURPOSE_RECOVERY})
        ),
        dnd_listed=False,
        dnd_checked_at=POOL_NOW,
        registered_templates=template_ids(),
    )
    return PolicyFacts(**(base | overrides))


def context(
    proposal: Proposal,
    *,
    event: FailureEvent | None = None,
    now: datetime = POOL_NOW,
    effective_at: datetime | None = None,
    facts: PolicyFacts | None = None,
    **fact_overrides,
) -> GuardContext:
    """A GuardContext for a unit test, permissive unless told otherwise."""
    return GuardContext(
        now=now,
        effective_at=effective_at if effective_at is not None else max(now, proposal.execute_at),
        event=event if event is not None else POOL[0][0],
        proposal=proposal,
        facts=facts if facts is not None else permissive_facts(**fact_overrides),
    )


def proposals_for(
    reason: str, source: str | None = None, *, attempt: int = 1, now: datetime = POOL_NOW
) -> tuple[Proposal, ...]:
    """Everything the real classifier proposes for a row on disk."""
    from tests.payloads import event_for

    event = event_for(reason, source)
    return proposals_from(classify(event, clock=VirtualClock(now), attempt=attempt), event, now=now)


def proposal_for(
    reason: str, source: str | None = None, *, attempt: int = 1, now: datetime = POOL_NOW
) -> Proposal:
    """The primary proposal for a row on disk."""
    return proposals_for(reason, source, attempt=attempt, now=now)[0]
