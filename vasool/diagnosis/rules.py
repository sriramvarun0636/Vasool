"""The deterministic classifier: FailureEvent -> Diagnosis.

Every row in docs/taxonomy.md §4 is a dictionary lookup, not a judgment call
(§8). This module does that lookup and applies §6's timing. It is the baseline
the Session-7 LLM classifier has to beat on the ambiguous branches before it
earns any part of this job — and the measurement is the deliverable, not the
LLM.

What lives here that isn't in taxonomy.py: §6. Salary-aware retry timing for
LIQUIDITY, the fixed TRANSIENT ladder, and the 00:00-06:00 IST exclusion.

All time is injected. The wall clock is reachable only through vasool/clock.py
(CLAUDE.md invariant 2), and scheduling is computed from `clock.now()` rather
than `event.occurred_at` — a replayed two-year-old event schedules from the
moment we are deciding, not from the moment it happened.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from vasool.clock import Clock
from vasool.diagnosis.taxonomy import (
    RETRY_INTERVENTIONS,
    FailureClass,
    InterventionType,
    Rule,
    lookup,
)
from vasool.events.schemas import FailureEvent

IST = timezone(timedelta(hours=5, minutes=30))
"""India Standard Time. A fixed offset with no DST, which is why the hour
arithmetic below can use .replace() safely."""

QUIET_HOURS_END_HOUR_IST = 6
"""§6: nothing is scheduled between 00:00 and 06:00 IST.

One boundary, two rules behind it, and §6 keeps them separate because one is far
better justified than the other:

  - **Outbound contact: absolute.** A message at 2am is harassment whatever it
    says. Not a tuning parameter, and it does not trade off against recovery
    rate.
  - **Silent retries: a cheap hedge.** The reason is that some issuers run batch
    maintenance overnight and return a spurious technical failure that consumes
    an attempt for reasons unrelated to the customer. Holding a 5-minute gateway
    retry until 06:00 costs about four hours of recovery latency and disturbs
    nobody, since nothing is sent — so it is applied, but it is the first thing
    to relax if that latency ever matters.

# VERIFY: the batch-maintenance claim is documentation and folklore, never
# observed on this account (docs/taxonomy.md §9). The contact half needs no such
# defence and does not depend on it.

Applied at one boundary so the invariant "nothing the diagnosis plane emits
falls in the quiet period" is a single property test rather than a per-class
argument.
"""

SALARY_WINDOW_OPEN_HOUR_IST = QUIET_HOURS_END_HOUR_IST
"""A salary window opens at the first legal hour of its first day.

Deliberately tied to the quiet-hours boundary: salary credits land overnight,
so the first moment we are allowed to act is also the first moment worth
acting. Attempt 3's "+6h to let the credit settle" then reads as noon IST on
payday rather than as an argument with the quiet-hours rule.
"""

LIQUIDITY_FIRST_PROBE = timedelta(hours=48)
"""§6 attempt 1: cheap, covers short-term timing."""

PATIENCE_WITH_AN_ATTEMPT_IN_RESERVE = timedelta(days=12)
"""How long an attempt waits for payday when another attempt is behind it.

§6: attempt 2 has a third attempt in reserve, so waiting for payday is cheap and
twelve days is the right amount of patience.
"""

PATIENCE_ON_THE_LAST_ATTEMPT = timedelta(days=20)
"""How long the final attempt waits for payday.

§6: attempt 3 is the last one, so landing it near money matters more than
landing it soon — it gets twenty days before falling back.

Twenty rather than twelve because twelve removes the month-long gap by
flattening the ladder instead of capping it. A failure on 8 September pushes
attempt 2 to the 15th; at twelve days attempt 3 would fall back again to the
20th and the episode would never touch a salary window at all, which is the one
thing §6 exists to prevent. At twenty it waits for the 30th.
"""

LIQUIDITY_WINDOW_PATIENCE: dict[int, timedelta] = {
    2: PATIENCE_WITH_AN_ATTEMPT_IN_RESERVE,
    3: PATIENCE_ON_THE_LAST_ATTEMPT,
}
"""§6's bound, per attempt. Asymmetric because the attempts are: what an attempt
can afford to wait for depends on whether anything follows it."""

LAST_LIQUIDITY_ATTEMPT = 3
"""The salary ladder is three rungs (§6). Any attempt at or past this is the
last one and gets the last one's patience."""

