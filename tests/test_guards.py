"""The thirteen, one class per guard.

Cross-cutting properties — fail-closed, deferral progress, purity, order
independence — live in tests/test_guard_properties.py. This file is the
per-guard behaviour: what each rule actually does, at its boundaries.

Every proposal is produced by running the real classifier over a real captured
payload (tests/policy/strategies.py). Facts are permissive unless a test names
the one thing wrong with the world.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from vasool.diagnosis.proposal import MessageCategory, ProposalRole
from vasool.diagnosis.rules import IST, QUIET_HOURS_END_HOUR_IST
from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from vasool.policy.facts import CONSENT_PURPOSE_RECOVERY, ConsentRecord, MerchantPolicy
from vasool.policy.guards.afa_threshold import AFA_THRESHOLD_PAISE, AFAThresholdGuard
from vasool.policy.guards.consent import ConsentGuard
from vasool.policy.guards.contact_window import CONTACT_JITTER_MAX, ContactWindowGuard
from vasool.policy.guards.dlt_template import DLTTemplateGuard
from vasool.policy.guards.dnd import DND_FACT_TTL, DNDGuard
from vasool.policy.guards.frequency_cap import (
    EPISODE_CONTACT_CAP,
    FREQUENCY_CAP_WINDOW,
    FrequencyCapGuard,
)
from vasool.policy.guards.human_approval import HumanApprovalGuard
from vasool.policy.guards.idempotency import IdempotencyGuard
from vasool.policy.guards.pre_debit_notice import (
    PRE_DEBIT_NOTICE_LEAD,
    PreDebitNoticeGuard,
)
from vasool.policy.guards.promise_to_pay import PromiseToPayGuard
from vasool.policy.guards.retry_cap import (
    MANDATE_ATTEMPT_CAP,
    ONETIME_ATTEMPT_CAP,
    RetryCapGuard,
)
from vasool.policy.guards.risk_block import RiskBlockGuard
from vasool.policy.guards.spend_cap import SpendCapGuard
from vasool.policy.verdict import Decision, ObligationKind
from tests.policy.strategies import (
    POOL_NOW,
    context,
    permissive_facts,
    proposal_for,
    proposals_for,
)

I = InterventionType
D = Decision


# ---------------------------------------------------------------------------
# 1. IdempotencyGuard — one execution per (payment, intervention, attempt, role)
# ---------------------------------------------------------------------------
class TestIdempotencyGuard:
    guard = IdempotencyGuard()

    def test_an_unexecuted_action_passes(self):
        p = proposal_for("card_expired")
        assert self.guard.evaluate(context(p)).decision is D.ALLOW

    def test_an_executed_action_is_blocked(self):
        p = proposal_for("card_expired")
        ctx = context(p, executed_keys=frozenset({p.idempotency_key}))
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_a_duplicate_webhook_delivery_cannot_execute_twice(self):
        """A01, and the normal case rather than the adversarial one: every
        webhook observed live arrived at least twice with an identical event id
        (docs/VERIFIED.md). The event plane dedupes on event_id; this is the
        backstop for the delivery that gets past it."""
        p = proposal_for("card_expired")
        first = context(p)
        assert self.guard.evaluate(first).decision is D.ALLOW
        second = context(p, executed_keys=frozenset({p.idempotency_key}))
        assert self.guard.evaluate(second).decision is D.BLOCK

    def test_a_replay_with_a_fresh_event_id_is_still_a_duplicate(self):
        """A02, the one the spec's (event_id, intervention) key cannot catch."""
        p = proposal_for("card_expired")
        replayed = p.model_copy(update={"event_id": "evt_brand_new"})
        ctx = context(replayed, executed_keys=frozenset({p.idempotency_key}))
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_a_later_attempt_is_not_a_duplicate_of_an_earlier_one(self):
        """gateway_technical_error gets three silent retries for one event. The
        spec's key would refuse the second as a duplicate of the first, turning
        a three-retry row into a one-retry row without anyone noticing."""
        first = proposal_for("gateway_technical_error", attempt=1)
        second = proposal_for("gateway_technical_error", attempt=2)
        ctx = context(second, executed_keys=frozenset({first.idempotency_key}))
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_a_nudge_is_not_a_duplicate_of_its_retry(self):
        retry, nudge = proposals_for("insufficient_fund")
        ctx = context(nudge, executed_keys=frozenset({retry.idempotency_key}))
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_it_cites_no_statute(self):
        """Dedupe is not a compliance rule and must not appear in the report
        card's statute column."""
        assert self.guard.statute is None


