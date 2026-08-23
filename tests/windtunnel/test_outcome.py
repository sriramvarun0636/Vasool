"""windtunnel/outcome.py — the component EVALUATION.md §1 says is the most
attackable thing in the project.

"I am measuring an agent against a world I built. The simulator decides
whether a retry succeeds. I wrote the simulator." These tests do not and
cannot establish that the outcome model is right. What they establish is
narrower and checkable: that it uses exactly the registered parameters, that
it applies them where §4 says, that its draws are addressed rather than
ordered so §6a's pairing holds, and that the three interpretations added on
top of §4 behave the way they were argued for rather than the way that would
flatter Vasool.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from vasool.diagnosis.proposal import ProposalRole
from vasool.diagnosis.rules import IST, in_salary_window
from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from windtunnel.outcome import Attempt, OutcomeModel, SettlementChannel
from windtunnel.parameters import OUTCOME_PARAMETERS, swept

IN_WINDOW = datetime(2026, 9, 3, 12, 0, tzinfo=IST).astimezone(timezone.utc)
OUT_OF_WINDOW = datetime(2026, 9, 17, 12, 0, tzinfo=IST).astimezone(timezone.utc)


def model(seed: int = 0, **sweeps: float) -> OutcomeModel:
    parameters = OUTCOME_PARAMETERS
    for name, factor in sweeps.items():
        parameters = swept(parameters, name, factor)
    return OutcomeModel(parameters=parameters, seed=seed)


def attempt(
    *,
    episode_id: str = "pay_1",
    failure_class: FailureClass = FailureClass.TRANSIENT,
    intervention: InterventionType = InterventionType.SILENT_RETRY,
    role: ProposalRole = ProposalRole.PRIMARY,
    n: int = 1,
    at: datetime = OUT_OF_WINDOW,
    salary_timed: bool | None = None,
) -> Attempt:
    return Attempt(
        episode_id=episode_id,
        failure_class=failure_class,
        intervention=intervention,
        role=role,
        attempt=n,
        amount_paise=120_000,
        effective_at=at,
        salary_timed=(intervention is InterventionType.TIMED_RETRY)
        if salary_timed is None
        else salary_timed,
    )


def rate(m: OutcomeModel, template: Attempt, trials: int = 4000) -> float:
    """The empirical success rate over many episodes — the only way to read a
    probability back out of an addressed draw."""
    hits = 0
    for i in range(trials):
        hits += m.rule_on(replace(template, episode_id=f"pay_{i}")).money_arrives
    return hits / trials


class TestTheCalendarFixtures:
    """If these drift, every LIQUIDITY test below is quietly testing nothing."""

    def test_the_in_window_fixture_is_in_a_salary_window(self):
        assert in_salary_window(IN_WINDOW.astimezone(IST).date())

    def test_the_out_of_window_fixture_is_not(self):
        assert not in_salary_window(OUT_OF_WINDOW.astimezone(IST).date())


class TestRegisteredRates:
    def test_a_transient_retry_uses_the_registered_transient_rate(self):
        m = model()
        a = attempt(failure_class=FailureClass.TRANSIENT)
        assert m.rule_on(a).parameter == "retry_success_transient"
        assert rate(m, a) == pytest.approx(0.35, abs=0.02)

    def test_a_timed_liquidity_retry_inside_a_window_uses_the_in_window_rate(self):
        m = model()
        a = attempt(
            failure_class=FailureClass.LIQUIDITY,
            intervention=InterventionType.TIMED_RETRY,
            at=IN_WINDOW,
        )
        assert m.rule_on(a).parameter == "retry_success_liquidity_in_window"
        assert rate(m, a) == pytest.approx(0.55, abs=0.02)

    def test_a_liquidity_retry_outside_a_window_uses_the_out_of_window_rate(self):
        m = model()
        a = attempt(
            failure_class=FailureClass.LIQUIDITY,
            intervention=InterventionType.TIMED_RETRY,
            at=OUT_OF_WINDOW,
        )
        assert m.rule_on(a).parameter == "retry_success_liquidity_out_of_window"
        assert rate(m, a) == pytest.approx(0.15, abs=0.02)

    def test_an_instrument_dead_retry_never_succeeds(self):
        """taxonomy §5's flagship claim, and the only non-guess in §4: not
        low, zero. There is no state of the world in which the same expired
        card authorises on the third attempt."""
        m = model()
        for i in range(2000):
            a = attempt(episode_id=f"pay_{i}", failure_class=FailureClass.INSTRUMENT_DEAD)
            ruling = m.rule_on(a)
            assert not ruling.money_arrives
            assert ruling.probability == 0.0
            assert ruling.parameter == "retry_success_instrument_dead"

    def test_an_instrument_dead_retry_stays_zero_under_every_sweep(self):
        """§7 sweeps every parameter ±50%. Half of zero is zero, so A3's
        flagship claim cannot be swept into existence — worth pinning,
        because if it ever could be, F2 would be measuring the sweep."""
        for factor in (0.5, 0.75, 1.25, 1.5):
            m = model(retry_success_instrument_dead=factor)
            a = attempt(failure_class=FailureClass.INSTRUMENT_DEAD)
            assert rate(m, a, trials=500) == 0.0

    def test_a_reauth_link_uses_the_registered_reauth_rate(self):
        m = model()
        a = attempt(
            failure_class=FailureClass.INSTRUMENT_DEAD,
            intervention=InterventionType.REAUTH_LINK,
            n=1,
        )
        assert m.rule_on(a).parameter == "reauth_link_completion"
        assert rate(m, a) == pytest.approx(0.25, abs=0.02)

    def test_a_reattempt_link_uses_the_registered_reattempt_rate(self):
        m = model()
        a = attempt(
            failure_class=FailureClass.CUSTOMER_ACTION,
            intervention=InterventionType.REATTEMPT_LINK,
        )
        assert m.rule_on(a).parameter == "reattempt_link_completion"
        assert rate(m, a) == pytest.approx(0.35, abs=0.02)

    def test_a_reattempt_link_beats_a_reauth_link(self):
        """§4's reasoning: same flow, less friction — the instrument already
        works, so it is a retry the customer chose rather than a new
        instrument they must supply."""
        assert (
            OUTCOME_PARAMETERS["reattempt_link_completion"].value
            > OUTCOME_PARAMETERS["reauth_link_completion"].value
        )


class TestTheThreeInterpretations:
    """§4 does not price every situation an arm can reach. Three readings
    were added on top of it, each argued as the anti-Vasool direction rather
    than chosen for convenience. These pin the behaviour, not the argument.
    """

    def test_a_liquidity_retry_that_lands_in_a_window_by_luck_gets_the_uplift(self):
        """0.15 x 2.0 = 0.30. This is what an arm that does not time for
        payday gets when it lands on one anyway — naive_retry, and ablation
        A2. It is deliberately not 0.55: aiming is supposed to beat luck."""
        m = model()
        a = attempt(
            failure_class=FailureClass.LIQUIDITY,
            intervention=InterventionType.SILENT_RETRY,
            at=IN_WINDOW,
            salary_timed=False,
        )
        assert m.rule_on(a).parameter == "retry_success_liquidity_lucky_window"
        assert rate(m, a) == pytest.approx(0.30, abs=0.02)

    def test_luck_is_worse_than_aim_but_better_than_missing_the_window(self):
        """The ordering A2 is a test of. If luck equalled aim, taxonomy §6
        would be measuring nothing; if luck equalled missing, the uplift
        would not exist."""
        liquidity = dict(failure_class=FailureClass.LIQUIDITY)
        m = model()
        aimed = rate(m, attempt(**liquidity, intervention=InterventionType.TIMED_RETRY, at=IN_WINDOW))
        lucky = rate(m, attempt(**liquidity, intervention=InterventionType.SILENT_RETRY, at=IN_WINDOW, salary_timed=False))
        missed = rate(m, attempt(**liquidity, intervention=InterventionType.SILENT_RETRY, at=OUT_OF_WINDOW, salary_timed=False))
        assert missed < lucky < aimed

    def test_the_uplift_is_never_stacked_on_the_registered_in_window_rate(self):
        """0.55 and 0.15 are already a 3.67x gap; multiplying the in-window
        rate by the uplift as well would double-count the same hypothesis and
        hand A2 a result it did not earn."""
        assert rate(
            model(),
            attempt(
                failure_class=FailureClass.LIQUIDITY,
                intervention=InterventionType.TIMED_RETRY,
                at=IN_WINDOW,
            ),
        ) == pytest.approx(0.55, abs=0.02)

    def test_a_transient_retry_does_not_decay_across_attempts(self):
        """Registered at "attempt 1" only; applied flat. Real gateway
        failures presumably decay, so a flat rate favours persistence — which
        is what the baselines do and Vasool does not. Deliberate, and
        anti-Vasool, rather than an oversight."""
        m = model()
        rates = [rate(m, attempt(failure_class=FailureClass.TRANSIENT, n=n)) for n in (1, 2, 3)]
        assert all(r == pytest.approx(0.35, abs=0.02) for r in rates)

    def test_a_reauth_link_sent_outside_the_contact_window_completes_the_same(self):
        """§4 registers 0.25 "in-window" and no out-of-window value. Using
        the same rate denies Vasool any modelled recovery credit for
        ContactWindowGuard, which inflates F5 (compliance is unaffordable)
        against the project's own thesis."""
        m = model()
        night = datetime(2026, 9, 17, 3, 0, tzinfo=IST).astimezone(timezone.utc)
        day = datetime(2026, 9, 17, 12, 0, tzinfo=IST).astimezone(timezone.utc)
        link = dict(
            failure_class=FailureClass.INSTRUMENT_DEAD,
            intervention=InterventionType.REAUTH_LINK,
        )
        assert rate(m, attempt(**link, at=night)) == pytest.approx(
            rate(m, attempt(**link, at=day)), abs=0.01
        )

    @pytest.mark.parametrize(
        "failure_class", [FailureClass.CUSTOMER_ACTION, FailureClass.RISK_BLOCK]
    )
    def test_a_retry_on_a_class_section_4_never_prices_uses_the_added_rate(
        self, failure_class: FailureClass
    ):
        """Only the baselines retry these. Set to the highest registered
        retry rate because generous is the anti-Vasool direction here — zero
        would flatter Vasool by construction."""
        m = model()
        a = attempt(failure_class=failure_class)
        assert m.rule_on(a).parameter == "retry_success_unpriced_class"
        assert rate(m, a) == pytest.approx(0.35, abs=0.02)

    def test_the_lucky_window_rule_declares_both_parameters_it_composes(self):
        """§7 sweeps parameters one at a time and asks which conclusions each
        could have touched. A rule built from two registered values has to
        say so, or the uplift's sweep would appear to affect nothing."""
        ruling = model().rule_on(
            attempt(
                failure_class=FailureClass.LIQUIDITY,
                intervention=InterventionType.SILENT_RETRY,
                at=IN_WINDOW,
                salary_timed=False,
            )
        )
        assert set(ruling.depends_on) == {
            "retry_success_liquidity_out_of_window",
            "salary_window_uplift",
        }

    def test_every_ruling_names_only_registered_parameters(self):
        m = model()
        for failure_class in FailureClass:
            for intervention in InterventionType:
                ruling = m.rule_on(
                    attempt(failure_class=failure_class, intervention=intervention)
                )
                assert all(name in OUTCOME_PARAMETERS for name in ruling.depends_on)

    def test_luck_can_never_beat_aim_under_any_sweep(self):
        """The uplift is swept ±50% like everything else, and at +50% it
        would price luck at 0.45 — still below aim. Pinned anyway, because an
        inversion here would let A2 report that not timing beats timing."""
        for factor in (0.5, 1.0, 1.5):
            m = model(salary_window_uplift=factor)
            liquidity = dict(failure_class=FailureClass.LIQUIDITY)
            lucky = m.rule_on(
                attempt(**liquidity, intervention=InterventionType.SILENT_RETRY, at=IN_WINDOW, salary_timed=False)
            ).probability
            aimed = m.rule_on(
                attempt(**liquidity, intervention=InterventionType.TIMED_RETRY, at=IN_WINDOW)
            ).probability
            assert lucky <= aimed

    def test_the_added_rate_is_no_lower_than_any_registered_retry_rate(self):
        registered = [
            OUTCOME_PARAMETERS[name].value
            for name in (
                "retry_success_transient",
                "retry_success_liquidity_in_window",
                "retry_success_liquidity_out_of_window",
            )
        ]
        added = OUTCOME_PARAMETERS["retry_success_unpriced_class"].value
        assert added >= min(registered)


