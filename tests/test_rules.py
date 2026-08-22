"""The deterministic classifier and docs/taxonomy.md §6 timing.

Every FailureEvent here is built from a payload in data/observed_payloads/ or
data/stubbed_payloads/ — reason and source strings are read off disk, not typed
in. Where a test needs a combination that has never been observed (an
unfamiliar source, a reason from a future Razorpay release) it says so at the
call site and uses an obviously synthetic value.

Time is pinned with VirtualClock throughout. Nothing here depends on when the
suite runs.
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vasool.clock import VirtualClock
from vasool.diagnosis.rules import (
    IST,
    QUIET_HOURS_END_HOUR_IST,
    Diagnosis,
    classify,
    hold_out_of_quiet_hours,
    in_salary_window,
    next_liquidity_retry,
    next_salary_window,
)
from vasool.diagnosis.taxonomy import (
    CONTACT_INTERVENTIONS,
    lookup,
    RETRY_INTERVENTIONS,
    RULES,
    SOURCE_ANY,
    UNKNOWN_REASON,
    FailureClass,
    InterventionType,
    known_reasons,
)
from vasool.events.schemas import FailureEvent, from_webhook

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED = REPO_ROOT / "data" / "observed_payloads"
STUBBED = REPO_ROOT / "data" / "stubbed_payloads"
TEST_PEPPER = "test-pepper-do-not-use-in-prod"
TAXONOMY_LOGGER = "vasool.diagnosis.taxonomy"

# Tue 25 Aug 2026, 10:00 IST. The 25th on purpose: §6's worked example, the
# worst possible day of the month to retry an insufficient-funds failure.
FIXED_NOW = datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc)

C = FailureClass
I = InterventionType


# ---------------------------------------------------------------------------
# fixtures on disk -> FailureEvent
# ---------------------------------------------------------------------------
def _payment_failed_fixtures() -> list[pathlib.Path]:
    return sorted(OBSERVED.glob("payment_failed__*.json")) + sorted(
        STUBBED.glob("SIMULATED__payment_failed__*.json")
    )


def _event_from(path: pathlib.Path) -> FailureEvent:
    fixture = json.loads(path.read_text())
    return from_webhook(
        event_id=fixture["headers"]["x-razorpay-event-id"],
        body=fixture["body"],
        pepper=TEST_PEPPER,
    )


def _all_events() -> list[FailureEvent]:
    return [_event_from(p) for p in _payment_failed_fixtures()]


def _one_event_per_pair() -> list[FailureEvent]:
    """One representative event per (reason, source) pair on disk. The six
    duplicate payment_failed/gateway captures add nothing to a mapping test."""
    seen: dict[tuple[str, str], FailureEvent] = {}
    for event in _all_events():
        seen.setdefault((event.error_reason, event.error_source), event)
    return [seen[k] for k in sorted(seen)]


def event_for(reason: str, source: str | None = None) -> FailureEvent:
    """A real captured event for `reason`, optionally re-sourced."""
    for event in _all_events():
        if event.error_reason == reason:
            return event if source is None else event.model_copy(update={"error_source": source})
    raise LookupError(f"no payload on disk for {reason!r}")


def _observed_sources() -> list[str]:
    return sorted({e.error_source for e in _all_events()})


NEVER_OBSERVED_SOURCE = "network"
"""A source string that appears on no payload in data/. Used deliberately to
reach the payment_failed / *other* row, which by definition cannot be reached
with a source that has its own branch."""


def event_for_key(key: tuple[str, str]) -> FailureEvent:
    """A FailureEvent that resolves to exactly the §4 row `key` names."""
    reason, source = key
    if source != SOURCE_ANY:
        return event_for(reason, source)
    if reason in {r for r, s in RULES if s != SOURCE_ANY}:
        # This reason branches on source, so its catch-all row is only
        # reachable with a source none of the branches claim.
        return event_for(reason, NEVER_OBSERVED_SOURCE)
    return event_for(reason)


def _liquidity_trajectory(start: datetime) -> list[datetime]:
    """Walk a real episode: attempt N is scheduled at the moment attempt N-1
    failed, which is how the policy plane will call classify(). The month-long
    gap §6 argues about only appears when the ladder is walked this way — it is
    invisible if every attempt is computed from the original failure."""
    event = event_for("insufficient_fund")
    schedule: list[datetime] = []
    t = start
    for attempt in (1, 2, 3):
        t = classify(event, clock=VirtualClock(t), attempt=attempt).execute_at
        schedule.append(t)
    return schedule


def _pairs_in_class(failure_class: FailureClass) -> list[tuple[str, str | None]]:
    """Every §4 row in a class, as (reason, source). A source of None means
    "whatever the payload on disk carries" — those rows ignore source anyway."""
    return [
        (reason, None if source == SOURCE_ANY else source)
        for (reason, source), rule in sorted(RULES.items())
        if rule.failure_class is failure_class
    ]


def _pair_id(pair: tuple[str, str | None]) -> str:
    reason, source = pair
    return f"{reason}|{source or 'as-captured'}"


def at(now: datetime, **delta) -> datetime:
    return now + timedelta(**delta)


utc_datetimes = st.datetimes(
    min_value=datetime(2024, 1, 1), max_value=datetime(2030, 12, 31)
).map(lambda d: d.replace(tzinfo=timezone.utc))

any_reason = st.sampled_from(sorted(known_reasons()))


# ---------------------------------------------------------------------------
# §4, end to end: every payload on disk, at attempt 1
# ---------------------------------------------------------------------------
# (reason, source) -> (class, intervention, retry budget, delay from now)
EXPECTED_FIRST_ACTION: dict[tuple[str, str], tuple] = {
    ("payment_failed", "gateway"): (C.TRANSIENT, I.SILENT_RETRY, 1, timedelta(minutes=15)),
    ("payment_failed", "bank"): (C.INSTRUMENT_DEAD, I.SILENT_RETRY, 1, timedelta(hours=6)),
    ("gateway_technical_error", "gateway"): (C.TRANSIENT, I.SILENT_RETRY, 3, timedelta(minutes=5)),
    ("payment_timed_out", "customer"): (C.TRANSIENT, I.SILENT_RETRY, 1, timedelta(minutes=10)),
    ("insufficient_fund", "bank"): (C.LIQUIDITY, I.TIMED_RETRY, 3, timedelta(hours=48)),
    ("payment_cancelled", "customer"): (C.CUSTOMER_ACTION, I.REATTEMPT_LINK, 0, timedelta(hours=2)),
    ("card_declined", "bank"): (C.INSTRUMENT_DEAD, I.SILENT_RETRY, 1, timedelta(hours=6)),
    ("card_disabled_for_online_payments", "bank"): (
        C.INSTRUMENT_DEAD,
        I.REAUTH_LINK,
        0,
        timedelta(0),
    ),
    ("card_number_invalid", "customer"): (C.CUSTOMER_ACTION, I.REATTEMPT_LINK, 0, timedelta(0)),
    ("card_expired", "bank"): (C.INSTRUMENT_DEAD, I.REAUTH_LINK, 0, timedelta(0)),
    ("payment_risk_check_failed", "business"): (C.RISK_BLOCK, I.HUMAN_QUEUE, 0, timedelta(0)),
}


# ---------------------------------------------------------------------------
# §4's escalation column — the "-> X" half of every row, transcribed. Keyed on
# the §4 row rather than on a payload, because two rows (payment_failed's
# `business` and *other* branches) have no payload of their own.
#
# This is where the document moved, so this is where drift has to fail.
# ---------------------------------------------------------------------------
# (reason, source) -> (intervention once the budget is spent, delay from now)
EXPECTED_AFTER_BUDGET: dict[tuple[str, str], tuple[InterventionType, timedelta]] = {
    ("payment_failed", "gateway"): (I.REATTEMPT_LINK, timedelta(0)),
    ("payment_failed", "bank"): (I.REAUTH_LINK, timedelta(0)),
    ("payment_failed", "business"): (I.HUMAN_QUEUE, timedelta(0)),
    ("payment_failed", SOURCE_ANY): (I.HUMAN_QUEUE, timedelta(0)),
    ("gateway_technical_error", SOURCE_ANY): (I.REATTEMPT_LINK, timedelta(0)),
    ("payment_timed_out", SOURCE_ANY): (I.REATTEMPT_LINK, timedelta(0)),
    ("insufficient_fund", SOURCE_ANY): (I.REATTEMPT_LINK, timedelta(0)),
    ("payment_cancelled", SOURCE_ANY): (I.REATTEMPT_LINK, timedelta(hours=2)),
    ("card_declined", SOURCE_ANY): (I.REAUTH_LINK, timedelta(0)),
    ("card_disabled_for_online_payments", SOURCE_ANY): (I.REAUTH_LINK, timedelta(0)),
    ("card_number_invalid", SOURCE_ANY): (I.REATTEMPT_LINK, timedelta(0)),
    ("card_expired", SOURCE_ANY): (I.REAUTH_LINK, timedelta(0)),
    ("payment_risk_check_failed", SOURCE_ANY): (I.HUMAN_QUEUE, timedelta(0)),
}


class TestEveryPayloadOnDisk:
    def test_the_expectation_table_covers_every_pair_on_disk(self):
        pairs = {(e.error_reason, e.error_source) for e in _all_events()}
        assert pairs == set(EXPECTED_FIRST_ACTION)

    @pytest.mark.parametrize(
        "event", _one_event_per_pair(), ids=lambda e: f"{e.error_reason}|{e.error_source}"
    )
    def test_first_action(self, event: FailureEvent):
        expected_class, expected_action, budget, delay = EXPECTED_FIRST_ACTION[
            (event.error_reason, event.error_source)
        ]
        d = classify(event, clock=VirtualClock(FIXED_NOW))

        assert d.failure_class is expected_class
        assert d.intervention is expected_action
        assert d.retry_budget == budget
        assert d.execute_at == FIXED_NOW + delay

    @pytest.mark.parametrize(
        "event", _all_events(), ids=lambda e: f"{e.error_reason}|{e.error_source}"
    )
    def test_no_captured_payload_falls_to_the_unknown_path(self, event: FailureEvent):
        d = classify(event, clock=VirtualClock(FIXED_NOW))
        assert d.reason != UNKNOWN_REASON
        assert d.reason in known_reasons()

    def test_identical_captures_classify_identically(self):
        """Six payment_failed/gateway payloads were captured. They differ in id,
        amount and timestamp, and must not differ in diagnosis."""
        gateway = [
            e
            for e in _all_events()
            if (e.error_reason, e.error_source) == ("payment_failed", "gateway")
        ]
        assert len(gateway) > 1
        clock = VirtualClock(FIXED_NOW)
        results = {
            (d.failure_class, d.intervention, d.retry_budget, d.execute_at)
            for d in (classify(e, clock=clock) for e in gateway)
        }
        assert len(results) == 1


class TestEscalation:
    """What §4 does once the retry budget is spent. Every row names something —
    §5: "three retries and then nothing is a broken product"."""

    def test_the_expectation_table_covers_every_row(self):
        assert set(EXPECTED_AFTER_BUDGET) == set(RULES)

    @pytest.mark.parametrize(
        "key", sorted(EXPECTED_AFTER_BUDGET), ids=lambda k: f"{k[0]}|{k[1]}"
    )
    def test_escalation_after_the_budget_is_spent(self, key):
        expected_action, delay = EXPECTED_AFTER_BUDGET[key]
        event = event_for_key(key)
        clock = VirtualClock(FIXED_NOW)

        budget = classify(event, clock=clock).retry_budget
        d = classify(event, clock=clock, attempt=budget + 1)

        assert d.intervention is expected_action
        assert d.execute_at == FIXED_NOW + delay
        assert not d.is_retry

    def test_gateway_technical_error_escalates_to_a_link(self):
        """The row that used to trail off into nothing. §5: if four hours of
        backoff hasn't cleared it, the self-healing-blip hypothesis is dead."""
        clock = VirtualClock(FIXED_NOW)
        event = event_for("gateway_technical_error")
        assert classify(event, clock=clock, attempt=3).intervention is I.SILENT_RETRY
        assert classify(event, clock=clock, attempt=4).intervention is I.REATTEMPT_LINK

    def test_insufficient_fund_escalates_to_a_link(self):
        """Three failures spanning two paydays means the timing hypothesis has
        been tested and lost."""
        clock = VirtualClock(FIXED_NOW)
        event = event_for("insufficient_fund")
        assert classify(event, clock=clock, attempt=3).intervention is I.TIMED_RETRY
        assert classify(event, clock=clock, attempt=4).intervention is I.REATTEMPT_LINK

    def test_no_row_ends_in_silence(self):
        """The property behind both changes: a customer whose payment keeps
        failing is eventually asked to pay another way, or a human is told."""
        clock = VirtualClock(FIXED_NOW)
        for key in sorted(RULES):
            event = event_for_key(key)
            budget = classify(event, clock=clock).retry_budget
            d = classify(event, clock=clock, attempt=budget + 1)
            assert d.intervention is not None, key
            assert d.execute_at is not None, key

    def test_liquidity_escalation_keeps_the_episode_within_two_contacts(self):
        """§5: the soft nudge plus the link is exactly §7's per-episode cap, not
        a step past it."""
        clock = VirtualClock(FIXED_NOW)
        event = event_for("insufficient_fund")
        contacts = sum(
            (classify(event, clock=clock, attempt=a).soft_nudge)
            + (classify(event, clock=clock, attempt=a).intervention in CONTACT_INTERVENTIONS)
            for a in range(1, 5)
        )
        assert contacts == 2


