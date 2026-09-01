"""windtunnel/arms.py: EVALUATION.md §5's baselines and §8's ablations.

**The constraint these tests exist to hold.** An arm is a *configuration* of
the real agent, never a second agent. If any arm ran its own state machine,
its own guards or its own ledger, every number downstream would be comparing
two codebases rather than two policies. So the first class below asserts, for
every arm, that the objects are the same objects.

The second thing under test is that each arm is the thing §5 or §8 describes
and only that thing — one mechanism removed, everything else identical. An
ablation that changed two things at once would not isolate anything.
"""
from __future__ import annotations

import pytest

from vasool.diagnosis.taxonomy import RULES, FailureClass, InterventionType
from vasool.policy.registry import GUARD_CHAIN, evaluate_all
from windtunnel.arms import (
    ABLATIONS,
    ALL_ARMS,
    BASELINES,
    VASOOL,
    ArmKind,
    arm_named,
)
from windtunnel.metrics import measure
from windtunnel.runner import run_seed

PEPPER = "test-pepper-do-not-use-in-prod"


def row(table, reason: str, source: str = "*"):
    return table[(reason, source)]


class TestTheRegisteredSet:
    def test_there_are_nine_arms(self):
        """§5's three baselines, §8's five ablations, and Vasool itself."""
        assert len(ALL_ARMS) == 9
        assert len(BASELINES) == 3 and len(ABLATIONS) == 5

    def test_names_are_unique_and_stable(self):
        names = [a.name for a in ALL_ARMS]
        assert len(set(names)) == len(names)
        assert {"naive_retry", "retry_plus_contact", "vasool_ungated"} <= set(names)
        assert {"A1", "A2", "A3", "A4", "A5"} <= set(names)

    def test_every_arm_states_what_it_isolates(self):
        """§8's table has a "Tests" column for every row. An ablation whose
        rationale is not written down is one nobody can check the result of."""
        assert all(len(a.rationale) > 80 for a in ALL_ARMS)

    def test_arm_named_finds_each_one(self):
        assert all(arm_named(a.name) is a for a in ALL_ARMS)

    def test_an_unknown_arm_raises(self):
        with pytest.raises(KeyError, match="not a registered arm"):
            arm_named("vasool_but_better")


class TestArmsAreConfigurationsNotCopies:
    """The session's central constraint, asserted mechanically."""

    def test_every_arm_uses_the_same_guard_objects(self):
        """Not an equal chain — the same objects. A guard reconstructed per
        arm could drift; these cannot."""
        for arm in ALL_ARMS:
            for guard in arm.chain:
                assert any(guard is registered for registered in GUARD_CHAIN), arm.name

    def test_only_the_registered_arms_drop_guards(self):
        """§5.1, §5.2 and §5.3 each say "no compliance layer" or "guard chain
        removed". Nothing else may."""
        ungated = {a.name for a in ALL_ARMS if not a.chain}
        assert ungated == {"naive_retry", "retry_plus_contact", "vasool_ungated"}

    def test_every_gated_arm_runs_all_thirteen_in_the_registered_order(self):
        for arm in ALL_ARMS:
            if arm.chain:
                assert arm.chain == GUARD_CHAIN, arm.name

    def test_only_a4_changes_how_the_chain_resolves(self):
        assert {a.name for a in ALL_ARMS if a.resolve is not evaluate_all} == {"A4"}

    def test_every_arm_table_has_the_registered_key_set(self):
        """An arm is a different policy for a reason, never a claim that a
        different set of reasons exists. the project's rule against inventing
        error strings holds for the baselines exactly as it holds for the
        agent."""
        for arm in ALL_ARMS:
            assert set(arm.rules) == set(RULES), arm.name

    def test_every_arm_table_keeps_the_budget_invariant(self):
        """tests/test_taxonomy.py holds this for the registered table: a row
        has exactly as many delays as its budget, and a retry intervention iff
        it has a budget. An arm that broke it would crash in `_retry_at`
        rather than measure anything."""
        for arm in ALL_ARMS:
            for key, rule in arm.rules.items():
                if rule.salary_aware:
                    assert rule.retry_delays == (), (arm.name, key)
                else:
                    assert len(rule.retry_delays) == rule.retry_budget, (arm.name, key)
                assert (rule.retry_intervention is not None) == (rule.retry_budget > 0), (
                    arm.name,
                    key,
                )

    def test_vasool_is_the_registered_configuration_untouched(self):
        assert VASOOL.rules is RULES
        assert VASOOL.chain is GUARD_CHAIN
        assert VASOOL.resolve is evaluate_all
        assert VASOOL.kind is ArmKind.AGENT