# ---------------------------------------------------------------------------
# 2. RiskBlockGuard — nothing automated, ever
# ---------------------------------------------------------------------------
class TestRiskBlockGuard:
    guard = RiskBlockGuard()

    def test_it_has_no_jurisdiction_over_an_ordinary_failure(self):
        v = self.guard.evaluate(context(proposal_for("card_expired")))
        assert v.decision is D.NOT_APPLICABLE

    def test_a_human_queue_handoff_is_permitted(self):
        """The one thing §2 allows: hand it to an operator and do nothing else."""
        p = proposal_for("payment_risk_check_failed")
        assert p.intervention is I.HUMAN_QUEUE
        assert self.guard.evaluate(context(p)).decision is D.ALLOW

    @pytest.mark.parametrize(
        "intervention", [I.SILENT_RETRY, I.TIMED_RETRY, I.REATTEMPT_LINK, I.REAUTH_LINK]
    )
    def test_every_other_action_on_a_risk_block_is_refused(self, intervention):
        """The containment story. The taxonomy already routes RISK_BLOCK to a
        human queue, so this guard only ever fires against something that
        proposed otherwise — which in Session 7 is the LLM."""
        p = proposal_for("payment_risk_check_failed").model_copy(
            update={"intervention": intervention}
        )
        assert self.guard.evaluate(context(p)).decision is D.BLOCK

    def test_it_covers_the_business_sourced_row_too(self):
        """§6.3's property test keys on error_reason == payment_risk_check_failed
        and so misses payment_failed/business, which is the *other* row routed to
        RISK_BLOCK. Keying on the class covers both."""
        p = proposal_for("payment_failed", "business")
        assert p.failure_class is FailureClass.RISK_BLOCK
        retried = p.model_copy(update={"intervention": I.SILENT_RETRY})
        assert self.guard.evaluate(context(retried)).decision is D.BLOCK

    def test_outbound_contact_is_refused_even_when_the_intervention_is_a_queue(self):
        """§2's fourth ground: an unexpected payment link to a customer whose
        card may be compromised is structurally a phishing message. Zero
        outbound means zero, including a message that arrives attached to an
        otherwise-permitted handoff."""
        p = proposal_for("payment_risk_check_failed").model_copy(
            update={"role": ProposalRole.NUDGE}
        )
        assert self.guard.evaluate(context(p)).decision is D.BLOCK


# ---------------------------------------------------------------------------
# 3. ConsentGuard — DPDP
# ---------------------------------------------------------------------------
class TestConsentGuard:
    guard = ConsentGuard()

    def test_live_consent_for_the_right_purpose_permits_contact(self):
        assert self.guard.evaluate(context(proposal_for("card_expired"))).decision is D.ALLOW

    def test_no_consent_record_blocks(self):
        """Fail closed: no record is not the same as a record saying yes."""
        v = self.guard.evaluate(context(proposal_for("card_expired"), consent=None))
        assert v.decision is D.BLOCK

    def test_consent_for_another_purpose_does_not_cover_recovery(self):
        """Purpose limitation is the whole point of the regime — a record
        listing only marketing does not authorise a dunning message."""
        marketing = ConsentRecord(granted_at=POOL_NOW, purposes=frozenset({"marketing"}))
        ctx = context(proposal_for("card_expired"), consent=marketing)
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_withdrawn_consent_blocks_contact(self):
        withdrawn = ConsentRecord(
            granted_at=POOL_NOW - timedelta(days=30),
            purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
            withdrawn_at=POOL_NOW - timedelta(days=1),
        )
        ctx = context(proposal_for("card_expired"), consent=withdrawn)
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_withdrawn_consent_blocks_a_silent_retry_too(self):
        """A12. A retry sends nothing and rests on the mandate rather than on
        consent to be messaged, so a narrow consent does not stop it — but a
        withdrawal is a stop signal for the whole relationship, and the spec
        requires purging the queue rather than merely muting it."""
        withdrawn = ConsentRecord(
            granted_at=POOL_NOW - timedelta(days=30),
            purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
            withdrawn_at=POOL_NOW - timedelta(days=1),
        )
        ctx = context(proposal_for("gateway_technical_error"), consent=withdrawn)
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_a_narrow_consent_still_permits_a_silent_retry(self):
        """The distinction worth being careful about: re-presenting an
        instrument the customer already authorised is not a communication, and
        blocking it for want of *messaging* consent would refuse the one
        intervention that needs no permission to be polite."""
        marketing = ConsentRecord(granted_at=POOL_NOW, purposes=frozenset({"marketing"}))
        ctx = context(proposal_for("gateway_technical_error"), consent=marketing)
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_withdrawal_in_the_future_does_not_block_today(self):
        later = ConsentRecord(
            granted_at=POOL_NOW - timedelta(days=30),
            purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
            withdrawn_at=POOL_NOW + timedelta(days=5),
        )
        ctx = context(proposal_for("card_expired"), consent=later)
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_withdrawal_is_judged_at_execution_time_not_decision_time(self):
        """A12 again, in its subtle form. An action decided before the
        withdrawal and executing after it must not go out."""
        p = proposal_for("card_expired")
        withdrawn = ConsentRecord(
            granted_at=POOL_NOW - timedelta(days=30),
            purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
            withdrawn_at=POOL_NOW + timedelta(hours=1),
        )
        ctx = context(
            p, now=POOL_NOW, effective_at=POOL_NOW + timedelta(hours=2), consent=withdrawn
        )
        assert self.guard.evaluate(ctx).decision is D.BLOCK


