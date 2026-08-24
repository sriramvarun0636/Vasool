"""The policy state machine.

Most of these are named for the adversary scenario they anticipate. The full
harness is stage 9; the point of writing them now is that the state machine is
where those attacks actually land, and a design that cannot survive them is
cheaper to find out about before the executors exist than after.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vasool.clock import VirtualClock
from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import InterventionType
from vasool.policy.episode import State
from vasool.policy.facts import MerchantPolicy, PolicyFacts
from vasool.policy.guards.base import Guard
from vasool.policy.machine import (
    DEFER_HORIZON,
    MAX_CLOCK_SKEW,
    MAX_DEFERRALS,
    PolicyMachine,
    RecordingExecutor,
)
from vasool.policy.registry import GUARD_CHAIN
from vasool.policy.verdict import Verdict
from tests.payloads import event_for
from tests.policy.strategies import permissive_facts

NOON = datetime(2026, 8, 25, 12, 0, tzinfo=IST).astimezone(timezone.utc)
"""Midday IST: inside the contact window, outside the quiet hours, so nothing
defers unless a test asks it to."""


class StubFactStore:
    """The external world, held still. Episode counters are supplied by the
    machine from the episode itself, so this only covers what is genuinely
    outside: consent, DND, templates, merchant config, spend."""

    def __init__(self, **overrides):
        self.overrides = overrides

    def snapshot(self, *, event, proposal, now) -> PolicyFacts:
        return permissive_facts(**self.overrides)


class DefersBy(Guard):
    """A guard that always defers by a fixed amount. Exists to drive the
    machine's deferral bounds, which no real guard can reach — every one of the
    thirteen defers to a condition that actually expires."""

    name = "DefersBy"
    statute = None

    def __init__(self, delta: timedelta):
        self.delta = delta

    def check(self, ctx) -> Verdict:
        return self.defer(ctx.effective_at + self.delta, "test guard, defers forever")


def machine(*, now=NOON, chain=GUARD_CHAIN, **fact_overrides):
    clock = VirtualClock(now)
    return PolicyMachine(
        clock=clock,
        facts=StubFactStore(**fact_overrides),
        executor=RecordingExecutor(),
        chain=chain,
    ), clock


# ---------------------------------------------------------------------------
# the happy path, and the state that the spec's diagram is missing
# ---------------------------------------------------------------------------
class TestScheduling:
    def test_observing_a_failure_schedules_rather_than_executing(self):
        """The spec's FSM goes DIAGNOSED -> GATED -> EXECUTING with no state for
        "diagnosed, waiting for its time". A 48-hour timed retry gated now and
        executed then would put two days between the compliance check and the
        money movement — adversary attack A04, built into the architecture."""
        m, _ = machine()
        m.observe(event_for("gateway_technical_error"))
        assert m.executor.executed == []
        assert len(m.pending()) == 1

    def test_a_due_action_executes(self):
        m, clock = machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        assert len(m.executor.executed) == 1

    def test_an_action_does_not_execute_before_its_time(self):
        m, clock = machine()
        m.observe(event_for("gateway_technical_error"))  # 5m backoff
        clock.advance_by(timedelta(minutes=1))
        m.tick()
        assert m.executor.executed == []

    def test_the_gate_runs_immediately_before_execution(self):
        """A04 closed from the other side: there is no window between the
        compliance decision and the action, because they happen in the same
        tick against the same snapshot."""
        m, clock = machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        gated = [t for t in m.transitions if t.to_state is State.GATED]
        assert gated and gated[-1].chain is not None

    def test_a_liquidity_failure_schedules_two_independent_actions(self):
        """The fan-out arriving in the machine: a retry for payday and a nudge
        for now, gated separately."""
        m, _ = machine()
        m.observe(event_for("insufficient_fund"))
        assert len(m.pending()) == 2

    def test_the_nudge_goes_out_while_the_retry_waits(self):
        m, clock = machine()
        m.observe(event_for("insufficient_fund"))
        m.tick()
        assert [p.role.value for p in m.executor.executed] == ["NUDGE"]
        assert len(m.pending()) == 1


# ---------------------------------------------------------------------------
# deferral: re-entry, and the two bounds
# ---------------------------------------------------------------------------
class TestDeferral:
    def evening_link(self):
        """A re-auth link diagnosed at 19:30 IST — outside the contact window,
        so ContactWindowGuard defers it to just after eight."""
        half_past_seven = datetime(2026, 8, 25, 19, 30, tzinfo=IST).astimezone(timezone.utc)
        return machine(now=half_past_seven)

    def test_an_out_of_hours_contact_defers_rather_than_dying(self):
        m, _ = self.evening_link()
        m.observe(event_for("card_expired"))
        m.tick()
        assert m.executor.executed == []
        assert len(m.pending()) == 1

    def test_the_deferred_action_executes_when_the_window_opens(self):
        """The sentence on the slide, end to end: a 7:30pm nudge becomes an
        8:02am nudge, and the money is still recovered."""
        m, clock = self.evening_link()
        m.observe(event_for("card_expired"))
        m.tick()
        clock.advance_to(datetime(2026, 8, 26, 8, 30, tzinfo=IST).astimezone(timezone.utc))
        m.tick()
        assert len(m.executor.executed) == 1

    def test_the_original_proposal_survives_the_deferral(self):
        """Nothing is lost. Proposals are immutable, so re-entry creates a
        successor that points back at what it replaced."""
        m, _ = self.evening_link()
        m.observe(event_for("card_expired"))
        original = m.pending()[0].proposal
        m.tick()
        successor = m.pending()[0]
        assert successor.origin == original
        assert successor.proposal.supersedes == original.proposal_id
        assert successor.proposal.execute_at > original.execute_at

    def test_each_deferral_records_the_clause_that_caused_it(self):
        m, _ = self.evening_link()
        m.observe(event_for("card_expired"))
        m.tick()
        (cause,) = m.pending()[0].causes
        assert cause.guard == "ContactWindowGuard"
        assert cause.statute == "RBI Fair Practices Code ¶55"

    def test_a_deferred_episode_reports_deferred(self):
        """DEFERRED is where the episode rests, not merely a line in the log.

        The distinction is worth the field: an episode waiting because §4 chose
        a time and one waiting because a guard moved it are different situations,
        and "how much is currently held by compliance" is a number the report
        card needs. Collapsing both into SCHEDULED makes it unrecoverable."""
        m, _ = self.evening_link()
        m.observe(event_for("card_expired"))
        assert m.state_of(event_for("card_expired").entity_id) is State.SCHEDULED
        m.tick()
        assert m.state_of(event_for("card_expired").entity_id) is State.DEFERRED

    def test_the_deferred_state_holds_until_the_action_is_gated_again(self):
        m, clock = self.evening_link()
        m.observe(event_for("card_expired"))
        m.tick()
        clock.advance_by(timedelta(hours=2))
        m.tick()  # still before the window opens
        assert m.state_of(event_for("card_expired").entity_id) is State.DEFERRED

    def test_a_deferral_always_moves_forward(self):
        m, _ = self.evening_link()
        m.observe(event_for("card_expired"))
        before = m.pending()[0].proposal.execute_at
        m.tick()
        assert m.pending()[0].proposal.execute_at > before


class TestDeferralBounds:
    """MAX_DEFERRALS and DEFER_HORIZON, tested rather than merely asserted in a
    comment. Neither is reachable by any of the thirteen — every real guard
    defers to a condition that expires — so both are driven by a stub."""

    def run_to_exhaustion(self, delta: timedelta, ticks: int = 40):
        m, clock = machine(chain=(DefersBy(delta),))
        m.observe(event_for("card_expired"))
        for _ in range(ticks):
            if not m.pending():
                break
            clock.advance_to(m.pending()[0].proposal.execute_at)
            m.tick()
        return m

    def test_repeated_deferral_ends_in_blocked(self):
        m = self.run_to_exhaustion(timedelta(hours=1))
        assert m.state_of(event_for("card_expired").entity_id) is State.BLOCKED
        assert m.executor.executed == []

    def test_it_gives_up_after_the_deferral_budget(self):
        m = self.run_to_exhaustion(timedelta(hours=1))
        deferrals = [t for t in m.transitions if t.to_state is State.DEFERRED]
        assert len(deferrals) == MAX_DEFERRALS

    def test_the_full_cause_chain_survives_into_the_final_transition(self):
        """What the receipt has to show: not "gave up", but every clause that
        was cited on the way to giving up.

        One more than MAX_DEFERRALS, because the verdict that pushed it over the
        budget is itself a citation — the audit trail should say why we stopped,
        not only why we waited five times."""
        m = self.run_to_exhaustion(timedelta(hours=1))
        final = [t for t in m.transitions if t.to_state is State.BLOCKED][-1]
        assert len(final.causes) == MAX_DEFERRALS + 1
        assert all(c.guard == "DefersBy" for c in final.causes)

    def test_a_slow_deferral_dies_on_the_horizon_instead(self):
        """The other bound, and the one that catches death by small increments:
        three three-day deferrals stay well inside the count budget while
        pushing the action nine days past the point at which it was worth
        taking."""
        m = self.run_to_exhaustion(timedelta(days=3))
        assert m.state_of(event_for("card_expired").entity_id) is State.BLOCKED
        deferrals = [t for t in m.transitions if t.to_state is State.DEFERRED]
        assert len(deferrals) < MAX_DEFERRALS

    def test_the_horizon_is_measured_from_the_original_execution_time(self):
        m = self.run_to_exhaustion(timedelta(days=3))
        final = [t for t in m.transitions if t.to_state is State.BLOCKED][-1]
        assert "horizon" in final.note.lower()

    def test_nothing_deferred_ever_executes_past_the_horizon(self):
        m = self.run_to_exhaustion(timedelta(days=3))
        assert m.executor.executed == []


# ---------------------------------------------------------------------------
# adversary scenarios the machine owns
# ---------------------------------------------------------------------------
class TestAdversary:
    def test_a04_an_action_queued_in_hours_and_firing_out_of_hours_defers(self):
        """Queued 18:58, fires 19:02. The gate runs at execution time against
        the execution time, so the message never lands outside the window."""
        m, clock = machine(now=datetime(2026, 8, 25, 18, 58, tzinfo=IST).astimezone(timezone.utc))
        m.observe(event_for("card_expired"))
        clock.advance_to(datetime(2026, 8, 25, 19, 2, tzinfo=IST).astimezone(timezone.utc))
        m.tick()
        assert m.executor.executed == []
        assert len(m.pending()) == 1

    def test_a06_a_second_failure_reclassifies_rather_than_repeating(self):
        """Card expires between attempts. The episode carries the attempt count;
        the new event carries the new reason; classification happens fresh, so
        attempt 2 is a re-auth link rather than another blind retry."""
        m, clock = machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        expired = event_for("card_expired").model_copy(
            update={"entity_id": event_for("gateway_technical_error").entity_id}
        )
        m.observe(expired)
        assert m.pending()[0].proposal.intervention is InterventionType.REAUTH_LINK

    def test_a07_out_of_band_settlement_cancels_everything_in_flight(self):
        """The failure mode taxonomy.md §5 says it would most fear in
        production: the customer pays by bank transfer while a retry is
        scheduled, and nothing tells us except the absence of a webhook we were
        not waiting for. Double collection."""
        m, clock = machine()
        m.observe(event_for("insufficient_fund"))
        m.settled(
            event_for("insufficient_fund").entity_id,
            reason="out-of-band payment",
            amount_paise=event_for("insufficient_fund").amount_paise,
        )
        clock.advance_by(timedelta(days=30))
        m.tick()
        assert m.executor.executed == []
        assert m.pending() == ()

    def test_a12_withdrawal_closes_an_episode_with_nothing_queued(self):
        """Purge *and* stop. An episode whose retry has already fired is sitting
        in AWAITING with an empty queue — there is nothing to purge, so a
        queue-only implementation leaves it open and the next failure webhook
        starts the chase again. DPDP does not have a carve-out for customers
        whose payment happened to fail twice."""
        m, clock = machine()
        event = event_for("gateway_technical_error")
        m.observe(event)
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        assert m.executor.executed and m.pending() == ()

        m.consent_withdrawn(event.customer_id)
        assert m.state_of(event.entity_id) is State.BLOCKED

        m.observe(event)
        assert m.pending() == ()
        assert len(m.executor.executed) == 1

    def test_settlement_closes_an_episode_with_nothing_queued(self):
        """A07's other half. The retry fired, we are waiting on the outcome, and
        the customer pays by bank transfer — the episode has to close, or the
        next failure event reopens a recovery for money already received."""
        m, clock = machine()
        event = event_for("gateway_technical_error")
        m.observe(event)
        clock.advance_by(timedelta(minutes=6))
        m.tick()

        m.settled(event.entity_id, reason="out-of-band payment", amount_paise=event.amount_paise)
        assert m.state_of(event.entity_id) is State.RECOVERED
        m.observe(event)
        assert m.pending() == ()

    def test_settled_amount_lands_on_the_recovered_transition(self):
        """amount_recovered_paise is the headline metric of the whole project
        (CLAUDE.md), and it is only knowable here: settled() is the one call
        that learns what actually landed. The RECOVERED transition is where
        receipts.py has to be able to read it from."""
        m, clock = machine()
        event = event_for("gateway_technical_error")
        m.observe(event)
        clock.advance_by(timedelta(minutes=6))
        m.tick()

        m.settled(event.entity_id, reason="captured", amount_paise=event.amount_paise)

        recovered = [t for t in m.transitions if t.to_state is State.RECOVERED]
        assert len(recovered) == 1
        assert recovered[0].settled_amount_paise == event.amount_paise

    def test_settled_amount_is_recorded_even_with_nothing_ever_executed(self):
        """A07: the customer can pay out of band before a single tick runs, so
        no proposal was ever gated or executed for this episode. settled()
        still has to be able to say how much arrived."""
        m, _ = machine()
        event = event_for("insufficient_fund")
        m.observe(event)

        m.settled(event.entity_id, reason="out-of-band payment", amount_paise=event.amount_paise)

        recovered = [t for t in m.transitions if t.to_state is State.RECOVERED]
        assert len(recovered) == 1
        assert recovered[0].settled_amount_paise == event.amount_paise
        assert recovered[0].proposal is None

    def test_a12_closes_every_episode_for_the_customer(self):
        """Withdrawal is about the person, not the payment."""
        m, _ = machine()
        first = event_for("insufficient_fund")
        second = first.model_copy(update={"entity_id": "pay_another"})
        m.observe(first)
        m.observe(second)

        m.consent_withdrawn(first.customer_id)
        assert m.state_of(first.entity_id) is State.BLOCKED
        assert m.state_of(second.entity_id) is State.BLOCKED

    def test_a12_consent_withdrawal_purges_the_queue(self):
        """DPDP. The spec's stopping rule is "immediate, purge queue" — muting
        the messages while the queue quietly drains is the failure this names."""
        m, clock = machine()
        event = event_for("insufficient_fund")
        m.observe(event)
        assert m.pending()
        m.consent_withdrawn(event.customer_id)
        assert m.pending() == ()
        clock.advance_by(timedelta(days=30))
        m.tick()
        assert m.executor.executed == []

    def test_a14_a_refund_stops_the_chase(self):
        m, clock = machine()
        event = event_for("insufficient_fund")
        m.observe(event)
        m.settled(event.entity_id, reason="refund issued", amount_paise=event.amount_paise)
        clock.advance_by(timedelta(days=30))
        m.tick()
        assert m.executor.executed == []

    def test_a18_an_event_from_the_future_is_not_acted_on(self):
        """Clock skew, or a corrupted payload. Scheduling from a timestamp we do
        not believe is how an agent ends up acting on something that has not
        happened."""
        m, _ = machine()
        event = event_for("card_expired").model_copy(
            update={"occurred_at": NOON + MAX_CLOCK_SKEW + timedelta(hours=1)}
        )
        m.observe(event)
        assert m.pending() == ()
        assert m.state_of(event.entity_id) is State.ESCALATED

    def test_an_event_barely_in_the_future_is_tolerated(self):
        """Clocks disagree by seconds all the time; treating that as an attack
        would reject ordinary traffic."""
        m, _ = machine()
        event = event_for("card_expired").model_copy(
            update={"occurred_at": NOON + timedelta(seconds=30)}
        )
        m.observe(event)
        assert len(m.pending()) == 1


# ---------------------------------------------------------------------------
# terminal states, and the kill switch
# ---------------------------------------------------------------------------
class TestTerminalStates:
    def test_a_blocked_episode_absorbs_further_events(self):
        """A blocked recovery stays blocked. Re-delivering the failure — which
        Razorpay does as a matter of course — must not reopen the chase."""
        m, _ = machine(registered_templates=frozenset())
        event = event_for("card_expired")
        m.observe(event)
        m.tick()
        assert m.state_of(event.entity_id) is State.BLOCKED
        m.observe(event)
        assert m.pending() == ()
        assert m.executor.executed == []

    def test_a_recovered_episode_absorbs_further_events(self):
        m, _ = machine()
        event = event_for("insufficient_fund")
        m.observe(event)
        m.settled(event.entity_id, reason="captured", amount_paise=event.amount_paise)
        m.observe(event)
        assert m.pending() == ()

    def test_a_risk_block_reaches_a_human_and_records_the_restraint(self):
        """taxonomy.md §5: the one path where correct behaviour is
        indistinguishable from being broken, so the log has to show the decision
        not to act."""
        m, _ = machine()
        event = event_for("payment_risk_check_failed")
        m.observe(event)
        m.tick()
        assert m.state_of(event.entity_id) is State.ESCALATED
        assert m.executor.executed == []
        assert any(t.to_state is State.ESCALATED for t in m.transitions)


class TestKillSwitch:
    def test_nothing_executes_while_it_is_on(self):
        m, clock = machine(merchant=MerchantPolicy(merchant_id="acc_test", kill_switch=True))
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(hours=1))
        m.tick()
        assert m.executor.executed == []

    def test_the_queue_survives_so_work_resumes_when_it_is_off(self):
        """Honoured mid-flight, not destructively. A kill switch that emptied
        the queue would turn an operational pause into data loss."""
        m, clock = machine(merchant=MerchantPolicy(merchant_id="acc_test", kill_switch=True))
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(hours=1))
        m.tick()
        assert len(m.pending()) == 1
        m.facts.overrides["merchant"] = MerchantPolicy(merchant_id="acc_test", kill_switch=False)
        m.tick()
        assert len(m.executor.executed) == 1

    def test_holding_does_not_accumulate_transitions(self):
        """A held action is not a decision, and the audit trail is a record of
        decisions. Ticking against a switched-off merchant every minute for a
        week must not bury the one transition that matters under ten thousand
        rows saying nothing happened."""
        m, clock = machine(merchant=MerchantPolicy(merchant_id="acc_test", kill_switch=True))
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(hours=1))
        m.tick()
        settled = len(m.transitions)
        for _ in range(10):
            clock.advance_by(timedelta(minutes=1))
            m.tick()
        assert len(m.transitions) == settled

    def test_it_is_not_one_of_the_thirteen(self):
        """A kill switch is an operability control. Rendering it as a compliance
        verdict would put "the merchant switched us off" in a column of statute
        citations."""
        assert "KillSwitch" not in {g.name for g in GUARD_CHAIN}


# ---------------------------------------------------------------------------
# the audit trail
# ---------------------------------------------------------------------------
class TestTransitionLog:
    def test_every_transition_is_recorded(self):
        m, clock = machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        states = [t.to_state for t in m.transitions]
        assert State.SCHEDULED in states
        assert State.GATED in states
        assert State.EXECUTING in states

    def test_the_log_is_append_only(self):
        m, _ = machine()
        assert not hasattr(m.transitions, "update")
        assert not hasattr(m.transitions, "delete")

    def test_a_gated_transition_carries_all_thirteen_verdicts(self):
        """What makes the receipt worth reading: every clause considered, not
        just the one that happened to decide it."""
        m, clock = machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        gated = [t for t in m.transitions if t.to_state is State.GATED][-1]
        assert len(gated.chain.verdicts) == len(GUARD_CHAIN)


# ---------------------------------------------------------------------------
# the pre-debit notice — docs/taxonomy.md §9.13
# ---------------------------------------------------------------------------
class MandateWorld:
    """A mandate customer, and a world that notices when the notice goes out.

    `StubFactStore` above holds the world still, which is right for every other
    test in this file. It cannot be right for these: `PreDebitNoticeGuard`'s
    entire behaviour is a function of whether a notice has been *sent*, so a
    store that can never learn that would defer the debit forever and the tests
    below would be asserting the defect rather than the fix.

    One object answering what the guards read and recording what the executor
    did, which is the same split `windtunnel/world.py` holds — a fact about the
    world, never a decision about the agent.
    """

    def __init__(self, clock: VirtualClock, **overrides):
        self.clock = clock
        self.overrides = overrides
        self.executed: list = []
        self.notice_sent_at: datetime | None = None

    # -- FactStore
    def snapshot(self, *, event, proposal, now) -> PolicyFacts:
        return permissive_facts(
            is_mandate=True,
            pre_debit_notice_sent_at=self.notice_sent_at,
            **self.overrides,
        )

    # -- Executor
    def execute(self, proposal):
        from vasool.diagnosis.proposal import ProposalRole
        from vasool.policy.machine import ExecutionResult

        self.executed.append(proposal)
        if proposal.role is ProposalRole.PRE_DEBIT_NOTICE:
            self.notice_sent_at = self.clock.now()
        return ExecutionResult(ok=True, detail="recorded")


def mandate_machine(*, now=NOON, chain=GUARD_CHAIN, **overrides):
    clock = VirtualClock(now)
    world = MandateWorld(clock, **overrides)
    return (
        PolicyMachine(clock=clock, facts=world, executor=world, chain=chain),
        clock,
        world,
    )


def _notices(machine_) -> list:
    from vasool.diagnosis.proposal import ProposalRole

    return [
        item
        for item in machine_.pending()
        if item.proposal.role is ProposalRole.PRE_DEBIT_NOTICE
    ]


class TestPreDebitNotice:
    """RBI e-mandate: the customer is notified before a recurring debit.

    The guard cannot send the notice — it is a pure function — so it defers the
    debit and returns an inert `Obligation` saying one is owed. Something has
    to turn that into a Proposal, and until docs/taxonomy.md §9.13 was fixed
    nothing did: `_execute` was the only place obligations were read, and a
    guard can only attach one to a `DEFER`, so the one action that could
    satisfy the guard was the action the guard was blocking.
    """

    def test_only_a_deferral_can_carry_an_obligation(self):
        """Why the machine honours obligations on the deferral path and nowhere
        else. This is a structural fact about the Guard base class, not a
        convention — and it is what makes an obligation loop inside `_execute`
        dead code rather than merely unreached."""
        import inspect

        assert "obligations" in inspect.signature(Guard.defer).parameters
        for constructor in ("allow", "block", "escalate", "not_applicable"):
            assert (
                "obligations" not in inspect.signature(getattr(Guard, constructor)).parameters
            ), constructor

    def test_a_mandate_debit_does_not_execute_before_a_notice_has_been_served(self):
        m, clock, world = mandate_machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        assert [p for p in world.executed if p.is_retry] == []

    def test_the_deferral_creates_the_notice_it_says_is_owed(self):
        m, clock, _ = mandate_machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        assert len(_notices(m)) == 1

    def test_the_notice_is_a_contact_and_spends_no_attempt(self):
        m, clock, _ = mandate_machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        notice = _notices(m)[0].proposal
        assert notice.is_contact and not notice.is_retry

    def test_the_notice_is_gated_like_any_other_contact(self):
        """The guard's own docstring: "a design where an obligation
        short-circuits into an executor is a hole straight through the policy
        plane". A notice owed at 03:00 IST waits for the contact window."""
        m, clock, world = mandate_machine(
            now=datetime(2026, 8, 25, 3, 0, tzinfo=IST).astimezone(timezone.utc)
        )
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(hours=4))  # past the quiet-hours retry hold
        m.tick()
        clock.advance_by(timedelta(minutes=1))
        m.tick()
        assert world.executed == []
        assert m.state_of(event_for("gateway_technical_error").entity_id) is State.DEFERRED

    def test_the_notice_goes_out_and_then_the_debit_does(self):
        m, clock, world = mandate_machine()
        event = event_for("gateway_technical_error")
        m.observe(event)
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        for _ in range(4):
            clock.advance_by(timedelta(hours=12))
            m.tick()
        roles = [p.role.value for p in world.executed]
        assert roles[0] == "PRE_DEBIT_NOTICE"
        assert any(p.is_retry for p in world.executed), "the debit never happened"
        notice_at = next(p for p in world.executed if not p.is_retry)
        debit = next(p for p in world.executed if p.is_retry)
        assert debit.execute_at >= world.notice_sent_at + timedelta(hours=24)
        assert notice_at is not debit

    def test_exactly_one_notice_is_ever_created(self):
        """The second deferral names a deadline rather than an obligation, so
        the debit re-gating does not mint another notice."""
        from vasool.diagnosis.proposal import ProposalRole

        m, clock, world = mandate_machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        for _ in range(4):
            clock.advance_by(timedelta(hours=12))
            m.tick()
        notices = [
            t
            for t in m.transitions
            if t.proposal is not None
            and t.proposal.role is ProposalRole.PRE_DEBIT_NOTICE
            and t.to_state is State.SCHEDULED
        ]
        assert len(notices) == 1

    def test_a_deferral_the_machine_refuses_creates_no_notice(self):
        """The reason obligations are honoured after the deferral bounds and
        not at the gate. Telling a customer we are about to debit them, for a
        debit we have just refused to reschedule, is worse than saying nothing.
        """
        m, clock, _ = mandate_machine(chain=GUARD_CHAIN + (DefersBy(DEFER_HORIZON + timedelta(days=1)),))
        event = event_for("gateway_technical_error")
        m.observe(event)
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        assert m.state_of(event.entity_id) is State.BLOCKED
        assert _notices(m) == []

    def test_the_notice_starts_with_its_own_deferral_budget(self):
        """It is a new action, not a continuation of the debit. Inheriting the
        debit's deferral count would give a notice owed on the fourth deferral
        a one-deferral budget."""
        m, clock, _ = mandate_machine()
        m.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        m.tick()
        assert _notices(m)[0].deferrals == 0