class TestActionsThatMoveNoMoney:
    @pytest.mark.parametrize("role", [ProposalRole.NUDGE, ProposalRole.PRE_DEBIT_NOTICE])
    def test_a_message_never_settles_anything_by_itself(self, role: ProposalRole):
        """A nudge and a pre-debit notice are messages, not re-presentations.
        vasool/diagnosis/proposal.py is explicit that a NUDGE shares its
        sibling's intervention without being a re-presentation of anything —
        letting one settle would credit the taxonomy with recoveries it never
        performed."""
        m = model()
        for i in range(500):
            ruling = m.rule_on(
                attempt(
                    episode_id=f"pay_{i}",
                    intervention=InterventionType.TIMED_RETRY,
                    role=role,
                    failure_class=FailureClass.LIQUIDITY,
                )
            )
            assert not ruling.money_arrives
            assert ruling.channel is None

    def test_a_human_queue_handoff_never_settles_anything(self):
        m = model()
        ruling = m.rule_on(
            attempt(
                failure_class=FailureClass.RISK_BLOCK,
                intervention=InterventionType.HUMAN_QUEUE,
            )
        )
        assert not ruling.money_arrives


class TestSettlementChannel:
    def test_a_retry_settles_through_a_capture(self):
        m = model()
        rulings = [
            m.rule_on(attempt(episode_id=f"pay_{i}", failure_class=FailureClass.TRANSIENT))
            for i in range(200)
        ]
        assert {r.channel for r in rulings if r.money_arrives} == {SettlementChannel.RETRY_CAPTURE}

    def test_a_link_settles_through_a_paid_link(self):
        m = model()
        rulings = [
            m.rule_on(
                attempt(
                    episode_id=f"pay_{i}",
                    failure_class=FailureClass.CUSTOMER_ACTION,
                    intervention=InterventionType.REATTEMPT_LINK,
                )
            )
            for i in range(200)
        ]
        assert {r.channel for r in rulings if r.money_arrives} == {SettlementChannel.LINK_PAID}


