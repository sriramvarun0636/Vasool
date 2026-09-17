"""UPI Autopay in the evaluated universe: the rail split, and what it decides.

Registered in docs/EVALUATION.md §10 on 2026-09-16 and built to that row. The
tests here are about the three things the row claims and nothing else: that a
customer's rail decides which mix and which taxonomy table their episodes are
drawn from, that the world's class for an unmapped reason is *absent* rather
than the placeholder the agent's rule carries, and that what a status check
finds was decided before any arm asked.

The registered *values* are checked in tests/windtunnel/test_parameters.py,
against the document rather than against this file.
"""
from __future__ import annotations

import collections

import pytest

from vasool.diagnosis.taxonomy import FailureClass, lookup
from vasool.mandate.record import MandateRail
from vasool.policy.machine import RailStatus
from windtunnel.outcome import OutcomeModel
from windtunnel.parameters import OUTCOME_PARAMETERS, REASON_MIX, UPI_REASON_MIX
from windtunnel.payloads import upi_reasons
from windtunnel.universe import (
    MONEY_MAY_BE_IN_FLIGHT,
    build_universe,
    world_class_for_upi,
)

PEPPER = "test-pepper-do-not-use-in-prod"


@pytest.fixture(scope="module")
def universe():
    outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=0)
    return build_universe(0, pepper=PEPPER, outcome=outcome)


class TestTheMixIsRegistrable:
    def test_the_shares_sum_to_exactly_one(self):
        """`windtunnel/rng.py::choose` refuses a table that does not, which is
        the same rule §3d's mix is held to."""
        assert sum(share for _, share in UPI_REASON_MIX) == pytest.approx(1.0, abs=1e-12)

    def test_every_reason_has_a_stub_on_disk(self):
        """A reason with a share and no envelope is a universe that cannot be
        built. The 61 stubs came with step 1; the mix may only name those."""
        assert {reason for reason, _ in UPI_REASON_MIX} <= upi_reasons()

    def test_the_bucket_split_is_the_one_npci_publishes(self):
        """§10, 2026-09-16: 0.990 of the mass on reasons NPCI would count a
        business decline, 0.010 on technical ones. The allocation inside each
        bucket is a guess; this ratio is not, and it is the only part of the
        table with a source behind it."""
        technical = {
            "psp_bank_not_available",
            "payment_timed_out",
            "banks_hsm_is_down_remitter",
            "response_not_received_within_tat",
        }
        shares = dict(UPI_REASON_MIX)
        assert sum(shares[r] for r in technical) == pytest.approx(0.010)
        assert sum(s for r, s in UPI_REASON_MIX if r not in technical) == pytest.approx(0.990)

    def test_a_reason_on_both_lists_is_classified_by_the_rail(self):
        """Five reason strings are on both of Razorpay's lists, and two of
        them carry a share in both registered mixes. That is not a collision
        to be avoided — it is the case that proves classification is keyed on
        the rail: `payment_timed_out` is TRANSIENT on a card, where §4 retries
        it, and on UPI it is a reason Razorpay documents as possibly having
        moved money, which the taxonomy leaves unmapped and never retries.
        """
        shared = {r for r, _ in REASON_MIX} & {r for r, _ in UPI_REASON_MIX}
        assert shared == {"payment_timed_out", "payment_risk_check_failed"}
        assert lookup("payment_timed_out", "customer")[1].failure_class is FailureClass.TRANSIENT
        assert world_class_for_upi("payment_timed_out") is None
        assert "payment_timed_out" in MONEY_MAY_BE_IN_FLIGHT
        # And one that agrees on both rails, so the point is the rail deciding
        # rather than the two tables always differing.
        assert lookup("payment_risk_check_failed", "business")[1].failure_class is (
            FailureClass.RISK_BLOCK
        )
        assert world_class_for_upi("payment_risk_check_failed") is FailureClass.RISK_BLOCK


class TestTheRailDecidesTheWorld:
    def test_both_rails_are_drawn(self, universe):
        rails = collections.Counter(c.mandate_rail for c in universe.customers)
        assert rails[MandateRail.UPI_AUTOPAY] > 0
        assert rails[MandateRail.CARD] > 0
        assert rails[None] > 0, "most customers hold no mandate at all"

    def test_a_upi_customer_draws_only_upi_reasons(self, universe):
        card_reasons = {r for r, _ in REASON_MIX}
        for episode in universe.episodes:
            if episode.is_upi:
                assert episode.reason in dict(UPI_REASON_MIX)
            else:
                assert episode.reason in card_reasons

    def test_a_upi_failure_carries_no_source(self, universe):
        """Razorpay documents none for the rail, and step 1 made the field
        optional rather than inventing one."""
        for episode in universe.episodes:
            if episode.is_upi:
                assert episode.source is None
                assert episode.event.error_source is None
            else:
                assert episode.source is not None

    def test_the_event_comes_from_the_rails_own_envelope(self, universe):
        upi = next(e for e in universe.episodes if e.is_upi)
        assert upi.event.error_reason == upi.reason
        assert upi.event.method == "upi"