# ---------------------------------------------------------------------------
# §3: the source branches
# ---------------------------------------------------------------------------
class TestSourceBranches:
    """§3 is the most important structural decision in the taxonomy: the same
    error_reason, from a different layer, is a different failure."""

    def test_gateway_and_bank_diverge_on_the_same_reason(self):
        clock = VirtualClock(FIXED_NOW)
        gateway = classify(event_for("payment_failed", "gateway"), clock=clock)
        bank = classify(event_for("payment_failed", "bank"), clock=clock)

        assert gateway.failure_class is C.TRANSIENT
        assert bank.failure_class is C.INSTRUMENT_DEAD
        assert gateway.execute_at != bank.execute_at

    def test_both_branches_come_from_real_captures(self):
        """VERIFIED.md: cards returned gateway, netbanking returned bank. If
        either disappears from data/observed_payloads/, the §3 argument stops
        being evidence-backed."""
        pairs = {(e.error_reason, e.error_source) for e in _all_events()}
        assert ("payment_failed", "gateway") in pairs
        assert ("payment_failed", "bank") in pairs

    def test_unfamiliar_source_gets_one_silent_retry_then_a_human(self):
        # "network" has never appeared on any payload. Deliberately so: this is
        # the branch for a source we have not seen.
        d = classify(event_for("payment_failed", "network"), clock=VirtualClock(FIXED_NOW))
        assert d.failure_class is C.TRANSIENT
        assert d.intervention is I.SILENT_RETRY
        assert d.retry_budget == 1
        assert d.execute_at == at(FIXED_NOW, minutes=30)

    def test_unfamiliar_source_escalates_to_a_human_not_to_the_customer(self):
        d = classify(
            event_for("payment_failed", "network"), clock=VirtualClock(FIXED_NOW), attempt=2
        )
        assert d.intervention is I.HUMAN_QUEUE

    @pytest.mark.parametrize("source", _observed_sources())
    def test_specific_reasons_ignore_source_entirely(self, source):
        """§3: 'Where the reason is specific, it already determines the class
        and source adds nothing.'"""
        clock = VirtualClock(FIXED_NOW)
        for reason in sorted(known_reasons() - {"payment_failed"}):
            baseline = classify(event_for(reason), clock=clock)
            resourced = classify(event_for(reason, source), clock=clock)
            assert resourced.failure_class is baseline.failure_class
            assert resourced.intervention is baseline.intervention
            assert resourced.execute_at == baseline.execute_at

    def test_the_source_is_kept_on_the_diagnosis_even_when_ignored(self):
        """§3 calls source noisy, so it never overrides a specific reason — but
        the receipt should still record what we were told."""
        d = classify(event_for("card_expired", "network"), clock=VirtualClock(FIXED_NOW))
        assert d.source == "network"
        assert d.failure_class is C.INSTRUMENT_DEAD


