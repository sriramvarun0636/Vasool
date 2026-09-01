"""The world an attack manipulates.

The arena is deliberately thin. It decides what happens TO the agent — which
webhooks arrive, when, who the customer really is — and never what the agent
does about it. Everything downstream of the webhook is production's own: the
real FastAPI receiver verifies the signature and dedupes, the real
`PolicyMachine` runs the real thirteen guards against the real §4 table, the
real `RazorpayExecutor` dispatches, and the ledger is built by the real
`build_from_transitions`. The same boundary windtunnel/runner.py holds, for
the same reason — an attack that reached inside the agent would be measuring a
copy of it.

These tests exist to make sure the arena is a faithful world before any attack
rests a finding on it. A harness that silently failed to deliver a webhook
would report a clean sheet.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import InterventionType
from vasool.ledger.receipts import Outcome, verify_chain
from vasool.policy.episode import State
from windtunnel.adversary.arena import Arena
from windtunnel.adversary.criterion import CLAUSE_LEDGER, judge


def _clean_run() -> Arena:
    arena = Arena()
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "gateway_technical_error")
    arena.advance_by(timedelta(minutes=10))
    return arena


class TestDelivery:
    def test_a_delivered_failure_opens_an_episode(self):
        arena = Arena()
        entity_id = arena.fail(arena.person("alice"), "gateway_technical_error")
        assert arena.state_of(entity_id) is State.SCHEDULED

    def test_the_event_reaches_the_store_through_the_real_receiver(self):
        arena = Arena()
        arena.fail(arena.person("alice"), "card_expired")
        assert len(arena.store.all_event_ids()) == 1

    def test_a_duplicate_event_id_is_refused_by_the_receiver(self):
        """docs/VERIFIED.md: every webhook observed live was delivered twice
        with an identical x-razorpay-event-id."""
        arena = Arena()
        alice = arena.person("alice")
        entity_id = arena.fail(alice, "gateway_technical_error", event_id="evt_same")
        again = arena.fail(
            alice, "gateway_technical_error", entity_id=entity_id, event_id="evt_same"
        )
        assert again == entity_id
        assert len(arena.store.all_event_ids()) == 1
        assert len(arena.machine.pending()) == 1

    def test_error_fields_are_never_authored_by_the_arena(self):
        """the project rules: every error_reason comes off disk. A reason with no
        payload must raise rather than be invented."""
        arena = Arena()
        with pytest.raises(Exception):
            arena.fail(arena.person("alice"), "definitely_not_a_razorpay_reason")

    def test_the_classification_matches_the_registered_table(self):
        arena = Arena()
        entity_id = arena.fail(arena.person("alice"), "card_expired")
        queued = [i for i in arena.machine.pending() if i.proposal.entity_id == entity_id]
        assert queued[0].proposal.intervention is InterventionType.REAUTH_LINK


class TestTime:
    def test_nothing_executes_before_its_time(self):
        arena = Arena()
        arena.advance_to(arena.ist(hour=10))
        arena.fail(arena.person("alice"), "gateway_technical_error")  # 5m backoff
        arena.advance_by(timedelta(minutes=1))
        assert arena.dispatched() == ()

    def test_a_due_action_executes(self):
        arena = _clean_run()
        assert len(arena.dispatched()) == 1
        assert arena.dispatched()[0].is_retry

    def test_advancing_ticks_at_every_intermediate_due_time(self):
        """A single jump to the end of a ladder must not skip the rungs."""
        arena = Arena()
        alice = arena.person("alice")
        arena.advance_to(arena.ist(hour=10))
        entity_id = arena.fail(alice, "gateway_technical_error")
        arena.advance_by(timedelta(minutes=10))
        arena.fail_last_retry(entity_id)
        arena.advance_by(timedelta(days=1))
        assert len(arena.dispatched()) == 2, "the 30m rung fired inside the jump"

    def test_the_clock_is_virtual_and_starts_at_the_epoch(self):
        assert Arena().now() == Arena.EPOCH


class TestTheLadder:
    def test_a_failed_retry_advances_the_episode_rather_than_opening_a_new_one(self):
        """Production's own correlation: createRecurring makes a new payment,
        so the follow-up webhook names an id the policy plane has never seen
        and RetryIndex is what resolves it back."""
        arena = Arena()
        alice = arena.person("alice")
        arena.advance_to(arena.ist(hour=10))
        entity_id = arena.fail(alice, "gateway_technical_error")
        arena.advance_by(timedelta(minutes=10))
        arena.fail_last_retry(entity_id)
        assert arena.state_of(entity_id) is not None
        assert {d.entity_id for d in arena.dispatched()} == {entity_id}

    def test_a_link_we_sent_being_paid_settles_the_episode(self):
        arena = Arena()
        alice = arena.person("alice")
        arena.advance_to(arena.ist(hour=10))
        entity_id = arena.fail(alice, "card_expired")
        arena.advance_by(timedelta(minutes=5))
        arena.pay_link(entity_id)
        assert arena.state_of(entity_id) is State.RECOVERED

    def test_an_out_of_band_payment_correlates_to_nothing(self):
        """docs/taxonomy.md §9.9: no vasool_entity_id, no RetryIndex entry, so
        settle_from_webhook correctly declines to attribute it."""
        arena = Arena()
        alice = arena.person("alice")
        arena.advance_to(arena.ist(hour=10))
        entity_id = arena.fail(alice, "gateway_technical_error")
        assert arena.pay_out_of_band(entity_id) is False
        assert arena.state_of(entity_id) is not State.RECOVERED


class TestWorldFacts:
    def test_a_promise_to_pay_reaches_the_guard(self):
        arena = Arena()
        alice = arena.person("alice")
        arena.advance_to(arena.ist(hour=10))
        entity_id = arena.fail(alice, "gateway_technical_error")
        arena.promise(entity_id, arena.ist(day=4).date())
        arena.advance_by(timedelta(minutes=10))
        assert arena.state_of(entity_id) is State.DEFERRED

    def test_a_withdrawal_purges_the_queue_and_closes_the_episode(self):
        arena = Arena()
        alice = arena.person("alice")
        arena.advance_to(arena.ist(hour=10))
        entity_id = arena.fail(alice, "insufficient_fund")
        arena.withdraw_consent(alice)
        assert arena.state_of(entity_id) is State.BLOCKED
        assert arena.machine.pending() == ()

    def test_the_merchant_kill_switch_holds_work_without_discarding_it(self):
        arena = Arena()
        alice = arena.person("alice")
        arena.advance_to(arena.ist(hour=10))
        arena.fail(alice, "gateway_technical_error")
        arena.set_merchant(kill_switch=True)
        arena.advance_by(timedelta(minutes=10))
        assert arena.dispatched() == ()
        assert len(arena.machine.pending()) == 1

    def test_two_people_with_the_same_phone_get_different_customer_ids(self):
        """The identity split A07 rests on — derive_customer_id keys on
        contact+email, so one human with two emails is two customers."""
        arena = Arena()
        one = arena.person("rahul", email="rahul@example.com")
        two = arena.person("rahul", email="r.kumar@example.com", contact=one.contact)
        assert one.human_id == two.human_id
        assert one.customer_id != two.customer_id


class TestTheRecord:
    def test_the_ledger_verifies(self):
        assert verify_chain(list(_clean_run().ledger()))

    def test_the_world_records_dispatches_independently_of_the_ledger(self):
        """§2a's evidence is the ledger; the arena keeps its own record from
        inside the executor seam so that the two agreeing is a finding rather
        than an assumption."""
        arena = _clean_run()
        in_ledger = {r.proposal.proposal_id for r in arena.ledger() if r.executed}
        assert in_ledger == {d.proposal_id for d in arena.dispatched() if d.ok}

    def test_an_ordinary_run_satisfies_the_criterion(self):
        assert judge(_clean_run(), attack_id="ARENA", evidence=()).survived

    def test_a_mark_records_the_moment_it_was_stamped(self):
        arena = Arena()
        arena.advance_to(arena.ist(hour=10))
        arena.mark("here")
        assert arena.mark_at("here") == arena.now()

    def test_the_receipt_for_an_escalation_names_the_guard_that_escalated(self):
        arena = Arena()
        arena.advance_to(arena.ist(hour=10))
        entity_id = arena.fail(arena.person("alice"), "payment_risk_check_failed")
        arena.advance_by(timedelta(minutes=1))
        escalations = [
            r for r in arena.ledger()
            if r.entity_id == entity_id and r.outcome is Outcome.ESCALATED
        ]
        assert escalations, "restraint has to be visible in the ledger (taxonomy §5)"


class TestDeterminism:
    def test_the_same_script_produces_a_byte_identical_ledger(self):
        """architectural invariant 5. The adversary runs in virtual time like
        everything else, so an attack is as replayable as a seed."""
        assert _clean_run().ledger_digest() == _clean_run().ledger_digest()