# ---------------------------------------------------------------------------
# 4. RetryCapGuard — the platform's ceiling, not the taxonomy's budget
# ---------------------------------------------------------------------------
class TestRetryCapGuard:
    guard = RetryCapGuard()

    def test_it_has_no_jurisdiction_over_a_contact(self):
        v = self.guard.evaluate(context(proposal_for("card_expired")))
        assert v.decision is D.NOT_APPLICABLE

    def test_a_nudge_does_not_count_as_a_retry(self):
        _, nudge = proposals_for("insufficient_fund")
        assert self.guard.evaluate(context(nudge)).decision is D.NOT_APPLICABLE

    def test_a_fresh_entity_may_retry(self):
        ctx = context(proposal_for("gateway_technical_error"), attempts_used=0)
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_a_one_time_payment_stops_at_three(self):
        p = proposal_for("gateway_technical_error")
        assert (
            self.guard.evaluate(
                context(p, is_mandate=False, attempts_used=ONETIME_ATTEMPT_CAP - 1)
            ).decision
            is D.ALLOW
        )
        assert (
            self.guard.evaluate(
                context(p, is_mandate=False, attempts_used=ONETIME_ATTEMPT_CAP)
            ).decision
            is D.BLOCK
        )

    def test_a_mandate_stops_at_four(self):
        p = proposal_for("gateway_technical_error")
        assert (
            self.guard.evaluate(
                context(p, is_mandate=True, attempts_used=MANDATE_ATTEMPT_CAP - 1)
            ).decision
            is D.ALLOW
        )
        assert (
            self.guard.evaluate(
                context(p, is_mandate=True, attempts_used=MANDATE_ATTEMPT_CAP)
            ).decision
            is D.BLOCK
        )

    def test_the_mandate_cap_is_the_looser_one(self):
        """A one-time payment gets the stricter cap on purpose: nothing halts on
        our behalf, so the restraint has to be ours."""
        assert ONETIME_ATTEMPT_CAP < MANDATE_ATTEMPT_CAP

    def test_it_is_a_ceiling_not_the_taxonomy_budget(self):
        """Two different numbers, both real. card_expired gets zero retries from
        §4; this guard would allow three. The taxonomy's budget is the tighter
        one and it is applied first, in classify(). This guard exists so that
        nothing — an LLM, a bug, a hand-built proposal — can exceed what the
        platform itself will tolerate."""
        ctx = context(proposal_for("gateway_technical_error", attempt=1), attempts_used=0)
        assert self.guard.evaluate(ctx).decision is D.ALLOW