LIQUIDITY_FALLBACK = timedelta(days=5)
"""§6: what an attempt does instead of waiting for a payday too far out."""

LIQUIDITY_SETTLE = timedelta(hours=6)
"""§6 attempt 3: let the credit settle before re-presenting."""

SALARY_WINDOW_MONTH_START_DAYS = 7
"""§6: Indian salary credit clusters on the 1st-7th and the last working day."""

_MAX_WINDOW_SEARCH_DAYS = 70
"""Bound on the forward search. The longest real gap between window starts is
about 30 days; this only exists so a logic bug fails loudly instead of hanging.
"""


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """What the rules classifier concluded. Inert — it describes an action, it
    does not perform one (CLAUDE.md invariant 1).

    Feeds the Proposal that the policy plane gates and the ledger records, so
    everything a receipt needs to explain the decision is carried here.
    """

    failure_class: FailureClass
    intervention: InterventionType | None
    """None means the retry budget is spent and the row names no escalation —
    the policy plane's EXHAUSTED terminal. `execute_at` is None exactly when
    this is. No row in §4 currently ends that way; the case is kept because the
    coupling with execute_at is what the policy plane relies on."""

    retry_budget: int
    """Total retries this row allows, from §4. Not the number remaining."""

    attempt: int
    """Which attempt this diagnosis is for. 1 is the original failure."""

    execute_at: datetime | None
    """UTC, never inside the quiet period, never in the past."""

    reason: str
    """Normalised error_reason, or taxonomy.UNKNOWN_REASON."""

    source: str
    """error_source exactly as received, kept for the receipt even where the
    row ignores it (§3: source is noisy, and an unfamiliar one is signal)."""

    soft_nudge: bool
    """Accompany this action with one low-pressure message (LIQUIDITY only)."""

    explain: bool
    """The message must name the specific cause, not say "payment failed"."""

    rationale: str
    """Why, in one line, traceable to docs/taxonomy.md §5."""

    @property
    def is_retry(self) -> bool:
        """True iff this action re-presents the instrument and so spends one of
        the four attempts before Razorpay halts a subscription."""
        return self.intervention in RETRY_INTERVENTIONS


# ---------------------------------------------------------------------------
# §6 — salary-aware timing
# ---------------------------------------------------------------------------
def last_working_day(year: int, month: int) -> date:
    """The last Mon-Fri of the month.

    # VERIFY: bank holidays are not modelled. Indian bank holidays are
    # state-specific and there is no stdlib calendar for them, so a salary
    # window opening on a gazetted holiday will be a day early. The failure
    # mode is a retry one day before the credit rather than a missed window,
    # which is the cheaper direction to be wrong in.
    """
    day = date(year, month, calendar.monthrange(year, month)[1])
    while day.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        day -= timedelta(days=1)
    return day


def in_salary_window(day: date) -> bool:
    """§6: salary credit clusters on the 1st-7th and the last working day.

    The last working day of a month is never earlier than the 26th, so the two
    halves of the definition never merge into one degenerate window.
    """
    return day.day <= SALARY_WINDOW_MONTH_START_DAYS or day == last_working_day(
        day.year, day.month
    )


def next_salary_window(now: datetime) -> datetime:
    """The opening instant of the next salary window strictly after `now`.

    "Next" is strict even when `now` is already inside a window. Two reasons:
    a retry scheduled for this instant burns an attempt with no new information
    since the last one; and §6's "too far out, use +5 days" fallback is what
    handles the inside-a-window case, landing attempt 2 five days later —
    which, one day after payday, is still inside the 1st-7th window.

    Worked example (the one §6 argues from). Failure on Tue 25 Aug 2026: the
    next window opens Mon 31 Aug, the last working day, six days out. Attempt 2
    goes there rather than to a fixed +48h, which would have re-presented on
    the 27th, at the point in the month when the balance is least likely to
    cover it.
    """
    day = now.astimezone(IST).date()
    for _ in range(_MAX_WINDOW_SEARCH_DAYS):
        day += timedelta(days=1)
        # The *opening* of a window: in the window, with the day before it out.
        if in_salary_window(day) and not in_salary_window(day - timedelta(days=1)):
            return datetime(
                day.year,
                day.month,
                day.day,
                SALARY_WINDOW_OPEN_HOUR_IST,
                tzinfo=IST,
            ).astimezone(timezone.utc)
    raise RuntimeError(
        f"no salary window within {_MAX_WINDOW_SEARCH_DAYS} days of {now!r} — "
        "in_salary_window() is broken"
    )