# ---------------------------------------------------------------------------
# the unknown path
# ---------------------------------------------------------------------------
class TestUnknownReason:
    @staticmethod
    def _unknown_event() -> FailureEvent:
        # Not a Razorpay string, and not meant to look like one — this is the
        # API-drift path, exercised with a value that could never be mistaken
        # for something in data/.
        return event_for("card_expired").model_copy(
            update={"error_reason": "a_reason_razorpay_has_never_sent"}
        )

    def test_fails_safe_to_one_silent_retry(self):
        d = classify(self._unknown_event(), clock=VirtualClock(FIXED_NOW))
        assert d.failure_class is C.TRANSIENT
        assert d.intervention is I.SILENT_RETRY
        assert d.retry_budget == 1
        assert d.execute_at == at(FIXED_NOW, minutes=30)
        assert d.reason == UNKNOWN_REASON

    def test_then_a_human_decides(self):
        d = classify(self._unknown_event(), clock=VirtualClock(FIXED_NOW), attempt=2)
        assert d.intervention is I.HUMAN_QUEUE

    def test_never_contacts_the_customer(self):
        """The least harmful possible action: no outbound on a reason we cannot
        interpret."""
        for attempt in range(1, 6):
            d = classify(self._unknown_event(), clock=VirtualClock(FIXED_NOW), attempt=attempt)
            assert d.intervention not in {I.REATTEMPT_LINK, I.REAUTH_LINK}
            assert not d.soft_nudge

    def test_logs_at_warn_with_the_full_string(self, caplog):
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            classify(self._unknown_event(), clock=VirtualClock(FIXED_NOW))
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert records[0].levelname == "WARNING"
        assert "a_reason_razorpay_has_never_sent" in records[0].getMessage()