# ---------------------------------------------------------------------------
# 5. PromiseToPayGuard — the spec's QuietPeriodGuard, renamed
# ---------------------------------------------------------------------------
class TestPromiseToPayGuard:
    guard = PromiseToPayGuard()

    def test_no_promise_means_nothing_to_honour(self):
        """The fact is known-absent, not unknown. A guard that blocked here
        would refuse every action for want of a promise nobody made."""
        ctx = context(proposal_for("card_expired"), promise_to_pay=None)
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_an_active_promise_defers_contact(self):
        ctx = context(
            proposal_for("card_expired"), promise_to_pay=(POOL_NOW + timedelta(days=3)).date()
        )
        v = self.guard.evaluate(ctx)
        assert v.decision is D.DEFER
        assert v.defer_until > ctx.effective_at

    def test_an_active_promise_defers_a_silent_retry_too(self):
        """The two source documents disagree: the guard table says "no contact
        during an active promise", the stopping-rule table says "hard stop".
        Debiting on the 3rd someone who promised to pay by the 5th is exactly
        the bad faith the rule exists to prevent, so the broader reading wins."""
        ctx = context(
            proposal_for("gateway_technical_error"),
            promise_to_pay=(POOL_NOW + timedelta(days=3)).date(),
        )
        assert self.guard.evaluate(ctx).decision is D.DEFER

    def test_a_promise_does_not_hold_a_human_queue(self):
        proposal = proposal_for("payment_risk_check_failed")
        ctx = context(
            proposal,
            promise_to_pay=(POOL_NOW + timedelta(days=3)).date(),
        )
        assert self.guard.evaluate(ctx).decision is D.NOT_APPLICABLE

    def test_a_lapsed_promise_no_longer_holds(self):
        ctx = context(
            proposal_for("card_expired"), promise_to_pay=(POOL_NOW - timedelta(days=5)).date()
        )
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_the_hold_lifts_the_day_after_the_promised_date(self):
        promised = date(2026, 9, 5)
        ctx = context(proposal_for("card_expired"), promise_to_pay=promised)
        lifted = self.guard.evaluate(ctx).defer_until.astimezone(IST)
        assert lifted.date() == date(2026, 9, 6)
        assert (lifted.hour, lifted.minute) == (0, 0)

    def test_it_defers_rather_than_blocks(self):
        """A promise is the strongest signal of intent in the system. Killing
        the recovery because the customer said they would pay would be the
        single most perverse outcome available."""
        ctx = context(
            proposal_for("card_expired"), promise_to_pay=(POOL_NOW + timedelta(days=2)).date()
        )
        assert self.guard.evaluate(ctx).decision is not D.BLOCK


# ---------------------------------------------------------------------------
# 6. DNDGuard — TRAI, promotional only
# ---------------------------------------------------------------------------
class TestDNDGuard:
    guard = DNDGuard()

    def promo(self, reason: str = "card_expired"):
        return proposal_for(reason).model_copy(
            update={"message_category": MessageCategory.PROMOTIONAL}
        )

    def test_a_transactional_message_is_out_of_scope(self):
        """The registry governs promotional traffic. Whether a recovery message
        counts as transactional is the uncertain part, and it is recorded on
        MessageCategory rather than decided here."""
        v = self.guard.evaluate(context(proposal_for("card_expired")))
        assert v.decision is D.NOT_APPLICABLE

    def test_a_silent_retry_is_out_of_scope(self):
        v = self.guard.evaluate(context(proposal_for("gateway_technical_error")))
        assert v.decision is D.NOT_APPLICABLE

    def test_a_listed_customer_blocks_promotional_traffic(self):
        ctx = context(self.promo(), dnd_listed=True, dnd_checked_at=POOL_NOW)
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_an_unlisted_customer_passes(self):
        ctx = context(self.promo(), dnd_listed=False, dnd_checked_at=POOL_NOW)
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_an_unscrubbed_customer_blocks(self):
        """Fail closed. In production this fact is a network call, and a call
        that failed must not read as a clean scrub."""
        ctx = context(self.promo(), dnd_listed=None, dnd_checked_at=None)
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_a_stale_scrub_blocks(self):
        """A registration made last week is not answered by a scrub from last
        month, and the whole value of the check is that it is current."""
        ctx = context(
            self.promo(),
            dnd_listed=False,
            dnd_checked_at=POOL_NOW - DND_FACT_TTL - timedelta(seconds=1),
        )
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_a_scrub_inside_the_ttl_is_trusted(self):
        ctx = context(
            self.promo(), dnd_listed=False, dnd_checked_at=POOL_NOW - DND_FACT_TTL / 2
        )
        assert self.guard.evaluate(ctx).decision is D.ALLOW