def next_liquidity_retry(now: datetime, attempt: int) -> datetime:
    """§6's ladder for LIQUIDITY.

        attempt 1:  now + 48h                 # cheap, covers short-term timing
        attempt 2:  next salary window        # unless >12 days out, then +5 days
        attempt 3:  next salary window + 6h   # unless >20 days out, then +5 days

    Fixed exponential backoff is wrong here because the constraint isn't system
    state, it's the customer's bank balance, and that follows a monthly cycle.

    Both later attempts are bounded, asymmetrically, because what an attempt can
    afford to wait for depends on whether anything follows it — see
    LIQUIDITY_WINDOW_PATIENCE. Without a bound on attempt 3 the ladder produces
    a real month-long gap: a failure on 25 August puts attempt 2 on the 31st,
    and from there the next window is 30 September. A subscription customer who
    hears nothing for a month has already churned.

    The two trajectories the bounds have to satisfy, each attempt scheduled at
    the moment the previous one failed (tests/test_rules.py asserts both):

        failure 25 Aug (late month):  27 Aug -> 31 Aug ->  5 Sep
        failure  8 Sep (mid month) :  10 Sep -> 15 Sep -> 30 Sep

    The late-month episode loses the month-long gap; the mid-month one keeps its
    payday landing.
    """
    if attempt <= 1:
        return now + LIQUIDITY_FIRST_PROBE

    window = next_salary_window(now)
    patience = LIQUIDITY_WINDOW_PATIENCE[min(attempt, LAST_LIQUIDITY_ATTEMPT)]
    if window - now > patience:
        return now + LIQUIDITY_FALLBACK
    if attempt < LAST_LIQUIDITY_ATTEMPT:
        return window
    return window + LIQUIDITY_SETTLE


def hold_out_of_quiet_hours(when: datetime) -> datetime:
    """Push `when` forward to 06:00 IST if it lands in the quiet period.

    Only ever moves forward, so it cannot schedule anything into the past.
    """
    local = when.astimezone(IST)
    if local.hour < QUIET_HOURS_END_HOUR_IST:
        local = local.replace(
            hour=QUIET_HOURS_END_HOUR_IST, minute=0, second=0, microsecond=0
        )
        return local.astimezone(timezone.utc)
    return when


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------
def _retry_at(rule: Rule, now: datetime, attempt: int) -> datetime:
    if rule.salary_aware:
        return next_liquidity_retry(now, attempt)
    # Safe to index: tests/test_taxonomy.py asserts len(retry_delays) ==
    # retry_budget for every row, and this is only reached inside the budget.
    return now + rule.retry_delays[attempt - 1]


def classify(event: FailureEvent, *, clock: Clock, attempt: int = 1) -> Diagnosis:
    """Classify a failed payment and say what to do about it, and when.

    `attempt` is the attempt being scheduled: 1 is the response to the original
    failure, 2 the response to the first retry having failed, and so on.
    FailureEvent deliberately carries no attempt_number — no observed payload
    has one — so the caller counts prior FailureEvents for the same entity and
    passes the result. Attempts past the row's budget yield the escalation, or
    no action at all where §4 defines none.

    Nothing here reaches Razorpay, the customer, or the database. It returns a
    description of an action; only actions/executor.py may perform one.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")

    now = clock.now()
    reason, rule = lookup(event.error_reason, event.error_source)

    if attempt <= rule.retry_budget:
        intervention = rule.retry_intervention
        execute_at = hold_out_of_quiet_hours(_retry_at(rule, now, attempt))
    else:
        intervention = rule.post_retry
        execute_at = (
            None
            if intervention is None
            else hold_out_of_quiet_hours(now + rule.post_retry_delay)
        )

    return Diagnosis(
        failure_class=rule.failure_class,
        intervention=intervention,
        retry_budget=rule.retry_budget,
        attempt=attempt,
        execute_at=execute_at,
        reason=reason,
        source=event.error_source,
        # §2 allows LIQUIDITY exactly one soft nudge. It rides attempt 1, where
        # "top up, we'll try again in two days" is still actionable. Capping it
        # here rather than leaving it to FrequencyCapGuard keeps the count a
        # property of the taxonomy, where §2 puts it.
        soft_nudge=rule.soft_nudge and attempt == 1,
        explain=rule.explain,
        rationale=rule.rationale,
    )
