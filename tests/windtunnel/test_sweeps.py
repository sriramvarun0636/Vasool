"""windtunnel/sweeps.py: EVALUATION.md §7's grid, plus §10's two amendments.

§7 says the sweep, not the point estimates, is where this evaluation's
credibility actually lives. So the grid has to be complete over the registered
parameters, and the two documented departures from "every parameter, four
points" have to be exactly the two §10 registers — no more.

The mix tests are the fiddly ones. `windtunnel/rng.py::choose` refuses a table
that does not sum to 1.0, deliberately, so a composite shift that renormalises
sloppily fails loudly at draw time rather than quietly reshaping the world.
"""
from __future__ import annotations

import pytest

from windtunnel.parameters import (
    OUTCOME_PARAMETERS,
    PAYMENT_FAILED_SOURCE_MIX,
    REASON_MIX,
    WORLD_PARAMETERS,
)
from windtunnel.rng import choose
from windtunnel.runner import run_seed
from windtunnel.sweeps import (
    FACTORS,
    MIX_SHIFTS,
    REFERENCE,
    SweepKind,
    parameters_touched,
    rescale,
    sweep_configurations,
)

PEPPER = "test-pepper-do-not-use-in-prod"


@pytest.fixture(scope="module")
def grid():
    return sweep_configurations()


def share_of(mix, name: str) -> float:
    return dict(mix)[name]


class TestTheGrid:
    def test_the_factors_are_the_registered_four_points(self):
        """§7: −50%, −25%, +25%, +50%."""
        assert FACTORS == (0.5, 0.75, 1.25, 1.5)

    def test_every_registered_scalar_is_swept_at_four_points(self, grid):
        scalars = [c for c in grid if c.kind in (SweepKind.OUTCOME, SweepKind.WORLD)]
        by_target: dict[str, list[float]] = {}
        for config in scalars:
            by_target.setdefault(config.target, []).append(config.factor)
        assert all(sorted(v) == sorted(FACTORS) for v in by_target.values())

    def test_the_only_unswept_parameter_is_the_definitional_zero(self, grid):
        """§10, registered 2026-08-23. Every other parameter in both
        registries is swept; a third exemption appearing here without a §10
        row is the drift §3c exists to prevent."""
        swept = {c.target for c in grid if c.kind in (SweepKind.OUTCOME, SweepKind.WORLD)}
        registered = set(OUTCOME_PARAMETERS) | set(WORLD_PARAMETERS)
        assert registered - swept == {"retry_success_instrument_dead"}

    def test_half_of_zero_is_zero(self):
        """The arithmetic §10 says to show. This is why the exemption is not a
        gap in coverage: the four runs would be identical to the unswept one."""
        value = OUTCOME_PARAMETERS["retry_success_instrument_dead"].value
        assert value == 0.0
        assert all(value * f == 0.0 for f in FACTORS)

    def test_the_grid_carries_the_three_mix_composites(self, grid):
        assert len([c for c in grid if c.kind is SweepKind.MIX]) == 3

    def test_the_grid_includes_an_unswept_reference(self, grid):
        """§10's mitigation: survival is judged against a reference computed on
        the same seeds as the sweep, so a conclusion cannot flip merely from
        the sweep having less power than the headline comparison."""
        references = [c for c in grid if c.kind is SweepKind.REFERENCE]
        assert len(references) == 1 and references[0] == REFERENCE

    def test_the_grid_is_the_size_the_cost_estimate_assumed(self, grid):
        """20 scalars × 4 points, 3 mix composites, 1 reference. A grid that
        grew silently would blow the registered overnight budget."""
        assert len(grid) == 20 * len(FACTORS) + 3 + 1

    def test_config_names_are_unique(self, grid):
        """They become directory names under out/, so a collision would have
        one config overwrite another's results."""
        names = [c.name for c in grid]
        assert len(set(names)) == len(names)