# ---------------------------------------------------------------------------
# 7. FrequencyCapGuard — two rules, two outcomes
# ---------------------------------------------------------------------------
class TestFrequencyCapGuard:
    guard = FrequencyCapGuard()

    def test_a_silent_retry_is_out_of_scope(self):
        v = self.guard.evaluate(context(proposal_for("gateway_technical_error")))
        assert v.decision is D.NOT_APPLICABLE

    def test_a_customer_we_have_never_messaged_passes(self):
        ctx = context(proposal_for("card_expired"), contact_history=())
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_the_seven_day_cap_defers(self):
        """This condition expires: the oldest contact ages out of the window on
        a date we can compute, so there is a real instant to name."""
        history = tuple(POOL_NOW - timedelta(days=d) for d in (5, 3, 1))
        ctx = context(proposal_for("card_expired"), contact_history=history)
        v = self.guard.evaluate(ctx)
        assert v.decision is D.DEFER
        assert v.defer_until == POOL_NOW - timedelta(days=5) + FREQUENCY_CAP_WINDOW

    def test_one_under_the_seven_day_cap_passes(self):
        history = tuple(POOL_NOW - timedelta(days=d) for d in (5, 1))
        ctx = context(proposal_for("card_expired"), contact_history=history)
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_contacts_older_than_the_window_do_not_count(self):
        history = tuple(
            POOL_NOW - timedelta(days=d) for d in (40, 30, 20, 1)
        )
        ctx = context(proposal_for("card_expired"), contact_history=history)
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_the_episode_cap_blocks_rather_than_defers(self):
        """The distinction the whole defer-vs-block rule turns on. An episode
        does not get shorter with time, so there is no instant at which this
        condition expires — deferring it would be deferring forever."""
        ctx = context(proposal_for("card_expired"), episode_contacts=EPISODE_CONTACT_CAP)
        v = self.guard.evaluate(ctx)
        assert v.decision is D.BLOCK

    def test_the_episode_cap_is_two(self):
        """§4's insufficient_fund row spends exactly this: one nudge, then one
        link after three failed attempts. The row is built to sit at the cap
        rather than to breach it."""
        assert EPISODE_CONTACT_CAP == 2

    def test_the_episode_cap_wins_over_the_window_cap(self):
        """Both fire; the guard returns one verdict, and the one that can never
        expire has to be it, or a dead action gets scheduled."""
        history = tuple(POOL_NOW - timedelta(days=d) for d in (5, 3, 1))
        ctx = context(
            proposal_for("card_expired"),
            contact_history=history,
            episode_contacts=EPISODE_CONTACT_CAP,
        )
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_a_contact_scheduled_in_the_future_does_not_count_against_us(self):
        ctx = context(
            proposal_for("card_expired"),
            contact_history=tuple(POOL_NOW + timedelta(days=d) for d in (1, 2, 3)),
        )
        assert self.guard.evaluate(ctx).decision is D.ALLOW