class TestAddressedDraws:
    def test_the_same_attempt_always_gets_the_same_ruling(self):
        m = model()
        a = attempt()
        assert m.rule_on(a).draw_value == m.rule_on(a).draw_value

    def test_two_arms_taking_different_actions_share_the_coin(self):
        """Common random numbers, and the reason `intervention` is not in the
        draw's address. When one arm retries where another sends a link, the
        question "was this episode going to pay at attempt 2" should get one
        answer; only the probability applied to it differs. That is what
        keeps §6a's paired differences low-variance."""
        m = model()
        retry = m.rule_on(attempt(failure_class=FailureClass.INSTRUMENT_DEAD, n=2))
        link = m.rule_on(
            attempt(
                failure_class=FailureClass.INSTRUMENT_DEAD,
                intervention=InterventionType.REAUTH_LINK,
                n=2,
            )
        )
        assert retry.draw_value == link.draw_value
        assert retry.probability != link.probability

    def test_different_attempts_of_one_episode_are_independent(self):
        m = model()
        draws = {m.rule_on(attempt(n=n)).draw_value for n in range(1, 6)}
        assert len(draws) == 5

    def test_a_nudge_and_its_sibling_retry_do_not_share_a_coin(self):
        """They are the same intervention and attempt, so without the role in
        the address they would be one draw — and a nudge settling nothing
        would still consume the retry's answer."""
        m = model()
        primary = m.rule_on(attempt(intervention=InterventionType.TIMED_RETRY))
        nudge = m.rule_on(
            attempt(intervention=InterventionType.TIMED_RETRY, role=ProposalRole.NUDGE)
        )
        assert primary.draw_value != nudge.draw_value

    def test_the_seed_changes_every_outcome(self):
        a = attempt()
        assert model(seed=1).rule_on(a).draw_value != model(seed=2).rule_on(a).draw_value

    def test_sweeping_a_parameter_does_not_re_roll_the_world(self):
        """§7 holds everything else fixed. If a sweep moved the draws, the
        sensitivity result would be confounded with a different world."""
        a = attempt(failure_class=FailureClass.TRANSIENT)
        base = model().rule_on(a)
        after = model(retry_success_transient=1.5).rule_on(a)
        assert base.draw_value == after.draw_value
        assert after.probability > base.probability