class TestBaselines:
    def test_naive_retry_treats_every_reason_identically(self):
        """§5.1: "retry every failure on fixed exponential backoff
        (5m/30m/4h) until the attempt cap, regardless of reason"."""
        arm = arm_named("naive_retry")
        rules = set(arm.rules.values())
        assert len(rules) == 1
        only = next(iter(rules))
        assert only.retry_budget == 3
        assert only.retry_intervention is InterventionType.SILENT_RETRY
        assert [d.total_seconds() for d in only.retry_delays] == [300, 1800, 14400]

    def test_naive_retry_never_contacts_anyone(self):
        """§5.1: "no contact". The row names no escalation, so the episode
        stops when the retries run out."""
        arm = arm_named("naive_retry")
        assert all(r.post_retry is None for r in arm.rules.values())
        assert all(not r.soft_nudge for r in arm.rules.values())

    def test_retry_plus_contact_is_naive_retry_plus_the_link(self):
        """§5.2: "naive_retry plus a payment link after the retries exhaust".
        Exactly one field differs, so the comparison isolates the link."""
        naive = arm_named("naive_retry").rules
        realistic = arm_named("retry_plus_contact").rules
        for key in RULES:
            assert realistic[key].post_retry is InterventionType.REATTEMPT_LINK
            assert realistic[key].retry_budget == naive[key].retry_budget
            assert realistic[key].retry_delays == naive[key].retry_delays

    def test_vasool_ungated_keeps_the_taxonomy_and_drops_the_guards(self):
        """§5.3: "Vasool's full taxonomy and timing with the guard chain
        removed" — the arm that prices the compliance layer."""
        arm = arm_named("vasool_ungated")
        assert arm.rules is RULES
        assert arm.chain == ()


class TestAblations:
    def test_a1_classifies_everything_as_the_uninformative_row(self):
        """§10, 2026-08-23: A1 takes `payment_failed`/`gateway`'s single probe,
        not `gateway_technical_error`'s three — an agent with no
        classification has no basis for knowing a gateway problem is a gateway
        problem."""
        arm = arm_named("A1")
        registered = row(RULES, "payment_failed", "gateway")
        assert set(arm.rules.values()) == {registered}
        assert registered.retry_budget == 1

    def test_a2_removes_only_the_salary_timing(self):
        """§8: "LIQUIDITY uses fixed backoff". Everything else about the row —
        budget, nudge, escalation, class — is untouched, or the ablation would
        not isolate taxonomy §6."""
        before, after = row(RULES, "insufficient_fund"), row(arm_named("A2").rules, "insufficient_fund")
        assert before.salary_aware and not after.salary_aware
        assert [d.total_seconds() for d in after.retry_delays] == [300, 1800, 14400]
        assert after.retry_budget == before.retry_budget
        assert after.soft_nudge == before.soft_nudge
        assert after.post_retry == before.post_retry
        assert after.failure_class is FailureClass.LIQUIDITY

    def test_a2_changes_nothing_else_in_the_table(self):
        arm = arm_named("A2")
        assert all(arm.rules[k] == RULES[k] for k in RULES if k[0] != "insufficient_fund")

    def test_a3_gives_the_dead_rows_a_single_probe(self):
        """§10, 2026-08-23: the budget the class would otherwise get, not
        three. The claim under test is that ONE futile retry costs an attempt
        the re-auth link needed; three would test a strawman."""
        arm = arm_named("A3")
        for reason in ("card_expired", "card_disabled_for_online_payments"):
            assert row(RULES, reason).retry_budget == 0
            assert row(arm.rules, reason).retry_budget == 1
            assert row(arm.rules, reason).retry_intervention is InterventionType.SILENT_RETRY
            assert row(arm.rules, reason).post_retry is InterventionType.REAUTH_LINK

    def test_a3_leaves_card_declined_alone(self):
        """It already has the one probe §10 registers, so A3 does not touch
        it — an ablation that also changed a row it did not need to would
        widen the effect it is measuring."""
        assert row(arm_named("A3").rules, "card_declined") == row(RULES, "card_declined")

    def test_a3_does_not_touch_the_risk_block_row(self):
        """§8: RISK_BLOCK is deliberately not ablated."""
        assert row(arm_named("A3").rules, "payment_risk_check_failed") == row(
            RULES, "payment_risk_check_failed"
        )

    def test_a4_short_circuits_on_the_first_refusal(self):
        """§8: the design spec's cheapest-first order with short-circuit
        semantics. The order is already the spec's (registry.py), so only the
        resolution differs."""
        arm = arm_named("A4")
        assert arm.chain == GUARD_CHAIN
        assert arm.rules is RULES
        assert arm.resolve is not evaluate_all

    def test_a5_removes_every_escalation(self):
        """§8: "retries exhaust and the episode stops, with no link"."""
        arm = arm_named("A5")
        assert all(r.post_retry is None for r in arm.rules.values())
        assert all(
            arm.rules[k].retry_budget == RULES[k].retry_budget for k in RULES
        )