# ---------------------------------------------------------------------------
# 8. ContactWindowGuard — 08:00-19:00 IST, and the reason the pitch says 8:02
# ---------------------------------------------------------------------------
class TestContactWindowGuard:
    guard = ContactWindowGuard()

    def at(self, hour: int, minute: int = 0, reason: str = "card_expired"):
        when = datetime(2026, 8, 25, hour, minute, tzinfo=IST).astimezone(timezone.utc)
        return context(proposal_for("card_expired"), now=when, effective_at=when)

    def test_a_silent_retry_is_out_of_scope(self):
        """Nothing is sent. The rule protects a person from being disturbed,
        and a re-presentation disturbs nobody."""
        v = self.guard.evaluate(context(proposal_for("gateway_technical_error")))
        assert v.decision is D.NOT_APPLICABLE

    def test_midday_passes(self):
        assert self.guard.evaluate(self.at(12)).decision is D.ALLOW

    def test_the_window_opens_at_eight(self):
        assert self.guard.evaluate(self.at(7, 59)).decision is D.DEFER
        assert self.guard.evaluate(self.at(8, 0)).decision is D.ALLOW

    def test_the_window_closes_at_nineteen(self):
        assert self.guard.evaluate(self.at(18, 59)).decision is D.ALLOW
        assert self.guard.evaluate(self.at(19, 0)).decision is D.DEFER

    def test_an_evening_nudge_becomes_a_morning_nudge(self):
        """The sentence on the slide, as an assertion: 7:30pm does not die, it
        moves to just after eight."""
        v = self.guard.evaluate(self.at(19, 30))
        landed = v.defer_until.astimezone(IST)
        assert landed.date() == date(2026, 8, 26)
        assert landed.hour == 8

    def test_an_early_morning_nudge_waits_for_the_same_day(self):
        v = self.guard.evaluate(self.at(3, 0))
        landed = v.defer_until.astimezone(IST)
        assert landed.date() == date(2026, 8, 25)
        assert landed.hour == 8

    def test_the_deferral_lands_inside_the_window(self):
        for hour in (0, 3, 7, 19, 22, 23):
            landed = self.guard.evaluate(self.at(hour)).defer_until.astimezone(IST)
            assert 8 <= landed.hour < 19

    def test_it_is_evaluated_at_execution_time_not_decision_time(self):
        """A04. Queued at 18:58, executes at 19:02. Checking the decision time
        would pass it, and the message would land outside the window — which is
        why GuardContext carries both times and every rule here reads the
        second one."""
        decided = datetime(2026, 8, 25, 18, 58, tzinfo=IST).astimezone(timezone.utc)
        lands = datetime(2026, 8, 25, 19, 2, tzinfo=IST).astimezone(timezone.utc)
        ctx = context(proposal_for("card_expired"), now=decided, effective_at=lands)
        assert self.guard.evaluate(ctx).decision is D.DEFER

    # -- the customer's own clock, not ours ---------------------------------
    # A08 was an open adversary failure until 2026-08-30: the window was
    # evaluated in the merchant's IST, so a customer elsewhere was protected by
    # our clock. The attack proves the fix end to end; these prove the guard's
    # own behaviour, which is faster to read and fails more precisely.

    NYC = timezone(timedelta(hours=-4))
    """Nine and a half hours behind IST — far enough west that the two windows
    barely overlap, which is what makes the arithmetic below legible."""

    def at_zone(self, hour: int, zone, minute: int = 0):
        when = datetime(2026, 8, 25, hour, minute, tzinfo=IST).astimezone(timezone.utc)
        return context(
            proposal_for("card_expired"), now=when, effective_at=when, customer_zone=zone
        )

    def test_an_unknown_zone_still_means_ist(self):
        """The fallback, asserted rather than assumed. Every customer the
        simulator builds has no zone, so this path is the one the entire
        evaluation runs on and a change to it would move published numbers."""
        assert self.guard.evaluate(self.at_zone(12, None)).decision is D.ALLOW
        assert self.guard.evaluate(self.at_zone(3, None)).decision is D.DEFER

    def test_inside_our_window_but_the_middle_of_their_night_defers(self):
        """08:00 IST is 22:30 in New York. Under the old rule this was the
        *destination* an overnight deferral aimed at — the bug, exactly."""
        assert self.guard.evaluate(self.at_zone(8, self.NYC)).decision is D.DEFER

    def test_outside_our_window_but_inside_theirs_allows(self):
        """03:00 IST is 17:30 in New York, which is their afternoon. Deferring
        it would be protecting them from a message they are awake for."""
        assert self.guard.evaluate(self.at_zone(3, self.NYC)).decision is D.ALLOW

    def test_the_deferral_lands_in_the_customers_morning_not_ours(self):
        """The half-fix this guards against: reading the customer's zone to
        decide, then deferring to 08:00 IST anyway. The target has to be in the
        same clock the judgement was made in."""
        v = self.guard.evaluate(self.at_zone(8, self.NYC))
        assert v.decision is D.DEFER
        landed = v.defer_until.astimezone(self.NYC)
        assert 8 <= landed.hour < 19, f"landed at {landed:%H:%M} New York time"

    def test_the_reason_names_the_clock_it_judged_in(self):
        """A receipt that says 'IST' for a decision made in another zone is a
        misleading audit trail, which is worse than a terse one."""
        v = self.guard.evaluate(self.at_zone(8, self.NYC))
        assert "IST" not in v.reason, v.reason

    def test_the_jitter_is_deterministic_for_a_customer(self):
        """No uuid, no random: same seed -> byte-identical ledger. The jitter
        exists so that a merchant's whole overnight backlog does not fire at
        08:00:00.000, and it has to survive replay."""
        ctx = self.at(2)
        assert self.guard.evaluate(ctx).defer_until == self.guard.evaluate(ctx).defer_until

    def test_the_jitter_stays_inside_its_bound(self):
        landed = self.guard.evaluate(self.at(2)).defer_until.astimezone(IST)
        assert timedelta(0) <= (landed - landed.replace(hour=8, minute=0, second=0)) <= (
            CONTACT_JITTER_MAX
        )

    def test_different_customers_do_not_all_fire_at_once(self):
        """Not a property — with a 15-minute spread and two customers a
        collision is possible — but the mechanism has to actually vary."""
        base = proposal_for("card_expired")
        when = datetime(2026, 8, 25, 2, 0, tzinfo=IST).astimezone(timezone.utc)
        landings = {
            self.guard.evaluate(
                context(
                    base.model_copy(update={"customer_id": f"cust_{n}"}),
                    now=when,
                    effective_at=when,
                )
            ).defer_until
            for n in range(25)
        }
        assert len(landings) > 1