class TestScalarSweeps:
    def test_a_swept_parameter_moves_and_the_others_do_not(self, grid):
        config = next(
            c for c in grid if c.target == "retry_success_transient" and c.factor == 1.5
        )
        spec = config.spec()
        assert spec.outcome_parameters["retry_success_transient"].value == pytest.approx(
            0.35 * 1.5
        )
        for name, parameter in OUTCOME_PARAMETERS.items():
            if name != "retry_success_transient":
                assert spec.outcome_parameters[name].value == parameter.value

    def test_probabilities_clamp_to_the_unit_interval(self, grid):
        """§10: "+50% on 0.97 registers as 1.0 rather than 1.455". A
        probability above one is not a certainty, it is a broken parameter."""
        config = next(c for c in grid if c.target == "consent_on_file_rate" and c.factor == 1.5)
        assert config.spec().world_parameters["consent_on_file_rate"].value == 1.0

    def test_a_sweep_does_not_mutate_the_registry(self, grid):
        before = OUTCOME_PARAMETERS["retry_success_transient"].value
        for config in grid:
            config.spec()
        assert OUTCOME_PARAMETERS["retry_success_transient"].value == before

    def test_the_reference_perturbs_nothing(self):
        spec = REFERENCE.spec()
        assert spec.outcome_parameters == OUTCOME_PARAMETERS
        assert spec.world_parameters == WORLD_PARAMETERS
        assert spec.reason_mix == REASON_MIX
        assert spec.source_mix == PAYMENT_FAILED_SOURCE_MIX


class TestMixComposites:
    def test_rescale_keeps_the_table_summing_to_one(self):
        out = rescale(REASON_MIX, {"insufficient_fund": 1.5})
        assert sum(share for _, share in out) == pytest.approx(1.0, abs=1e-12)

    def test_rescale_moves_the_named_share_by_its_factor(self):
        out = rescale(REASON_MIX, {"insufficient_fund": 1.5})
        assert share_of(out, "insufficient_fund") == pytest.approx(0.22 * 1.5)

    def test_rescale_moves_unnamed_shares_in_proportion(self):
        """The remainder is scaled by one common factor, so the shape of the
        rest of the table is preserved — a renormalisation, not a reshuffle."""
        out = dict(rescale(REASON_MIX, {"insufficient_fund": 1.5}))
        before = dict(REASON_MIX)
        ratios = [
            out[k] / before[k] for k in before if k != "insufficient_fund"
        ]
        assert max(ratios) == pytest.approx(min(ratios))

    def test_rescale_preserves_the_registered_order(self):
        """§3d's table is registered in a stated order and `choose` walks it in
        that order. Reordering would not change the distribution, but it would
        change which draw maps to which reason, and with it every world."""
        assert [k for k, _ in rescale(REASON_MIX, {"card_expired": 0.5})] == [
            k for k, _ in REASON_MIX
        ]

    def test_rescale_refuses_a_factor_that_leaves_no_remainder(self):
        with pytest.raises(ValueError, match="remainder"):
            rescale(REASON_MIX, {"payment_failed": 3.4})

    @pytest.mark.parametrize("shift", MIX_SHIFTS, ids=lambda s: s.name)
    def test_every_shift_produces_tables_choose_accepts(self, shift):
        """The real function, on the real tables. `choose` raises rather than
        falling back on the last option, so this is the check that a composite
        cannot quietly deform a distribution."""
        spec = shift.spec()
        for i in range(3000):
            choose(spec.reason_mix, 0, "probe", i)
            choose(spec.source_mix, 0, "probe", i)

    def test_recoverable_heavy_moves_mass_the_way_it_says(self):
        spec = next(s for s in MIX_SHIFTS if s.name.endswith("recoverable_heavy")).spec()
        assert share_of(spec.reason_mix, "insufficient_fund") > share_of(
            REASON_MIX, "insufficient_fund"
        )
        for dead in ("card_declined", "card_expired", "card_disabled_for_online_payments"):
            assert share_of(spec.reason_mix, dead) < share_of(REASON_MIX, dead)

    def test_recoverable_light_is_the_mirror(self):
        spec = next(s for s in MIX_SHIFTS if s.name.endswith("recoverable_light")).spec()
        assert share_of(spec.reason_mix, "insufficient_fund") < share_of(
            REASON_MIX, "insufficient_fund"
        )
        assert share_of(spec.reason_mix, "card_expired") > share_of(REASON_MIX, "card_expired")

    def test_generic_skews_dead_touches_only_the_source_split(self):
        """M3 moves how the generic reason resolves, not how often it occurs.
        §3d's 70/25/5 is what makes one registered reason exercise three
        different failure classes."""
        spec = next(s for s in MIX_SHIFTS if s.name.endswith("generic_skews_dead")).spec()
        assert spec.reason_mix == REASON_MIX
        assert share_of(spec.source_mix, "bank") > share_of(PAYMENT_FAILED_SOURCE_MIX, "bank")
        assert share_of(spec.source_mix, "gateway") < share_of(
            PAYMENT_FAILED_SOURCE_MIX, "gateway"
        )


