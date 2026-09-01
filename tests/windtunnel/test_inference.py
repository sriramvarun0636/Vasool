"""windtunnel/inference.py: EVALUATION.md §6a's paired bootstrap and §6b's pass^k.

§6a rules out the obvious version of this comparison — overlapping marginal
intervals — as "a conservative test that would miss real differences the
pairing can detect". `test_pairing_detects_what_marginals_would_miss` is that
sentence as an executable claim.

§6b's distinction between seeds and repeats is the one this file exists to
pin down. Re-running a single seed is not an independent trial: the project rules
invariant 5 makes the ledger byte-identical, so pass^k over repeats is 1.0 by
construction and measures nothing. What varies across *seeds* is the world.
"""
from __future__ import annotations

import math

import pytest

from windtunnel.inference import (
    PASS_K_VALUES,
    marginal_interval,
    paired_difference,
    pass_k,
    survives,
)


def flat(values: dict[int, float]) -> dict[int, float]:
    return values


class TestPairedDifference:
    def test_a_consistent_gain_is_superior(self):
        treatment = {s: 0.30 + 0.001 * s for s in range(200)}
        baseline = {s: 0.20 + 0.001 * s for s in range(200)}
        result = paired_difference(treatment, baseline, metric="recovery_rate")
        assert result.interval.point == pytest.approx(0.10, abs=1e-9)
        assert result.interval.excludes_zero
        assert result.superior

    def test_no_difference_does_not_exclude_zero(self):
        shared = {s: 0.25 + 0.002 * (s % 17) for s in range(200)}
        result = paired_difference(shared, dict(shared), metric="recovery_rate")
        assert result.interval.point == pytest.approx(0.0, abs=1e-12)
        assert not result.interval.excludes_zero
        assert not result.superior

    def test_a_consistent_loss_excludes_zero_but_is_not_superiority(self):
        """§6a claims superiority iff the interval excludes zero — which an
        interval wholly below zero also does. Direction is not optional."""
        treatment = {s: 0.10 for s in range(200)}
        baseline = {s: 0.20 for s in range(200)}
        result = paired_difference(treatment, baseline, metric="recovery_rate")
        assert result.interval.excludes_zero
        assert not result.superior
        assert result.interval.point < 0

    def test_pairing_detects_what_marginals_would_miss(self):
        """§6a's whole argument. The arms differ by a steady 2pp on every
        seed, but each arm's own spread is an order of magnitude wider — so
        the marginal intervals overlap comfortably while the paired interval
        does not go near zero."""
        baseline = {s: 0.10 + 0.005 * (s % 100) for s in range(200)}
        treatment = {s: v + 0.02 for s, v in baseline.items()}

        left, right = marginal_interval(treatment), marginal_interval(baseline)
        assert left.low < right.high, "marginals must overlap for this test to mean anything"

        assert paired_difference(treatment, baseline, metric="recovery_rate").interval.excludes_zero

    def test_the_difference_is_per_seed(self, ):
        """d_s = metric(treatment, s) − metric(baseline, s), seed by seed. A
        comparison of the two means would silently tolerate arms being
        measured on different worlds."""
        treatment = {0: 0.5, 1: 0.1}
        baseline = {0: 0.1, 1: 0.5}
        result = paired_difference(treatment, baseline, metric="recovery_rate")
        assert sorted(result.differences) == [-0.4, 0.4]
        assert result.interval.point == pytest.approx(0.0)

    def test_mismatched_seeds_raise(self):
        """Pairing requires the same worlds on both sides. Silently
        intersecting would drop seeds and report an n nobody chose."""
        with pytest.raises(ValueError, match="same seeds"):
            paired_difference({0: 1.0, 1: 1.0}, {0: 1.0}, metric="recovery_rate")

    def test_it_is_deterministic(self):
        """The bootstrap's own randomness is seeded. Two runs of the
        evaluator must not report two different intervals for one dataset."""
        treatment = {s: 0.3 + 0.01 * (s % 7) for s in range(300)}
        baseline = {s: 0.2 + 0.01 * (s % 11) for s in range(300)}
        first = paired_difference(treatment, baseline, metric="m")
        second = paired_difference(treatment, baseline, metric="m")
        assert first.interval == second.interval

    def test_the_interval_is_a_percentile_interval(self):
        result = paired_difference(
            {s: float(s) for s in range(500)}, {s: 0.0 for s in range(500)}, metric="m"
        )
        assert result.interval.level == 0.95
        assert result.interval.low < result.interval.point < result.interval.high