class TestTheUnmappedHaveNoClass:
    def test_the_world_reads_the_mapping_not_the_rule(self):
        """`rule_for_reason` answers what the agent should do, and its Rule
        carries TRANSIENT as a placeholder for an unmapped reason because a
        Diagnosis must carry one of five. The world must not read that as a
        fact: a cap decline is not a gateway blip."""
        from vasool.diagnosis.upi import rule_for_reason

        assert rule_for_reason("per_transaction_limit_exceeded")[1].failure_class is (
            FailureClass.TRANSIENT
        )
        assert world_class_for_upi("per_transaction_limit_exceeded") is None

    def test_a_mapped_reason_keeps_its_class(self):
        assert world_class_for_upi("mandate_expired") is FailureClass.INSTRUMENT_DEAD
        assert world_class_for_upi("insufficient_funds") is FailureClass.LIQUIDITY

    def test_the_registered_mix_reaches_both_kinds(self, universe):
        classes = collections.Counter(e.failure_class for e in universe.episodes if e.is_upi)
        assert classes[None] > 0, "no unmapped reason was drawn"
        assert set(classes) - {None}, "no mapped reason was drawn"

    def test_the_unmapped_share_is_what_the_row_registers(self):
        """0.145 of the mix: the cap declines at 0.140 and the
        money-may-be-in-flight reasons at 0.005."""
        unmapped = sum(
            share for reason, share in UPI_REASON_MIX if world_class_for_upi(reason) is None
        )
        assert unmapped == pytest.approx(0.145)


class TestWhatTheRailAlreadyDid:
    def test_only_a_money_in_flight_reason_can_have_moved_money(self, universe):
        for episode in universe.episodes:
            if episode.debited_in_flight:
                assert episode.is_upi
                assert episode.reason in MONEY_MAY_BE_IN_FLIGHT

    def test_the_draw_does_not_depend_on_the_arm(self):
        """The whole point of deciding it in the universe: an arm that never
        asks must not change what the rail did."""
        outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=7)
        first = build_universe(7, pepper=PEPPER, outcome=outcome)
        again = build_universe(7, pepper=PEPPER, outcome=outcome)
        assert [e.debited_in_flight for e in first.episodes] == [
            e.debited_in_flight for e in again.episodes
        ]

    def test_asking_twice_is_two_questions_not_one_re_rolled(self):
        """OC-215 allows three checks. Each is addressed by its own number, so
        a second check is a fresh answer rather than the first one again."""
        outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=3)
        answers = [outcome.status_answer_pending("pay_x", check=n) for n in (1, 2, 3)]
        assert answers == [outcome.status_answer_pending("pay_x", check=n) for n in (1, 2, 3)]
        # Independent across checks rather than one answer repeated: over many
        # episodes both outcomes appear at every check number, which is what
        # makes a third check ever necessary.
        for check in (1, 2, 3):
            seen = {
                outcome.status_answer_pending(f"pay_{i}", check=check) for i in range(40)
            }
            assert seen == {True, False}

    def test_the_rail_answers_what_the_world_decided(self):
        """The adapter is a reader of the universe, never a decider."""
        from windtunnel.runner import SimulatedStatusCheck
        from windtunnel.world import WorldFactStore

        outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=11)
        universe = build_universe(11, pepper=PEPPER, outcome=outcome)
        world = WorldFactStore(universe=universe)
        check = SimulatedStatusCheck(world=world, outcome=outcome)

        episode = universe.episodes[0]
        proposal = _status_proposal(episode)
        reading = check.check(proposal)
        assert reading.answer in {
            RailStatus.PENDING,
            RailStatus.DEBITED,
            RailStatus.NOT_DEBITED,
        }
        if reading.answer is RailStatus.DEBITED:
            assert episode.debited_in_flight
            assert reading.amount_paise == episode.amount_paise
        if reading.answer is RailStatus.NOT_DEBITED:
            assert not episode.debited_in_flight


def _status_proposal(episode):
    """A status check of this episode's debit, built the way the machine
    builds one."""
    from datetime import timedelta

    from vasool.diagnosis.proposal import (
        Proposal,
        ProposalRole,
        status_check_proposal_from,
    )
    from vasool.diagnosis.taxonomy import InterventionType

    debit = Proposal(
        proposal_id=f"prop_{episode.entity_id}",
        role=ProposalRole.PRIMARY,
        event_id=episode.event.event_id,
        entity_id=episode.entity_id,
        customer_id=episode.customer.customer_id,
        merchant_id="acc_test",
        amount_paise=episode.amount_paise,
        failure_class=FailureClass.TRANSIENT,
        intervention=InterventionType.SILENT_RETRY,
        attempt=1,
        execute_at=episode.arrives_at,
        rationale="a debit, so that its status can be asked after",
    )
    return status_check_proposal_from(
        debit, execute_at=episode.arrives_at + timedelta(seconds=90), check=1
    )