# ---------------------------------------------------------------------------
# §6: salary-aware timing
# ---------------------------------------------------------------------------
class TestSalaryWindow:
    def test_the_25th_targets_payday_not_a_fixed_backoff(self):
        """The headline claim of §6. Aug 2026's last working day is Mon 31st."""
        d = classify(event_for("insufficient_fund"), clock=VirtualClock(FIXED_NOW), attempt=2)

        landed = d.execute_at.astimezone(IST)
        assert landed.date() == datetime(2026, 8, 31).date()
        assert d.execute_at != at(FIXED_NOW, hours=48)
        assert d.execute_at - FIXED_NOW > timedelta(days=5)

    def test_attempt_1_is_the_cheap_48h_probe(self):
        d = classify(event_for("insufficient_fund"), clock=VirtualClock(FIXED_NOW))
        assert d.execute_at == at(FIXED_NOW, hours=48)

    def test_attempt_3_waits_six_hours_for_the_credit_to_settle(self):
        clock = VirtualClock(FIXED_NOW)
        second = classify(event_for("insufficient_fund"), clock=clock, attempt=2)
        third = classify(event_for("insufficient_fund"), clock=clock, attempt=3)
        assert third.execute_at == second.execute_at + timedelta(hours=6)

    def test_the_window_opens_at_the_first_legal_hour(self):
        window = next_salary_window(FIXED_NOW).astimezone(IST)
        assert window.hour == QUIET_HOURS_END_HOUR_IST
        assert (window.minute, window.second) == (0, 0)

    def test_last_working_day_skips_the_weekend(self):
        """Sat 31 Oct 2026 -> the window opens Fri 30 Oct."""
        oct_20 = datetime(2026, 10, 20, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        window = next_salary_window(oct_20).astimezone(IST)
        assert window.date() == datetime(2026, 10, 30).date()
        assert window.weekday() == 4  # Friday

    def test_window_is_strictly_in_the_future(self):
        """Inside a window already, 'next' means the one after — otherwise
        attempt 2 gets scheduled for this instant and burns with no new
        information since the last one."""
        inside = datetime(2026, 9, 3, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        assert next_salary_window(inside) > inside

    def test_more_than_twelve_days_out_falls_back_to_five_days(self):
        """§6's escape hatch: on the 8th, payday is three weeks away."""
        sept_8 = datetime(2026, 9, 8, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        assert next_salary_window(sept_8) - sept_8 > timedelta(days=12)
        assert next_liquidity_retry(sept_8, attempt=2) == sept_8 + timedelta(days=5)

    def test_the_five_day_fallback_still_lands_near_money(self):
        """Scheduling attempt 2 the day after payday: +5 days keeps it inside
        the 1st-7th window rather than waiting a month."""
        aug_31 = datetime(2026, 8, 31, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        retry = next_liquidity_retry(aug_31, attempt=2).astimezone(IST)
        assert retry.date() == datetime(2026, 9, 5).date()
        assert in_salary_window(retry.date())

    def test_attempt_3_is_more_patient_than_attempt_2(self):
        """§6: the bound is asymmetric because the attempts are. Attempt 2 has a
        third attempt behind it, so twelve days is the right patience. Attempt 3
        is the last one, so landing near money matters more than landing soon —
        it waits up to twenty days.

        On 8 Sep payday is ~22 days out, so attempt 2 falls back. Fifteen days
        later it is inside attempt 3's bound but still outside attempt 2's."""
        sept_8 = datetime(2026, 9, 8, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        sept_15 = datetime(2026, 9, 15, 12, 0, tzinfo=IST).astimezone(timezone.utc)

        assert next_liquidity_retry(sept_8, attempt=2) == sept_8 + timedelta(days=5)
        assert next_liquidity_retry(sept_15, attempt=2) == sept_15 + timedelta(days=5)
        # Same distance to payday, last attempt: waits for it instead.
        assert next_liquidity_retry(sept_15, attempt=3) == next_salary_window(
            sept_15
        ) + timedelta(hours=6)


class TestDocumentedTrajectories:
    """§6's two worked examples, asserted exactly. Each attempt is scheduled at
    the moment the previous one failed, which is the only way the month-long gap
    the 20-day bound removes is visible at all."""

    def test_late_month_episode(self):
        """25 Aug 2026 -> 27 Aug, 31 Aug, 5 Sep. The gap the bound removes:
        without it, attempt 3 sat on 30 September."""
        landed = [d.astimezone(IST).date() for d in _liquidity_trajectory(FIXED_NOW)]
        assert landed == [date(2026, 8, 27), date(2026, 8, 31), date(2026, 9, 5)]

    def test_mid_month_episode(self):
        """8 Sep 2026 -> 10 Sep, 15 Sep, 30 Sep. The landing the bound keeps:
        under a twelve-day bound this last attempt fired on the 20th and the
        episode never touched a salary window at all."""
        start = datetime(2026, 9, 8, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        landed = [d.astimezone(IST).date() for d in _liquidity_trajectory(start)]
        assert landed == [date(2026, 9, 10), date(2026, 9, 15), date(2026, 9, 30)]

    def test_the_late_month_episode_never_leaves_a_month_long_gap(self):
        schedule = _liquidity_trajectory(FIXED_NOW)
        assert max(b - a for a, b in zip(schedule, schedule[1:])) < timedelta(days=12)

    def test_the_mid_month_episode_still_lands_on_payday(self):
        """The feature the bound protects. A twelve-day bound would flatten this
        to 10/15/20 Sep, none of which is near money."""
        start = datetime(2026, 9, 8, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        last = _liquidity_trajectory(start)[-1].astimezone(IST)
        assert in_salary_window(last.date())
        assert last.date() != date(2026, 9, 20)

    @settings(max_examples=200)
    @given(utc_datetimes)
    def test_no_gap_between_liquidity_attempts_exceeds_the_bound(self, start):
        """§6's bound as a property, over any starting instant.

        The ceiling is twenty days plus six hours, not a flat twenty: the bound
        governs the wait for the salary *window*, and attempt 3's "let the credit
        settle" delay rides on top of it. A window exactly twenty days out is
        inside the bound and then adds the six hours."""
        schedule = _liquidity_trajectory(start)
        gaps = [b - a for a, b in zip([start, *schedule], schedule)]
        assert max(gaps) <= timedelta(days=20, hours=6)

    @settings(max_examples=200)
    @given(utc_datetimes)
    def test_the_ladder_never_runs_backwards(self, start):
        schedule = _liquidity_trajectory(start)
        assert schedule == sorted(schedule)
        assert schedule[0] > start

    @settings(max_examples=200)
    @given(utc_datetimes)
    def test_the_window_always_lands_on_a_salary_day(self, now):
        assert in_salary_window(next_salary_window(now).astimezone(IST).date())

    @settings(max_examples=200)
    @given(utc_datetimes, st.integers(min_value=1, max_value=3))
    def test_liquidity_retries_are_always_in_the_future(self, now, attempt):
        assert next_liquidity_retry(now, attempt=attempt) > now


# ---------------------------------------------------------------------------
# §6: the 00:00-06:00 IST exclusion
# ---------------------------------------------------------------------------
class TestQuietHours:
    """§6's 00:00-06:00 hold, which applies to retries and to nothing else.

    The two halves of §6's rule now live in different planes. The retry half is
    an efficacy claim — issuers running overnight batch maintenance return
    spurious failures that consume an attempt — and efficacy is this plane's
    business. The contact half is a claim about disturbing a human, and belongs
    to ContactWindowGuard, which enforces a wider window (08:00-19:00) at
    execution time rather than at classification time.
    """

    def test_a_retry_at_02_00_is_held_to_06_00(self):
        two_am = datetime(2026, 8, 25, 2, 0, tzinfo=IST).astimezone(timezone.utc)
        landed = classify(
            event_for("gateway_technical_error"), clock=VirtualClock(two_am)
        ).execute_at.astimezone(IST)
        assert landed.hour == QUIET_HOURS_END_HOUR_IST
        assert landed.minute == 0
        assert landed.date() == datetime(2026, 8, 25).date()

    def test_a_retry_that_would_land_in_the_quiet_period_is_pushed_out(self):
        # 23:50 IST + 15m would be 00:05 IST.
        late = datetime(2026, 8, 25, 23, 50, tzinfo=IST).astimezone(timezone.utc)
        landed = classify(
            event_for("payment_failed", "gateway"), clock=VirtualClock(late)
        ).execute_at.astimezone(IST)
        assert landed.hour == QUIET_HOURS_END_HOUR_IST
        assert landed.date() == datetime(2026, 8, 26).date()

    def test_06_00_itself_is_allowed(self):
        six_am = datetime(2026, 8, 25, 6, 0, tzinfo=IST).astimezone(timezone.utc)
        assert hold_out_of_quiet_hours(six_am) == six_am

    def test_the_hold_never_moves_a_retry_backwards(self):
        two_am = datetime(2026, 8, 25, 2, 0, tzinfo=IST).astimezone(timezone.utc)
        d = classify(event_for("gateway_technical_error"), clock=VirtualClock(two_am))
        assert d.execute_at > two_am

    def test_an_outbound_contact_is_not_held_here(self):
        """It is held by ContactWindowGuard instead, on a window that already
        contains this one. Holding it in both places moved a 05:00 re-auth link
        twice for the same reason, and hid the compliance save from the receipt:
        the classifier now proposes "immediately" and the guard defers it to
        08:00 citing the clause, which is both more honest and a better demo."""
        two_am = datetime(2026, 8, 25, 2, 0, tzinfo=IST).astimezone(timezone.utc)
        d = classify(event_for("card_expired"), clock=VirtualClock(two_am))
        assert d.intervention in CONTACT_INTERVENTIONS
        assert d.execute_at == two_am

    def test_a_human_queue_handoff_is_never_held(self):
        """The bug the split fixed. Nothing is sent to anyone on this path, so
        no rule applied to it — and a risk-declined payment arriving at 02:00
        was sitting until 06:00 before an operator could even see it."""
        two_am = datetime(2026, 8, 25, 2, 0, tzinfo=IST).astimezone(timezone.utc)
        d = classify(event_for("payment_risk_check_failed"), clock=VirtualClock(two_am))
        assert d.intervention is I.HUMAN_QUEUE
        assert d.execute_at == two_am

    @settings(max_examples=300)
    @given(utc_datetimes, any_reason, st.integers(min_value=1, max_value=5))
    def test_no_retry_is_ever_scheduled_in_the_quiet_period(self, now, reason, attempt):
        """The invariant, narrowed to what this plane actually owns: no retry
        the diagnosis plane emits falls between 00:00 and 06:00 IST."""
        d = classify(event_for(reason), clock=VirtualClock(now), attempt=attempt)
        if d.is_retry and d.execute_at is not None:
            assert d.execute_at.astimezone(IST).hour >= QUIET_HOURS_END_HOUR_IST

    @settings(max_examples=300)
    @given(utc_datetimes, any_reason, st.integers(min_value=1, max_value=5))
    def test_nothing_but_a_retry_is_ever_moved(self, now, reason, attempt):
        """The other half of the ownership claim, and the one that keeps this
        plane out of the policy plane's business: a non-retry executes exactly
        when §4's row says, and any holding of it happens downstream where the
        clause can be cited."""
        event = event_for(reason)
        d = classify(event, clock=VirtualClock(now), attempt=attempt)
        if not d.is_retry and d.execute_at is not None:
            _, rule = lookup(event.error_reason, event.error_source)
            assert d.execute_at == now + rule.post_retry_delay


# ---------------------------------------------------------------------------
# §6: TRANSIENT backoff
# ---------------------------------------------------------------------------
class TestTransientBackoff:
    def test_gateway_technical_error_ladder_is_5m_30m_4h(self):
        clock = VirtualClock(FIXED_NOW)
        event = event_for("gateway_technical_error")
        ladder = [
            classify(event, clock=clock, attempt=n).execute_at - FIXED_NOW for n in (1, 2, 3)
        ]
        assert ladder == [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=4)]

    def test_the_clean_transient_gets_three_times_the_budget_of_the_vague_one(self):
        """§5: 'same class, three times the budget, because here we actually
        know what broke.'"""
        clock = VirtualClock(FIXED_NOW)
        clean = classify(event_for("gateway_technical_error"), clock=clock)
        vague = classify(event_for("payment_failed", "gateway"), clock=clock)
        assert clean.failure_class is vague.failure_class is C.TRANSIENT
        assert clean.retry_budget == 3
        assert vague.retry_budget == 1

    def test_transient_retries_never_contact_the_customer(self):
        """§2: messaging on a transient converts an invisible, self-healing
        problem into a visible one, and burns a contact we may need later."""
        clock = VirtualClock(FIXED_NOW)
        for attempt in (1, 2, 3):
            d = classify(event_for("gateway_technical_error"), clock=clock, attempt=attempt)
            assert d.intervention is I.SILENT_RETRY
            assert not d.soft_nudge

    def test_the_three_retries_stay_silent_before_escalating(self):
        """The escalation is the fourth action, not a contact riding alongside
        the retries. §2's rule that a TRANSIENT gets no contact holds for the
        whole time the retries are running."""
        clock = VirtualClock(FIXED_NOW)
        event = event_for("gateway_technical_error")
        for attempt in (1, 2, 3):
            assert classify(event, clock=clock, attempt=attempt).intervention is I.SILENT_RETRY
        assert classify(event, clock=clock, attempt=4).intervention in CONTACT_INTERVENTIONS


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------
class TestRiskBlockInvariant:
    """§2: 'Hard stop. Human queue. Zero outbound.' The sharpest rule in the
    taxonomy, so it is checked over generated input rather than one example."""

    @settings(max_examples=400)
    @given(
        st.integers(min_value=1, max_value=25),
        st.one_of(st.sampled_from(_observed_sources()), st.text(max_size=20)),
        utc_datetimes,
    )
    def test_risk_check_failure_only_ever_reaches_a_human(self, attempt, source, now):
        d = classify(
            event_for("payment_risk_check_failed", source),
            clock=VirtualClock(now),
            attempt=attempt,
        )
        assert d.failure_class is C.RISK_BLOCK
        assert d.intervention is I.HUMAN_QUEUE
        assert d.retry_budget == 0

    @settings(max_examples=100)
    @given(st.integers(min_value=1, max_value=25))
    def test_risk_check_failure_never_retries_the_instrument(self, attempt):
        d = classify(
            event_for("payment_risk_check_failed"), clock=VirtualClock(FIXED_NOW), attempt=attempt
        )
        assert d.intervention not in RETRY_INTERVENTIONS
        assert not d.is_retry

    @settings(max_examples=100)
    @given(st.integers(min_value=1, max_value=25))
    def test_risk_check_failure_sends_nothing_outbound(self, attempt):
        """If it was fraud, a retry loop is the fraudster's tool. If it was a
        false positive, an unexpected payment link is structurally phishing."""
        d = classify(
            event_for("payment_risk_check_failed"), clock=VirtualClock(FIXED_NOW), attempt=attempt
        )
        assert d.intervention not in {I.REATTEMPT_LINK, I.REAUTH_LINK}
        assert not d.soft_nudge
        assert not d.explain


class TestBusinessSourceInvariant:
    """§5's precautionary row, held to the same standard as the explicit one.

    The argument for this row is that being wrong in the automated direction is
    far more expensive than being wrong in the human direction. That argument is
    only worth anything if there is genuinely no path from a business-sourced
    payment_failed to an automated action — so it is checked the same way the
    explicit risk block is, over generated attempts and clocks."""

    @settings(max_examples=400)
    @given(st.integers(min_value=1, max_value=25), utc_datetimes)
    def test_business_source_never_reaches_an_automated_action(self, attempt, now):
        d = classify(
            event_for("payment_failed", "business"),
            clock=VirtualClock(now),
            attempt=attempt,
        )
        assert d.failure_class is C.RISK_BLOCK
        assert d.intervention is I.HUMAN_QUEUE
        assert not d.is_retry
        assert d.intervention not in RETRY_INTERVENTIONS
        assert d.intervention not in CONTACT_INTERVENTIONS
        assert not d.soft_nudge

    def test_business_takes_precedence_over_the_catch_all(self):
        """Ordering matters: before this row existed, a business-sourced
        payment_failed fell to the *other* branch and got a silent retry — an
        automated re-presentation of what may be a declined authorisation."""
        clock = VirtualClock(FIXED_NOW)
        business = classify(event_for("payment_failed", "business"), clock=clock)
        catch_all = classify(event_for("payment_failed", NEVER_OBSERVED_SOURCE), clock=clock)

        assert catch_all.intervention is I.SILENT_RETRY
        assert business.intervention is I.HUMAN_QUEUE
        assert business.failure_class is not catch_all.failure_class

    def test_business_is_inert_on_the_specific_reason_too(self):
        """The explicit risk reason and the inferred source agree, which is the
        point — the row exists so the two are handled identically."""
        clock = VirtualClock(FIXED_NOW)
        explicit = classify(event_for("payment_risk_check_failed"), clock=clock)
        inferred = classify(event_for("payment_failed", "business"), clock=clock)
        assert explicit.failure_class is inferred.failure_class
        assert explicit.intervention is inferred.intervention

    def test_business_on_a_specific_reason_still_ignores_source(self):
        """§3's boundary: the business row is a payment_failed branch, not a
        global override. An expired card reported by `business` is still an
        expired card, not a risk block."""
        d = classify(event_for("card_expired", "business"), clock=VirtualClock(FIXED_NOW))
        assert d.failure_class is C.INSTRUMENT_DEAD
        assert d.intervention is I.REAUTH_LINK


class TestInstrumentDeadInvariant:
    @pytest.mark.parametrize(
        "pair", _pairs_in_class(FailureClass.INSTRUMENT_DEAD), ids=_pair_id
    )
    @settings(max_examples=100)
    @given(st.integers(min_value=1, max_value=25), utc_datetimes)
    def test_no_retry_survives_a_spent_budget(self, pair, attempt, now):
        """§5 (card_expired): every futile retry is paid for out of a budget the
        re-auth link needed."""
        reason, source = pair
        d = classify(event_for(reason, source), clock=VirtualClock(now), attempt=attempt)
        assert d.failure_class is C.INSTRUMENT_DEAD
        if attempt > d.retry_budget:
            assert d.intervention not in RETRY_INTERVENTIONS
            assert d.intervention is I.REAUTH_LINK

    @pytest.mark.parametrize(
        "pair", _pairs_in_class(FailureClass.INSTRUMENT_DEAD), ids=_pair_id
    )
    def test_at_most_one_soft_decline_probe(self, pair):
        reason, source = pair
        clock = VirtualClock(FIXED_NOW)
        retries = [
            attempt
            for attempt in range(1, 26)
            if classify(event_for(reason, source), clock=clock, attempt=attempt).is_retry
        ]
        assert retries in ([], [1]), f"{_pair_id(pair)} retried on attempts {retries}"

    def test_card_expired_never_retries_at_all(self):
        """The flagship zero. An expired card has no state of the world in which
        it authorises."""
        clock = VirtualClock(FIXED_NOW)
        for attempt in range(1, 10):
            d = classify(event_for("card_expired"), clock=clock, attempt=attempt)
            assert d.intervention is I.REAUTH_LINK
            assert not d.is_retry

    def test_card_declined_probes_once_at_six_hours_then_stops(self):
        clock = VirtualClock(FIXED_NOW)
        first = classify(event_for("card_declined"), clock=clock, attempt=1)
        second = classify(event_for("card_declined"), clock=clock, attempt=2)
        assert first.intervention is I.SILENT_RETRY
        assert first.execute_at == at(FIXED_NOW, hours=6)
        assert second.intervention is I.REAUTH_LINK
        assert not second.is_retry


class TestLiquidity:
    def test_one_soft_nudge_not_three(self):
        """§2: 'Yes, one soft nudge.' It rides attempt 1, where 'top up, we'll
        try again in two days' is still actionable."""
        clock = VirtualClock(FIXED_NOW)
        nudged = [
            attempt
            for attempt in range(1, 5)
            if classify(event_for("insufficient_fund"), clock=clock, attempt=attempt).soft_nudge
        ]
        assert nudged == [1]

    def test_liquidity_retries_are_timed_not_silent(self):
        clock = VirtualClock(FIXED_NOW)
        for attempt in (1, 2, 3):
            d = classify(event_for("insufficient_fund"), clock=clock, attempt=attempt)
            assert d.intervention is I.TIMED_RETRY
            assert d.is_retry

    def test_budget_is_three_then_a_link(self):
        d = classify(event_for("insufficient_fund"), clock=VirtualClock(FIXED_NOW), attempt=4)
        assert d.intervention is I.REATTEMPT_LINK
        assert d.execute_at == FIXED_NOW
        assert not d.is_retry

    def test_the_plural_spelling_still_reaches_liquidity(self):
        """The alias, end to end: a payload spelled with the plural must not
        fall through to the unknown path."""
        event = event_for("insufficient_fund").model_copy(
            update={"error_reason": "insufficient_funds"}
        )
        d = classify(event, clock=VirtualClock(FIXED_NOW))
        assert d.failure_class is C.LIQUIDITY
        assert d.reason == "insufficient_fund"


class TestExplanationFlag:
    def test_card_disabled_carries_the_explain_flag(self):
        """§5: the intervention is trivial, the wording decides whether it
        works. 'Payment failed, try again' is useless here."""
        d = classify(event_for("card_disabled_for_online_payments"), clock=VirtualClock(FIXED_NOW))
        assert d.intervention is I.REAUTH_LINK
        assert d.explain

    def test_card_expired_does_not(self):
        assert not classify(event_for("card_expired"), clock=VirtualClock(FIXED_NOW)).explain


# ---------------------------------------------------------------------------
# determinism / clock injection
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_all_time_comes_from_the_injected_clock(self):
        early = classify(event_for("card_expired"), clock=VirtualClock(FIXED_NOW))
        later = classify(event_for("card_expired"), clock=VirtualClock(at(FIXED_NOW, days=1)))
        assert early.execute_at == FIXED_NOW
        assert later.execute_at == at(FIXED_NOW, days=1)

    def test_the_event_timestamp_does_not_drive_scheduling(self):
        """Replay: a two-year-old event replayed now schedules from now, not
        from when it happened."""
        stale = event_for("card_expired").model_copy(
            update={"occurred_at": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        )
        assert classify(stale, clock=VirtualClock(FIXED_NOW)).execute_at == FIXED_NOW

    @pytest.mark.parametrize(
        "event", _one_event_per_pair(), ids=lambda e: f"{e.error_reason}|{e.error_source}"
    )
    def test_classification_is_pure(self, event):
        clock = VirtualClock(FIXED_NOW)
        assert classify(event, clock=clock) == classify(event, clock=clock)

    def test_diagnosis_is_frozen(self):
        d = classify(event_for("card_expired"), clock=VirtualClock(FIXED_NOW))
        assert isinstance(d, Diagnosis)
        with pytest.raises(Exception):
            d.intervention = I.SILENT_RETRY

    def test_every_diagnosis_carries_a_rationale_for_the_receipt(self):
        clock = VirtualClock(FIXED_NOW)
        for event in _one_event_per_pair():
            assert classify(event, clock=clock).rationale

    def test_attempt_zero_is_a_programming_error(self):
        with pytest.raises(ValueError):
            classify(event_for("card_expired"), clock=VirtualClock(FIXED_NOW), attempt=0)


class TestNoActionWithoutATime:
    @settings(max_examples=300)
    @given(utc_datetimes, any_reason, st.integers(min_value=1, max_value=6))
    def test_execute_at_is_present_exactly_when_an_intervention_is(self, now, reason, attempt):
        """No row currently ends in silence, so the None side of this is vacuous
        today. It stays because the coupling is what the policy plane relies on:
        an action without a time, or a time without an action, is a bug however
        the table changes."""
        d = classify(event_for(reason), clock=VirtualClock(now), attempt=attempt)
        assert (d.execute_at is None) == (d.intervention is None)

    @settings(max_examples=300)
    @given(utc_datetimes, any_reason, st.integers(min_value=1, max_value=6))
    def test_nothing_is_ever_scheduled_in_the_past(self, now, reason, attempt):
        d = classify(event_for(reason), clock=VirtualClock(now), attempt=attempt)
        if d.execute_at is not None:
            assert d.execute_at >= now


def test_source_any_sentinel_is_not_a_real_source():
    assert SOURCE_ANY not in _observed_sources()
