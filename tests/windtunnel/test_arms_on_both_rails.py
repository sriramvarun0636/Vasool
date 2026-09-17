"""An arm is a policy on both rails — docs/EVALUATION.md §10, 2026-09-17.

An arm is an edit to §4's table, and §4's table never classifies a UPI payment.
For one re-run that meant every baseline and every ablation classified a UPI
failure exactly as Vasool does, so on 0.175 of episodes the comparison compared
nothing. These tests hold the fix to what the row registered: which arms are
identities, what each of the others becomes, and — the one that matters most —
that the transformation cannot leak into an arm that is supposed to be
untouched.
"""
from __future__ import annotations

import pytest

from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from vasool.diagnosis.upi import rule_for_reason
from windtunnel.arms import ALL_ARMS, arm_named

# One reason per class the registered UPI mix actually draws.
MANDATE_DEAD = "mandate_expired"          # INSTRUMENT_DEAD, 0.27 of the mix
SHORT_BALANCE = "insufficient_funds"      # LIQUIDITY, 0.52
CAP_DECLINE = "per_transaction_limit_exceeded"  # unmapped, no class among the five
IN_FLIGHT = "payment_timed_out"           # unmapped, money may have moved

IDENTITY_ARMS = ("vasool", "vasool_ungated", "A4")
"""§5.3's arm removes guards and §8's A4 changes chain resolution; neither is a
taxonomy change, so neither has anything to say about a second table."""


def registered(reason: str):
    return rule_for_reason(reason)[1]


class TestTheIdentitiesAreIdentities:
    """The registered negative, and the one worth the most: an arm whose
    definition is not about a table must classify a UPI failure exactly as the
    table wrote it, or re-run #5's byte-identical expectation is false before
    it runs."""

    @pytest.mark.parametrize("name", IDENTITY_ARMS)
    @pytest.mark.parametrize("reason", [MANDATE_DEAD, SHORT_BALANCE, CAP_DECLINE, IN_FLIGHT])
    def test_the_rule_is_returned_unchanged(self, name: str, reason: str):
        rule = registered(reason)
        assert arm_named(name).upi_rule(rule) == rule


class TestTheBaselinesRetryOnBothRails:
    """§5.1 is "retry everything regardless of reason". A rail on which it
    retries nothing is a different arm."""

    @pytest.mark.parametrize("name", ("naive_retry", "retry_plus_contact"))
    @pytest.mark.parametrize("reason", [MANDATE_DEAD, SHORT_BALANCE, CAP_DECLINE, IN_FLIGHT])
    def test_every_upi_reason_gets_the_same_retry_budget(self, name: str, reason: str):
        rule = arm_named(name).upi_rule(registered(reason))
        assert rule.retry_budget == 3
        assert rule.retry_intervention is InterventionType.SILENT_RETRY
        assert rule.failure_class is FailureClass.TRANSIENT

    def test_only_the_incumbent_contacts_afterwards(self):
        naive = arm_named("naive_retry").upi_rule(registered(MANDATE_DEAD))
        incumbent = arm_named("retry_plus_contact").upi_rule(registered(MANDATE_DEAD))
        assert naive.post_retry is None
        assert incumbent.post_retry is InterventionType.REATTEMPT_LINK

    def test_the_baselines_retry_what_the_taxonomy_refuses(self):
        """The whole point: §12 gives a cap decline and a money-may-be-in-flight
        reason no retry at all, and an arm with no taxonomy presents them
        again."""
        for reason in (CAP_DECLINE, IN_FLIGHT):
            assert registered(reason).retry_budget == 0
            assert arm_named("naive_retry").upi_rule(registered(reason)).retry_budget == 3


class TestTheAblationsAblateOnBothRails:
    def test_a1_takes_the_uninformative_row_everywhere(self):
        """"No taxonomy" cannot mean "no card taxonomy, and UPI's"."""
        from windtunnel.arms import UNINFORMATIVE_ROW

        for reason in (MANDATE_DEAD, SHORT_BALANCE, CAP_DECLINE):
            assert arm_named("A1").upi_rule(registered(reason)) == UNINFORMATIVE_ROW

    def test_a2_removes_the_salary_ladder_and_nothing_else(self):
        rule = arm_named("A2").upi_rule(registered(SHORT_BALANCE))
        assert registered(SHORT_BALANCE).salary_aware is True
        assert rule.salary_aware is False
        assert rule.retry_delays, "the fixed backoff replaces the ladder"
        assert rule.retry_budget == registered(SHORT_BALANCE).retry_budget
        assert rule.failure_class is FailureClass.LIQUIDITY

    def test_a2_leaves_every_other_class_alone(self):
        for reason in (MANDATE_DEAD, CAP_DECLINE, IN_FLIGHT):
            assert arm_named("A2").upi_rule(registered(reason)) == registered(reason)

    def test_a3_gives_the_zero_retry_rule_one_probe(self):
        """§8's flagship claim, on the rail where INSTRUMENT_DEAD is 0.27 of the
        mix rather than card_expired's 0.05."""
        assert registered(MANDATE_DEAD).retry_budget == 0
        rule = arm_named("A3").upi_rule(registered(MANDATE_DEAD))
        assert rule.retry_budget == 1
        assert rule.retry_intervention is InterventionType.SILENT_RETRY
        assert rule.post_retry is InterventionType.REAUTH_LINK, "the escalation survives"
        assert rule.failure_class is FailureClass.INSTRUMENT_DEAD

    def test_a3_leaves_the_liquidity_ladder_alone(self):
        assert arm_named("A3").upi_rule(registered(SHORT_BALANCE)) == registered(SHORT_BALANCE)

    def test_a5_drops_every_escalation(self):
        for reason in (MANDATE_DEAD, SHORT_BALANCE, CAP_DECLINE, IN_FLIGHT):
            assert registered(reason).post_retry is not None
            assert arm_named("A5").upi_rule(registered(reason)).post_retry is None


class TestEveryArmSaysWhatItIsOnBothRails:
    def test_no_arm_is_silent_about_the_second_rail(self):
        """A new arm added without a `upi_rule` inherits the identity, which is
        a claim — that its change does not reach UPI — and the claim has to be
        true. The three identities are listed here so that a fourth cannot join
        them by omission."""
        identities = [a.name for a in ALL_ARMS if a.upi_rule(registered(MANDATE_DEAD)) == registered(MANDATE_DEAD)]
        assert sorted(identities) == sorted(IDENTITY_ARMS + ("A2",)), identities