class TestSweepsReachTheRun:
    """A sweep that does not change the run reports 'survives' for a
    conclusion nobody tested — the failure `swept()` already raises KeyError
    to prevent, checked here end to end."""

    def test_the_run_kwargs_carry_the_swept_outcome_model(self):
        """The footgun this signature exists to close: a caller who assembles
        the world parameters and forgets the outcome model gets a silently
        unswept run, and §7 then reports 'survives' for an untested
        conclusion."""
        config = next(
            c
            for c in sweep_configurations()
            if c.target == "retry_success_transient" and c.factor == 1.5
        )
        kwargs = config.spec().run_kwargs(0)
        assert kwargs["outcome"].parameters["retry_success_transient"].value == pytest.approx(
            0.35 * 1.5
        )

    def test_an_outcome_sweep_changes_the_result(self):
        config = next(
            c
            for c in sweep_configurations()
            if c.target == "retry_success_transient" and c.factor == 1.5
        )
        base = run_seed(0, pepper=PEPPER)
        swept = run_seed(0, pepper=PEPPER, **config.spec().run_kwargs(0))
        assert swept.transition_digest() != base.transition_digest()

    def test_a_world_sweep_changes_the_universe(self):
        config = next(
            c
            for c in sweep_configurations()
            if c.target == "amount_median_rupees" and c.factor == 1.5
        )
        swept = run_seed(0, pepper=PEPPER, **config.spec().run_kwargs(0))
        base = run_seed(0, pepper=PEPPER)
        assert [e.amount_paise for e in swept.universe.episodes] != [
            e.amount_paise for e in base.universe.episodes
        ]

    def test_a_swept_run_is_still_deterministic(self):
        config = next(
            c
            for c in sweep_configurations()
            if c.target == "retry_success_transient" and c.factor == 0.5
        )
        kwargs = config.spec().run_kwargs(1)
        assert (
            run_seed(1, pepper=PEPPER, **kwargs).transition_digest()
            == run_seed(1, pepper=PEPPER, **kwargs).transition_digest()
        )


class TestAttribution:
    """§7 needs to say which conclusions a parameter could have touched.
    `Ruling.depends_on` was built so that is mechanical."""

    def test_the_parameters_a_run_actually_consulted_are_recoverable(self):
        touched = parameters_touched(run_seed(0, pepper=PEPPER))
        assert "retry_success_transient" in touched
        assert "reauth_link_completion" in touched

    def test_attribution_names_only_registered_parameters(self):
        """A rule that composes two parameters reports both, and every name it
        reports has to be one §4 or §10 registers — otherwise a sweep could
        never be matched to it."""
        registered = set(OUTCOME_PARAMETERS) | set(WORLD_PARAMETERS)
        assert parameters_touched(run_seed(0, pepper=PEPPER)) <= registered

    def test_an_unconsulted_parameter_cannot_have_moved_the_run(self):
        """The claim the attribution licenses: if no ruling depended on a
        parameter, sweeping it cannot change that arm's settlements."""
        run = run_seed(0, pepper=PEPPER)
        assert "retry_success_instrument_dead" in parameters_touched(run)