# ---------------------------------------------------------------------------
# 9. PreDebitNoticeGuard — defers, and says what it is waiting for
# ---------------------------------------------------------------------------
class TestPreDebitNoticeGuard:
    guard = PreDebitNoticeGuard()

    def mandate(self, **overrides):
        return context(proposal_for("gateway_technical_error"), is_mandate=True, **overrides)

    def test_a_one_time_payment_is_out_of_scope(self):
        ctx = context(proposal_for("gateway_technical_error"), is_mandate=False)
        assert self.guard.evaluate(ctx).decision is D.NOT_APPLICABLE

    def test_a_contact_is_out_of_scope(self):
        """The rule is about debiting an account, not about messaging."""
        ctx = context(proposal_for("card_expired"), is_mandate=True)
        assert self.guard.evaluate(ctx).decision is D.NOT_APPLICABLE

    def test_an_unnoticed_debit_defers_by_the_notice_period(self):
        ctx = self.mandate(pre_debit_notice_sent_at=None)
        v = self.guard.evaluate(ctx)
        assert v.decision is D.DEFER
        assert v.defer_until == ctx.effective_at + PRE_DEBIT_NOTICE_LEAD

    def test_it_returns_an_obligation_rather_than_sending_anything(self):
        """A guard is a pure function; it cannot send a notice. It says one is
        owed and the machine acts — the same discipline the diagnosis plane
        follows one layer up."""
        v = self.guard.evaluate(self.mandate(pre_debit_notice_sent_at=None))
        assert [o.kind for o in v.obligations] == [ObligationKind.SEND_PRE_DEBIT_NOTICE]

    def test_a_notice_served_long_enough_ago_permits_the_debit(self):
        ctx = self.mandate(pre_debit_notice_sent_at=POOL_NOW - timedelta(hours=25))
        assert self.guard.evaluate(ctx).decision is D.ALLOW

    def test_a_notice_served_too_recently_defers_to_the_deadline(self):
        sent = POOL_NOW - timedelta(hours=1)
        ctx = self.mandate(pre_debit_notice_sent_at=sent)
        v = self.guard.evaluate(ctx)
        assert v.decision is D.DEFER
        assert v.defer_until == sent + PRE_DEBIT_NOTICE_LEAD

    def test_a_served_notice_produces_no_further_obligation(self):
        ctx = self.mandate(pre_debit_notice_sent_at=POOL_NOW - timedelta(hours=1))
        assert self.guard.evaluate(ctx).obligations == ()

    def test_the_boundary_is_inclusive(self):
        ctx = self.mandate(pre_debit_notice_sent_at=POOL_NOW - PRE_DEBIT_NOTICE_LEAD)
        assert self.guard.evaluate(ctx).decision is D.ALLOW


# ---------------------------------------------------------------------------
# 10. AFAThresholdGuard — A11's boundary
# ---------------------------------------------------------------------------
class TestAFAThresholdGuard:
    guard = AFAThresholdGuard()

    def at_amount(self, paise: int):
        return context(
            proposal_for("gateway_technical_error").model_copy(update={"amount_paise": paise}),
            is_mandate=True,
        )

    def test_a_one_time_payment_is_out_of_scope(self):
        """AFA is a recurring-mandate requirement. A one-time payment the
        customer is sitting in front of authenticates itself."""
        ctx = context(proposal_for("gateway_technical_error"), is_mandate=False)
        assert self.guard.evaluate(ctx).decision is D.NOT_APPLICABLE

    def test_fifteen_thousand_exactly_passes(self):
        assert self.guard.evaluate(self.at_amount(AFA_THRESHOLD_PAISE)).decision is D.ALLOW

    def test_one_paisa_over_escalates(self):
        """A11, at the boundary the attack names."""
        assert self.guard.evaluate(self.at_amount(AFA_THRESHOLD_PAISE + 1)).decision is (
            D.ESCALATE
        )

    def test_it_escalates_rather_than_blocking(self):
        """The action is not forbidden — it needs a factor we cannot supply
        unattended. Blocking would throw away a recoverable payment over a step
        a human can complete."""
        v = self.guard.evaluate(self.at_amount(AFA_THRESHOLD_PAISE * 10))
        assert v.decision is D.ESCALATE

    def test_the_threshold_is_fifteen_thousand_rupees(self):
        assert AFA_THRESHOLD_PAISE == 15_000 * 100


# ---------------------------------------------------------------------------
# 11. DLTTemplateGuard
# ---------------------------------------------------------------------------
class TestDLTTemplateGuard:
    guard = DLTTemplateGuard()

    def test_a_silent_retry_is_out_of_scope(self):
        v = self.guard.evaluate(context(proposal_for("gateway_technical_error")))
        assert v.decision is D.NOT_APPLICABLE

    def test_a_registered_template_passes(self):
        assert self.guard.evaluate(context(proposal_for("card_expired"))).decision is D.ALLOW

    def test_an_unregistered_template_blocks(self):
        ctx = context(proposal_for("card_expired"), registered_templates=frozenset())
        assert self.guard.evaluate(ctx).decision is D.BLOCK

    def test_a_message_with_no_template_at_all_blocks(self):
        p = proposal_for("card_expired").model_copy(update={"template_id": None})
        assert self.guard.evaluate(context(p)).decision is D.BLOCK

    def test_it_blocks_rather_than_defers(self):
        """A registration could in principle be added, but not on any schedule
        we can name — so there is no instant to defer to, and the rule blocks."""
        ctx = context(proposal_for("card_expired"), registered_templates=frozenset())
        assert self.guard.evaluate(ctx).decision is not D.DEFER


