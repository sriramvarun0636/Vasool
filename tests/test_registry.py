"""The chain: every guard runs, and order does not matter.

The second half is the point. If a shuffled registry produces a different
ruling, then which statute an action is refused under depends on the order
someone happened to list the guards in — and the compliance table becomes an
artefact of the source file rather than of the rules.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vasool.diagnosis.rules import IST
from vasool.policy.registry import GUARD_CHAIN, evaluate_all
from vasool.policy.verdict import Decision
from tests.policy.strategies import (
    context,
    guard_contexts,
    permissive_facts,
    proposal_for,
)

D = Decision


class TestTheChain:
    def test_there_are_thirteen(self):
        assert len(GUARD_CHAIN) == 13

    def test_every_guard_is_named_once(self):
        names = [g.name for g in GUARD_CHAIN]
        assert len(set(names)) == len(names)

    def test_every_guard_reports_a_verdict(self):
        result = evaluate_all(context(proposal_for("card_expired")))
        assert len(result.verdicts) == len(GUARD_CHAIN)

    def test_every_verdict_names_its_own_guard(self):
        result = evaluate_all(context(proposal_for("card_expired")))
        assert [v.guard for v in result.verdicts] == [g.name for g in GUARD_CHAIN]

    def test_a_clean_action_is_allowed(self):
        assert evaluate_all(context(proposal_for("card_expired"))).decision is D.ALLOW


class TestOrderIndependence:
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(ctx=guard_contexts(), seed=st.integers(min_value=0, max_value=10_000))
    def test_a_shuffled_chain_rules_identically(self, ctx, seed):
        """What running all thirteen buys: the decision is a property of the
        rules, not of the order they happen to be listed in."""
        shuffled = list(GUARD_CHAIN)
        random.Random(seed).shuffle(shuffled)
        straight = evaluate_all(ctx)
        crooked = evaluate_all(ctx, tuple(shuffled))
        assert straight.decision is crooked.decision
        assert straight.defer_until == crooked.defer_until
        assert set(straight.statutes()) == set(crooked.statutes())

    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(ctx=guard_contexts())
    def test_the_verdict_set_is_the_same_whatever_the_order(self, ctx):
        shuffled = tuple(reversed(GUARD_CHAIN))
        assert {(v.guard, v.decision) for v in evaluate_all(ctx).verdicts} == {
            (v.guard, v.decision) for v in evaluate_all(ctx, shuffled).verdicts
        }


class TestSpecOrderingBug:
    """The concrete failure the spec's short-circuit ordering produces."""

    def evening_sms_with_an_unregistered_template(self):
        half_past_seven = datetime(2026, 8, 25, 19, 30, tzinfo=IST).astimezone(timezone.utc)
        return context(
            proposal_for("card_expired"),
            now=half_past_seven,
            effective_at=half_past_seven,
            facts=permissive_facts(registered_templates=frozenset()),
        )

    def test_it_is_refused_rather_than_scheduled(self):
        """Under the spec's chain, ContactWindowGuard (#8) defers this to 08:02
        and DLTTemplateGuard (#11) never runs. It wakes up tomorrow morning only
        to be blocked — a deferral spent on an action that was dead when it was
        made."""
        result = evaluate_all(self.evening_sms_with_an_unregistered_template())
        assert result.decision is D.BLOCK
        assert result.defer_until is None

    def test_both_problems_are_recorded_not_just_the_first(self):
        """The audit value: the receipt says the message was out of hours *and*
        unregistered, which is what someone debugging it needs to know."""
        result = evaluate_all(self.evening_sms_with_an_unregistered_template())
        by_guard = {v.guard: v.decision for v in result.verdicts}
        assert by_guard["ContactWindowGuard"] is D.DEFER
        assert by_guard["DLTTemplateGuard"] is D.BLOCK


class TestResolution:
    def test_a_deferral_takes_the_latest_deadline(self):
        """Two guards defer; satisfying the earlier one would wake the action
        into the later one and burn deferral budget for nothing."""
        before_dawn = datetime(2026, 8, 25, 3, 0, tzinfo=IST).astimezone(timezone.utc)
        result = evaluate_all(
            context(
                proposal_for("card_expired"),
                now=before_dawn,
                effective_at=before_dawn,
                promise_to_pay=(before_dawn + timedelta(days=4)).date(),
            )
        )
        assert result.decision is D.DEFER
        # PromiseToPayGuard's deadline is days out; ContactWindowGuard's is
        # hours. The later one governs.
        assert result.defer_until > before_dawn + timedelta(days=3)

    def test_an_escalation_outranks_a_deferral(self):
        """An action heading for a human queue must not first be scheduled for
        unattended execution."""
        before_dawn = datetime(2026, 8, 25, 3, 0, tzinfo=IST).astimezone(timezone.utc)
        big = proposal_for("card_expired").model_copy(update={"amount_paise": 99_000_000})
        result = evaluate_all(context(big, now=before_dawn, effective_at=before_dawn))
        assert result.decision is D.ESCALATE
        assert result.defer_until is None

    def test_a_risk_block_records_the_restraint(self):
        """taxonomy.md §5: the one path where correct behaviour is
        indistinguishable from being broken, so the receipt has to show the
        decision not to act rather than leaving a silence."""
        result = evaluate_all(context(proposal_for("payment_risk_check_failed")))
        assert result.decision is D.ALLOW
        assert any(v.guard == "RiskBlockGuard" for v in result.verdicts)