class TestOutOfBandSettlement:
    def test_it_fires_at_roughly_the_registered_daily_rate(self):
        m = model()
        fired = sum(m.out_of_band_at(f"pay_{i}", arrived_at=IN_WINDOW, horizon_days=1) is not None
                    for i in range(4000))
        assert fired / 4000 == pytest.approx(0.02, abs=0.006)

    def test_a_longer_episode_is_more_exposed(self):
        m = model()
        short = sum(m.out_of_band_at(f"pay_{i}", arrived_at=IN_WINDOW, horizon_days=1) is not None
                    for i in range(3000))
        long = sum(m.out_of_band_at(f"pay_{i}", arrived_at=IN_WINDOW, horizon_days=30) is not None
                   for i in range(3000))
        assert long > short * 5

    def test_it_lands_inside_the_horizon_it_was_given(self):
        m = model()
        for i in range(500):
            at = m.out_of_band_at(f"pay_{i}", arrived_at=IN_WINDOW, horizon_days=10)
            if at is not None:
                assert IN_WINDOW <= at < IN_WINDOW + timedelta(days=10)

    def test_it_does_not_depend_on_anything_the_agent_did(self):
        """The decisive property. An out-of-band payment is something the
        customer does, so it must be a fact about the world and identical
        across every arm — otherwise the arms are not running on the same
        universe and §5's premise fails."""
        m = model()
        assert m.out_of_band_at("pay_7", arrived_at=IN_WINDOW, horizon_days=30) == m.out_of_band_at(
            "pay_7", arrived_at=IN_WINDOW, horizon_days=30
        )

    def test_a_longer_horizon_only_ever_adds_occasions_never_moves_one(self):
        """Because the scan is day-by-day over addressed draws, extending the
        horizon cannot change an earlier day's answer. Without this, an arm
        that ended an episode sooner would see a different out-of-band world.
        """
        m = model()
        for i in range(300):
            early = m.out_of_band_at(f"pay_{i}", arrived_at=IN_WINDOW, horizon_days=5)
            late = m.out_of_band_at(f"pay_{i}", arrived_at=IN_WINDOW, horizon_days=40)
            if early is not None:
                assert late == early

    def test_zeroing_the_parameter_removes_the_attack_from_the_run(self):
        """§4: "set it to zero and the attack never fires in evaluation"."""
        m = model(out_of_band_per_episode_day=0.0)
        assert all(
            m.out_of_band_at(f"pay_{i}", arrived_at=IN_WINDOW, horizon_days=60) is None
            for i in range(500)
        )