# ---------------------------------------------------------------------------
# 12. SpendCapGuard — merchant blast radius
# ---------------------------------------------------------------------------
class TestSpendCapGuard:
    guard = SpendCapGuard()

    def spending(self, amount: int, spent: int, cap: int = 10_000_000):
        return context(
            proposal_for("gateway_technical_error").model_copy(update={"amount_paise": amount}),
            facts=permissive_facts(
                merchant=MerchantPolicy(merchant_id="acc_test", daily_retry_cap_paise=cap),
                spent_today_paise=spent,
            ),
        )

    def test_a_contact_is_out_of_scope(self):
        """The cap limits value re-presented, not messages sent."""
        assert self.guard.evaluate(context(proposal_for("card_expired"))).decision is (
            D.NOT_APPLICABLE
        )

    def test_a_retry_inside_the_cap_passes(self):
        assert self.guard.evaluate(self.spending(100_000, 0)).decision is D.ALLOW

    def test_a_retry_due_in_quiet_hours_defers_to_six(self):
        quiet_now = POOL_NOW.astimezone(IST).replace(hour=1).astimezone(timezone.utc)
        proposal = proposal_for("gateway_technical_error", now=quiet_now).model_copy(
            update={"execute_at": quiet_now}
        )
        v = self.guard.evaluate(context(proposal, now=quiet_now))
        assert v.decision is D.DEFER
        assert v.defer_until.astimezone(IST).hour == QUIET_HOURS_END_HOUR_IST

    def test_a_retry_that_would_breach_the_cap_defers(self):
        """Deferring, not blocking — the spec says block, but a daily ceiling
        expires at midnight, and a recovery killed because it arrived late in
        the queue on a busy day is lost revenue for no compliance gain."""
        v = self.guard.evaluate(self.spending(100_000, 9_950_000))
        assert v.decision is D.DEFER

    def test_the_deferral_lands_after_the_reset_and_out_of_the_quiet_hours(self):
        """The gap the plane split opens: the classifier holds retries out of
        00:00-06:00 IST at classify time, and a guard deferring to midnight
        would walk an action straight back into the window nothing re-checks."""
        v = self.guard.evaluate(self.spending(100_000, 9_950_000))
        landed = v.defer_until.astimezone(IST)
        assert landed.date() == date(2026, 8, 26)
        assert landed.hour >= QUIET_HOURS_END_HOUR_IST

    def test_a_single_retry_larger_than_the_whole_cap_blocks(self):
        """The edge the defer-iff-it-expires rule catches. Waiting for tomorrow
        does not help a payment that exceeds the entire daily ceiling — it would
        defer every day, forever, which is the failure mode deferral is supposed
        to avoid."""
        v = self.guard.evaluate(self.spending(20_000_000, 0, cap=10_000_000))
        assert v.decision is D.BLOCK

    def test_the_cap_counts_the_proposed_amount_not_just_the_spend_so_far(self):
        assert self.guard.evaluate(self.spending(100_000, 9_999_999)).decision is D.DEFER

    def test_it_cites_no_statute(self):
        assert self.guard.statute is None


# ---------------------------------------------------------------------------
# 13. HumanApprovalGuard — last
# ---------------------------------------------------------------------------
class TestHumanApprovalGuard:
    guard = HumanApprovalGuard()

    def worth(self, paise: int, threshold: int = 5_000_000):
        return context(
            proposal_for("gateway_technical_error").model_copy(update={"amount_paise": paise}),
            facts=permissive_facts(
                merchant=MerchantPolicy(
                    merchant_id="acc_test", human_approval_threshold_paise=threshold
                )
            ),
        )

    def test_an_ordinary_amount_passes(self):
        assert self.guard.evaluate(self.worth(100_000)).decision is D.ALLOW

    def test_the_threshold_itself_passes(self):
        assert self.guard.evaluate(self.worth(5_000_000)).decision is D.ALLOW

    def test_above_the_threshold_escalates(self):
        assert self.guard.evaluate(self.worth(5_000_001)).decision is D.ESCALATE

    def test_an_action_already_bound_for_a_human_is_out_of_scope(self):
        """Escalating something that is already an escalation would queue it
        twice and make the queue depth a lie."""
        p = proposal_for("payment_risk_check_failed").model_copy(
            update={"amount_paise": 99_000_000}
        )
        assert self.guard.evaluate(context(p)).decision is D.NOT_APPLICABLE
