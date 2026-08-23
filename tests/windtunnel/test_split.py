"""windtunnel/split.py: EVALUATION.md §3c's 40/60 customer-level split.

Two things are under test. The split has to be a real stratified split —
customer-level, sticky, balanced on §3b's key, reproducible from the seed. And
the holdout has to be *sealed*: §3c says it is evaluated once, and the failure
mode that rule exists to prevent is not a deliberate peek, it is an absent-
minded one. So the tests below check that reading it by accident is loud.
"""
from __future__ import annotations

import pytest

from vasool.diagnosis.taxonomy import FailureClass
from windtunnel.metrics import measure
from windtunnel.outcome import OutcomeModel
from windtunnel.parameters import OUTCOME_PARAMETERS
from windtunnel.runner import run_seed
from windtunnel.split import (
    DEVELOPMENT_SHARE,
    UNSEAL_PHRASE,
    Cohort,
    HoldoutSealed,
    split_customers,
)
from windtunnel.universe import CUSTOMER_COUNT, build_universe

PEPPER = "test-pepper-do-not-use-in-prod"


@pytest.fixture(scope="module")
def run():
    return run_seed(0, pepper=PEPPER)


@pytest.fixture(scope="module")
def split(run):
    return split_customers(run.universe)


def universe_for(seed: int):
    outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=seed)
    return build_universe(seed, pepper=PEPPER, outcome=outcome)


class TestPartition:
    def test_every_customer_lands_on_exactly_one_side(self, run, split):
        everyone = {c.customer_id for c in run.universe.customers}
        holdout = split.holdout(unseal=UNSEAL_PHRASE)
        assert split.development | holdout == everyone
        assert not (split.development & holdout)
        assert len(everyone) == CUSTOMER_COUNT

    def test_the_development_share_is_forty_percent(self, split):
        assert split.development_size == pytest.approx(
            CUSTOMER_COUNT * DEVELOPMENT_SHARE, abs=3
        )

    def test_the_split_is_at_the_customer_not_the_episode(self, run, split):
        """§3a: a customer and all their episodes land wholly on one side.
        Splitting per episode would leak a customer's frequency-cap budget and
        contact history across the boundary."""
        for customer in run.universe.customers:
            episodes = [e for e in run.universe.episodes if e.customer.index == customer.index]
            if not episodes:
                continue
            sides = {
                customer.customer_id in split.development for _ in episodes
            }
            assert len(sides) == 1


class TestStratification:
    def test_the_key_is_the_first_episodes_failure_class(self, run, split):
        """§3b, against the Universe's own definition of the key rather than
        against a second copy of it."""
        for customer in run.universe.customers:
            assert split.stratum_of(customer.customer_id) == run.universe.stratum_of(customer)

    def test_every_stratum_is_split_in_proportion(self, split):
        """An unstratified split can hand one side a materially easier
        population by chance, and the classes differ enormously in
        recoverability. Each stratum is split at the same ratio, ±1 customer
        for the rounding."""
        for balance in split.strata:
            total = balance.development + balance.holdout
            expected = total * DEVELOPMENT_SHARE
            assert abs(balance.development - expected) <= 1, balance

    def test_all_five_classes_are_exercised(self, split):
        """§3d shaped the mix so every class appears. If a stratum were empty
        the split would be balanced on a key that does not vary."""
        present = {b.stratum for b in split.strata if b.development + b.holdout > 0}
        assert set(FailureClass) <= present

    def test_the_balance_table_totals_the_universe(self, split):
        assert sum(b.development + b.holdout for b in split.strata) == CUSTOMER_COUNT


class TestDeterminism:
    def test_the_same_seed_gives_the_same_split(self):
        first, second = split_customers(universe_for(4)), split_customers(universe_for(4))
        assert first.development == second.development

    def test_a_different_seed_gives_a_different_split(self):
        """Each seed is an independent world with its own customers, so the
        split is per-universe. Identical splits would mean the seed is not
        reaching the assignment."""
        assert split_customers(universe_for(4)).development != split_customers(
            universe_for(5)
        ).development

    def test_assignment_does_not_depend_on_iteration_order(self, run):
        """Coordinate-addressed, like everything else in this simulator: a
        customer's side is a function of their id, not of how many customers
        were assigned before them."""
        forward = split_customers(run.universe)
        assert all(
            forward.side_of(c.customer_id) == forward.side_of(c.customer_id)
            for c in reversed(run.universe.customers)
        )


class TestTheHoldoutIsSealed:
    """§3c: evaluated once, and an accidental peek must be loud."""

    def test_reading_the_holdout_without_the_phrase_raises(self, split):
        with pytest.raises(HoldoutSealed):
            split.holdout()

    def test_a_wrong_phrase_raises(self, split):
        with pytest.raises(HoldoutSealed):
            split.holdout(unseal="yes")

    def test_the_phrase_names_the_clause_it_is_breaking(self):
        """Whoever types this has to type the rule they are invoking. A bare
        boolean would be as easy to pass by reflex as to pass deliberately."""
        assert "§3c" in UNSEAL_PHRASE and "once" in UNSEAL_PHRASE

    def test_the_error_says_what_the_rule_is(self, split):
        with pytest.raises(HoldoutSealed, match="once"):
            split.holdout()

    def test_the_holdout_is_not_reachable_through_the_public_fields(self, split):
        """A sealed set that is still sitting in a public attribute is not
        sealed. The only route is the method that demands the phrase."""
        public = {
            name: getattr(split, name)
            for name in dir(split)
            if not name.startswith("_") and not callable(getattr(split, name))
        }
        holdout = split.holdout(unseal=UNSEAL_PHRASE)
        assert not any(value == holdout for value in public.values())

    def test_the_cohorts_have_separate_output_paths(self):
        """§3c wants an accidental read to be loud rather than silent, and a
        shared output directory is how a holdout number ends up in a
        development report without anyone noticing."""
        assert Cohort.DEVELOPMENT.directory != Cohort.HOLDOUT.directory


class TestMeasuringACohort:
    def test_development_metrics_only_see_development_customers(self, run, split):
        m = measure(run, arm="vasool", cohort=Cohort.DEVELOPMENT.value, customers=split.development)
        assert m.cohort == Cohort.DEVELOPMENT.value
        entities = {
            e.entity_id
            for e in run.universe.episodes
            if e.customer.customer_id in split.development
        }
        assert m.episodes == len(entities)

    def test_the_two_cohorts_partition_the_run(self, run, split):
        dev = measure(run, arm="vasool", customers=split.development)
        held = measure(run, arm="vasool", customers=split.holdout(unseal=UNSEAL_PHRASE))
        whole = measure(run, arm="vasool")
        assert dev.episodes + held.episodes == whole.episodes
        assert dev.recovered + held.recovered == whole.recovered

    def test_the_development_set_is_big_enough_to_iterate_on(self, run, split):
        """40% of 500 customers has to carry enough episodes for the
        development numbers to mean anything at all."""
        dev = measure(run, arm="vasool", customers=split.development)
        assert dev.episodes > 250
