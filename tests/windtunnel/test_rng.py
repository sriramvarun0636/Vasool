"""windtunnel/rng.py: coordinate-addressed randomness.

The property every test here exists to protect is stated in EVALUATION.md §5:
every arm runs on "identical arrivals and identical outcome draws". A stateful
generator cannot deliver that — the moment `naive_retry` makes one more call
than Vasool, every subsequent draw in that arm shifts, and the arms are paired
on the universe but not on the draws. §6a's paired inference is the only thing
making these comparisons statistically worth anything, so the pairing is not a
nicety.

Addressing the draws by coordinate instead of by call order makes the pairing
structural: "does episode E's attempt 2 succeed" has one answer per seed, and
every arm that asks the question gets it.
"""
from __future__ import annotations

import statistics

from windtunnel.rng import (
    bernoulli,
    choose,
    draw,
    exponential,
    integer,
    lognormal,
    poisson,
    uniform,
)


class TestDraw:
    def test_every_draw_is_a_unit_interval_variate(self):
        values = [draw(s, "episode", i) for s in range(20) for i in range(20)]
        assert all(0.0 <= v < 1.0 for v in values)

    def test_the_same_coordinates_always_give_the_same_value(self):
        assert draw(7, "pay_abc", "retry_success", 2) == draw(7, "pay_abc", "retry_success", 2)

    def test_the_seed_is_a_coordinate_like_any_other(self):
        assert draw(7, "pay_abc", "x") != draw(8, "pay_abc", "x")

    def test_different_coordinates_give_different_values(self):
        assert draw(7, "pay_abc", "x", 1) != draw(7, "pay_abc", "x", 2)
        assert draw(7, "pay_abc", "x", 1) != draw(7, "pay_abd", "x", 1)
        assert draw(7, "pay_abc", "x", 1) != draw(7, "pay_abc", "y", 1)

    def test_coordinates_cannot_collide_by_concatenation(self):
        """("ab", "c") and ("a", "bc") are different addresses and must stay
        so — a separator-free basis would make them the same draw, which is
        the classic way an addressing scheme silently correlates two things
        that were meant to be independent."""
        assert draw(1, "ab", "c") != draw(1, "a", "bc")

    def test_call_order_does_not_affect_any_value(self):
        """The pairing property, directly. One 'arm' asks three questions,
        another asks the same three with two extra questions interleaved;
        the three shared answers must be identical."""
        lean = [draw(3, "pay_1", "success", i) for i in (1, 2, 3)]

        busy = []
        for i in (1, 2, 3):
            draw(3, "pay_1", "noise", i)
            busy.append(draw(3, "pay_1", "success", i))
            draw(3, "pay_2", "noise", i)

        assert lean == busy

    def test_values_are_stable_across_processes_and_releases(self):
        """Pins the wire format of the draw itself. architectural invariant 5 is
        a claim about reproducing a ledger tomorrow, on another machine —
        which a salted or version-dependent hash would quietly break while
        every other test in this file still passed."""
        assert draw(0, "pay_TSOPJqQGAvaA2K", "retry_success", 1) == 0.8714137448500499

    def test_the_distribution_is_flat_enough_to_be_a_uniform(self):
        values = [draw(0, "episode", i) for i in range(4000)]
        assert 0.48 < statistics.fmean(values) < 0.52
        deciles = [0] * 10
        for v in values:
            deciles[int(v * 10)] += 1
        assert all(330 < count < 470 for count in deciles), deciles


class TestBernoulli:
    def test_probability_zero_never_fires(self):
        assert not any(bernoulli(0.0, s, "x") for s in range(200))

    def test_probability_one_always_fires(self):
        assert all(bernoulli(1.0, s, "x") for s in range(200))

    def test_the_rate_matches_the_probability(self):
        fired = sum(bernoulli(0.35, 0, "episode", i) for i in range(4000))
        assert 0.32 < fired / 4000 < 0.38

    def test_a_larger_probability_is_a_superset_of_a_smaller_one(self):
        """Because the comparison is `u < p` against a fixed u, raising p can
        only ever turn a False into a True. §7 sweeps every parameter ±50%,
        and this is what stops a sweep from re-rolling the world underneath
        the parameter it is supposed to be isolating."""
        for i in range(500):
            if bernoulli(0.2, 0, "e", i):
                assert bernoulli(0.6, 0, "e", i)


class TestDerivedDistributions:
    def test_uniform_stays_inside_its_bounds(self):
        values = [uniform(10.0, 20.0, 0, "x", i) for i in range(500)]
        assert all(10.0 <= v < 20.0 for v in values)
        assert 14.5 < statistics.fmean(values) < 15.5

    def test_integer_is_inclusive_at_both_ends(self):
        values = {integer(1, 3, 0, "x", i) for i in range(200)}
        assert values == {1, 2, 3}

    def test_exponential_has_the_mean_it_was_asked_for(self):
        values = [exponential(9.0, 0, "gap", i) for i in range(4000)]
        assert all(v >= 0.0 for v in values)
        assert 8.4 < statistics.fmean(values) < 9.6

    def test_poisson_has_the_lambda_it_was_asked_for(self):
        values = [poisson(1.0, 0, "count", i) for i in range(4000)]
        assert all(v >= 0 for v in values)
        assert 0.93 < statistics.fmean(values) < 1.07

    def test_poisson_produces_a_real_tail_not_just_zero_and_one(self):
        values = [poisson(1.0, 0, "count", i) for i in range(4000)]
        assert max(values) >= 4

    def test_lognormal_has_the_median_it_was_asked_for(self):
        values = [lognormal(1200.0, 1.4, 0, "amount", i) for i in range(4000)]
        assert all(v > 0.0 for v in values)
        assert 1100 < statistics.median(values) < 1300

    def test_lognormal_has_a_heavy_enough_tail_to_reach_the_big_thresholds(self):
        """AFAThresholdGuard fires above ₹15,000 and HumanApprovalGuard above
        ₹50,000. A distribution that never reaches them would leave two of
        the thirteen guards permanently unexercised, and the report card
        would show them as passing when they had never run."""
        values = [lognormal(1200.0, 1.4, 0, "amount", i) for i in range(20000)]
        assert sum(v > 15_000 for v in values) > 100
        assert sum(v > 50_000 for v in values) > 5


class TestChoose:
    OPTIONS = (("a", 0.5), ("b", 0.3), ("c", 0.2))

    def test_picks_options_at_their_registered_share(self):
        picks = [choose(self.OPTIONS, 0, "reason", i) for i in range(6000)]
        for option, share in self.OPTIONS:
            assert abs(picks.count(option) / 6000 - share) < 0.02

    def test_is_deterministic_for_a_coordinate(self):
        assert choose(self.OPTIONS, 0, "reason", 1) == choose(self.OPTIONS, 0, "reason", 1)

    def test_a_zero_share_option_is_never_picked(self):
        options = (("never", 0.0), ("always", 1.0))
        assert {choose(options, 0, "x", i) for i in range(500)} == {"always"}