class TestArmsRun:
    """Every arm has to reach the end of a real universe. An arm that crashes
    on seed 0 is one whose numbers never appear."""

    @pytest.mark.parametrize("arm", ALL_ARMS, ids=lambda a: a.name)
    def test_the_arm_completes_and_is_deterministic(self, arm):
        first = run_seed(0, pepper=PEPPER, arm=arm)
        assert first.arm == arm.name
        assert len(first.transitions) > 0
        assert first.transition_digest() == run_seed(0, pepper=PEPPER, arm=arm).transition_digest()

    @pytest.mark.parametrize("arm", ALL_ARMS, ids=lambda a: a.name)
    def test_every_arm_produces_a_verifiable_ledger(self, arm):
        """Whatever an arm's policy is, the ledger is the same code and must
        still chain. A baseline that violates every rule still writes a valid
        audit trail of having done so."""
        m = measure(run_seed(0, pepper=PEPPER, arm=arm), arm=arm.name)
        chain = next(c for c in m.safety.claims if c.name == "receipt_chain_verifies")
        assert chain.passed

    def test_every_arm_sees_the_same_world(self):
        """§5: "all running on the identical seeded universe with identical
        arrivals and identical outcome draws". If the arms disagreed about the
        world, §6a's pairing would be measuring that disagreement."""
        universes = [run_seed(0, pepper=PEPPER, arm=a).universe for a in ALL_ARMS]
        first = universes[0]
        for other in universes[1:]:
            assert [e.entity_id for e in other.episodes] == [e.entity_id for e in first.episodes]
            assert [e.amount_paise for e in other.episodes] == [
                e.amount_paise for e in first.episodes
            ]
            assert [e.out_of_band_at for e in other.episodes] == [
                e.out_of_band_at for e in first.episodes
            ]


class TestTheAblationsActuallyBite:
    """§8: "If A1 matches full Vasool, docs/taxonomy.md is decoration." Each
    ablation has to change *something*, or its result is untestable. These are
    development-set observations on one seed — direction, not evidence. The
    registered comparison is §6a's over 1000 seeds.
    """

    def test_a_dead_instrument_never_authorises_however_it_is_labelled(self):
        """The defect this simulator would have had if the outcome model
        priced on the agent's belief: A1 calls an expired card TRANSIENT, and
        if that decided the coin the card would revive and "no taxonomy" would
        beat the taxonomy by being wrong."""
        run = run_seed(0, pepper=PEPPER, arm=arm_named("A1"))
        dead = {
            e.entity_id
            for e in run.universe.episodes
            if e.failure_class is FailureClass.INSTRUMENT_DEAD
        }
        settled_by_retry = {
            entity for entity, channel in run.settled if channel == "RETRY_CAPTURE"
        }
        assert not (dead & settled_by_retry)

    def test_a3_burns_attempts_on_instruments_that_cannot_authorise(self):
        """F2's mechanism, as a number: A3 spends retries where Vasool spends
        none, and the design spec's headline guardrail counts them."""
        vasool = run_seed(0, pepper=PEPPER, arm=VASOOL)
        ablated = run_seed(0, pepper=PEPPER, arm=arm_named("A3"))

        def futile(run):
            return sum(
                1
                for a in run.executed
                if a.is_retry and a.ok and a.true_failure_class == FailureClass.INSTRUMENT_DEAD
            )

        assert futile(ablated) > futile(vasool)

    def test_the_ungated_arm_is_the_one_that_breaks_the_rules(self):
        """§5.3 is deliberately adversarial to the project's own thesis: if
        the guards cost nothing they are decorative. The converse has to hold
        too — without them, the safety predicate must actually fail."""
        gated = measure(run_seed(0, pepper=PEPPER, arm=VASOOL), arm="vasool")
        ungated = measure(
            run_seed(0, pepper=PEPPER, arm=arm_named("vasool_ungated")), arm="vasool_ungated"
        )
        assert gated.safety.holds
        assert not ungated.safety.holds