class TestPassK:
    """§6b: the probability the §2a predicate holds across k independently
    seeded universes, as the fraction of size-k seed subsets where all pass."""

    def test_all_seeds_passing_gives_one_at_every_k(self):
        assert pass_k([True] * 200) == {k: 1.0 for k in PASS_K_VALUES}

    def test_it_is_the_exact_subset_fraction(self):
        """C(m, k) / C(n, k), not a Monte Carlo over sampled subsets. The
        closed form is exact and free, and a sampled one would put noise in a
        number F4 turns on."""
        outcomes = [True] * 90 + [False] * 10
        computed = pass_k(outcomes)
        for k in (1, 5, 10, 25, 50):
            assert computed[k] == pytest.approx(math.comb(90, k) / math.comb(100, k))

    def test_one_failure_in_a_hundred_collapses_pass_100(self):
        """§6b: "a system that satisfies the safety predicate in 99 of 100
        worlds is not safe" — pass^k is what makes that visible where a mean
        would bury it at 0.99."""
        outcomes = [True] * 99 + [False]
        computed = pass_k(outcomes)
        assert computed[1] == pytest.approx(0.99)
        assert computed[100] == 0.0

    def test_k_larger_than_the_passing_count_is_zero(self):
        assert pass_k([True] * 4 + [False] * 96)[5] == 0.0

    def test_k_larger_than_the_seed_count_raises(self):
        """Undefined rather than zero: there is no size-100 subset of 50
        seeds, and reporting 0.0 would read as "it failed" instead of "it was
        not measured"."""
        with pytest.raises(ValueError, match="needs at least"):
            pass_k([True] * 50)

    def test_repeats_of_one_seed_are_not_what_this_measures(self):
        """§6b's central warning. Invariant 5 makes a re-run byte-identical,
        so k repeats of one passing seed is a vector of identical Trues and
        pass^k is 1.0 by construction — measuring the determinism guarantee,
        not the agent. The same k drawn from *different* worlds, one of which
        fails, is not 1.0. Both are computed here from the same function, so
        the difference is entirely in what the caller feeds it."""
        repeats = [True] * 100
        worlds = [True] * 99 + [False]
        assert pass_k(repeats)[100] == 1.0
        assert pass_k(worlds)[100] == 0.0

    def test_the_reported_k_values_are_the_registered_ones(self):
        assert PASS_K_VALUES == (1, 5, 10, 25, 50, 100)


class TestSurvives:
    """§7: a conclusion survives a sweep when the swept interval still
    excludes zero in the same direction as the reference."""

    def test_a_conclusion_that_holds_both_ways_survives(self):
        reference = paired_difference(
            {s: 0.3 for s in range(100)}, {s: 0.2 for s in range(100)}, metric="m"
        )
        swept = paired_difference(
            {s: 0.28 for s in range(100)}, {s: 0.2 for s in range(100)}, metric="m"
        )
        assert survives(reference, swept)

    def test_a_conclusion_that_loses_significance_does_not_survive(self):
        reference = paired_difference(
            {s: 0.3 for s in range(100)}, {s: 0.2 for s in range(100)}, metric="m"
        )
        swept = paired_difference(
            {s: 0.2 for s in range(100)}, {s: 0.2 for s in range(100)}, metric="m"
        )
        assert not survives(reference, swept)

    def test_a_conclusion_that_reverses_does_not_survive(self):
        """A sweep that flips the sign is the case §7 says to report as an
        artifact and drop from the pitch."""
        reference = paired_difference(
            {s: 0.3 for s in range(100)}, {s: 0.2 for s in range(100)}, metric="m"
        )
        swept = paired_difference(
            {s: 0.1 for s in range(100)}, {s: 0.2 for s in range(100)}, metric="m"
        )
        assert swept.interval.excludes_zero
        assert not survives(reference, swept)

    def test_a_reference_that_never_concluded_anything_cannot_survive(self):
        """If the unswept comparison did not exclude zero there is no
        conclusion to survive, and reporting one as robust would be inventing
        a finding out of a null result."""
        null = paired_difference(
            {s: 0.2 for s in range(100)}, {s: 0.2 for s in range(100)}, metric="m"
        )
        assert not survives(null, null)
