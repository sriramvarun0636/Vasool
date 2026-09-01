"""windtunnel/evaluate.py: the driver behind `make eval`.

Three things matter here and none of them is a number. The protocol has to be
**resumable**, because the sweep grid is an overnight run and a crash at hour
six must not mean starting again. It has to **not touch the holdout**, by
default or by accident. And it has to write something a hostile reader can
open — machine-readable, with the eight §2a claims by name rather than a bare
"passed".

The seed counts here are small on purpose. What is under test is the plumbing;
the registered protocol is 1000 seeds and lives behind `make eval`.
"""
from __future__ import annotations

import json

import pytest

from windtunnel.arms import VASOOL, arm_named
from windtunnel.evaluate import (
    BASE_CONFIG,
    F6_DENOMINATOR,
    F6_PARTIAL_GRID,
    F6_THRESHOLD,
    REGISTERED_SEEDS,
    SWEEP_SEEDS,
    ZERO_DIFFERENCE_DETAIL,
    _Base,
    _shard,
    collect,
    compare,
    determinism_check,
    f6_verdict,
    falsification,
    main,
    reference_differences,
    selected_grid,
    sweep_targets,
    sweep_verdicts,
)
from windtunnel.split import Cohort, HoldoutSealed
from windtunnel.sweeps import REFERENCE, sweep_configurations

PEPPER = "test-pepper-do-not-use-in-prod"
ARMS = (VASOOL, arm_named("vasool_ungated"))


def gather(out, seeds, arms=ARMS, workers=1):
    return collect(
        out=out,
        configs=[_Base()],
        arms=arms,
        seeds=list(seeds),
        cohort=Cohort.DEVELOPMENT.value,
        pepper=PEPPER,
        unseal=None,
        workers=workers,
        progress=False,
    )


class TestRegisteredRanges:
    def test_the_base_protocol_runs_the_registered_thousand(self):
        """§6a fixes the range at 0..999. A smaller default would be an
        amendment, and §10 is where amendments go."""
        assert list(REGISTERED_SEEDS) == list(range(1000))

    def test_the_sweep_range_is_the_amended_two_hundred(self):
        """§10, 2026-08-23."""
        assert list(SWEEP_SEEDS) == list(range(200))


class TestResumability:
    def test_a_completed_seed_is_not_recomputed(self, tmp_path):
        first = gather(tmp_path, range(3))
        shard = _shard(tmp_path, BASE_CONFIG, "vasool")
        before = shard.read_text()

        second = gather(tmp_path, range(3))
        assert shard.read_text() == before
        assert first[BASE_CONFIG]["vasool"].keys() == second[BASE_CONFIG]["vasool"].keys()

    def test_an_interrupted_run_resumes_where_it_stopped(self, tmp_path):
        gather(tmp_path, range(2))
        resumed = gather(tmp_path, range(4))
        assert sorted(resumed[BASE_CONFIG]["vasool"]) == [0, 1, 2, 3]

    def test_a_half_written_line_costs_exactly_one_seed(self, tmp_path):
        """The only way to get a torn line is a kill mid-write. It must not be
        fatal, and the seed it belonged to must come back — which it can,
        byte-identically, under invariant 5."""
        gather(tmp_path, range(3))
        shard = _shard(tmp_path, BASE_CONFIG, "vasool")
        shard.write_text(shard.read_text() + '{"seed": 3, "recov')

        resumed = gather(tmp_path, range(4))
        assert sorted(resumed[BASE_CONFIG]["vasool"]) == [0, 1, 2, 3]
        assert resumed[BASE_CONFIG]["vasool"][3]["recovery_rate"] > 0

    def test_shards_are_per_config_and_per_arm(self, tmp_path):
        gather(tmp_path, range(2))
        assert _shard(tmp_path, BASE_CONFIG, "vasool").exists()
        assert _shard(tmp_path, BASE_CONFIG, "vasool_ungated").exists()

    def test_workers_produce_the_same_rows_as_serial(self, tmp_path):
        """Parallelism is an implementation detail of the driver, not of the
        result. Runs share nothing, so the pool must not change a number."""
        serial = gather(tmp_path / "a", range(4), workers=1)
        pooled = gather(tmp_path / "b", range(4), workers=4)
        for seed in range(4):
            assert (
                serial[BASE_CONFIG]["vasool"][seed]["recovery_rate"]
                == pooled[BASE_CONFIG]["vasool"][seed]["recovery_rate"]
            )


class TestTheHoldoutStaysSealed:
    def test_the_default_cohort_is_development(self, tmp_path):
        rows = gather(tmp_path, range(2))[BASE_CONFIG]["vasool"]
        assert all(row["cohort"] == Cohort.DEVELOPMENT.value for row in rows.values())

    def test_a_holdout_run_without_the_phrase_raises(self, tmp_path):
        with pytest.raises(HoldoutSealed):
            collect(
                out=tmp_path, configs=[_Base()], arms=[VASOOL], seeds=[0],
                cohort=Cohort.HOLDOUT.value, pepper=PEPPER, unseal=None,
                workers=1, progress=False,
            )

    def test_the_cli_refuses_a_holdout_without_the_phrase(self, tmp_path):
        with pytest.raises(HoldoutSealed, match="once"):
            main(["--cohort", "holdout", "--seeds", "1", "--out", str(tmp_path)], pepper=PEPPER)

    def test_development_and_holdout_write_to_different_trees(self, tmp_path):
        assert Cohort.DEVELOPMENT.directory != Cohort.HOLDOUT.directory


class TestTheReport:
    def test_a_row_carries_all_eight_claims_by_name(self, tmp_path):
        row = gather(tmp_path, range(1))[BASE_CONFIG]["vasool"][0]
        assert len(row["safety"]) == 8
        assert row["safety_holds"] is True
        assert all("passed" in c and "violations" in c for c in row["safety"].values())

    def test_rows_are_json_serialisable(self, tmp_path):
        gather(tmp_path, range(2))
        for line in _shard(tmp_path, BASE_CONFIG, "vasool").read_text().splitlines():
            assert json.loads(line)["arm"] == "vasool"

    def test_comparisons_are_paired_against_vasool(self, tmp_path):
        results = gather(tmp_path, range(8))[BASE_CONFIG]
        comparisons = compare(results)
        assert "vasool" not in comparisons
        assert "recovery_rate" in comparisons["vasool_ungated"]
        assert comparisons["vasool_ungated"]["recovery_rate"]["n_seeds"] == 8

    def test_falsification_reports_every_registered_criterion(self, tmp_path):
        results = gather(tmp_path, range(6), arms=None or (
            VASOOL,
            arm_named("vasool_ungated"),
            arm_named("retry_plus_contact"),
            arm_named("A2"),
            arm_named("A3"),
        ))[BASE_CONFIG]
        report = falsification(results, compare(results))
        assert [k[:2] for k in report] == ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]

    def test_f5_is_measured_in_absolute_points(self, tmp_path):
        """§10, 2026-08-23. A relative figure inflates as the base rate falls,
        which would make the guards look most expensive exactly where there
        was least to lose."""
        results = gather(tmp_path, range(6))[BASE_CONFIG]
        f5 = falsification(results, compare(results))["F5_compliance_unaffordable"]
        assert f5["threshold_pp"] == 20.0
        assert "absolute" in f5["detail"]

    def test_f6_and_f7_are_not_decided_by_the_base_protocol(self, tmp_path):
        """F6 needs the sweep grid and F7 is a separate determinism check.
        Reporting either as "did not fire" from the base run alone would claim
        a result nothing produced."""
        results = gather(tmp_path, range(4))[BASE_CONFIG]
        report = falsification(results, compare(results))
        assert report["F6_conclusions_are_model_artifacts"]["fired"] is None
        assert report["F7_determinism_fails"]["fired"] is None


class TestDeterminismCheck:
    def test_two_runs_of_a_seed_produce_identical_ledgers(self):
        """architectural invariant 5 and §9's F7, at ledger-digest level."""
        result = determinism_check([0, 1], pepper=PEPPER)
        assert result["identical"] and not result["mismatches"]


class TestEndToEnd:
    def test_the_cli_writes_a_report(self, tmp_path):
        assert main(["--seeds", "2", "--workers", "2", "--out", str(tmp_path)], pepper=PEPPER) == 0

        report = json.loads((tmp_path / "development" / "evaluation.json").read_text())
        assert report["cohort"] == "development"
        assert len(report["arms"]) == 9
        assert set(report["per_arm"]) == set(report["arms"])
        assert report["determinism"]["identical"]

    def test_the_report_never_contains_the_pepper(self, tmp_path):
        """the project rules: never write a secret's value into any file. The customer
        ids in a universe are HMACs keyed on it, and the report records only
        that it was configured."""
        main(["--seeds", "1", "--workers", "1", "--out", str(tmp_path)], pepper=PEPPER)
        text = (tmp_path / "development" / "evaluation.json").read_text()
        assert PEPPER not in text
        assert json.loads(text)["pepper_configured"] is True

    def test_a_missing_pepper_is_refused(self, tmp_path):
        """The value arrives as an argument because nothing in windtunnel/ may
        read the environment — `tools/evaluate.py` does that lookup. An empty
        one would silently produce a universe nobody can reproduce."""
        with pytest.raises(ValueError, match="pepper is required"):
            main(["--seeds", "1", "--out", str(tmp_path)], pepper="")

    def test_the_entry_point_lives_outside_the_package(self):
        """The env read has to be somewhere the package scan does not reach,
        and `make eval` has to go through it."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent.parent
        assert "VASOOL_ID_PEPPER" in (root / "tools" / "evaluate.py").read_text()
        assert "tools/evaluate.py" in (root / "Makefile").read_text()


# ---------------------------------------------------------------------------
# §7's block, and §9's F6 read off it
# ---------------------------------------------------------------------------
SWEEP_ARMS = (VASOOL, arm_named("naive_retry"), arm_named("A4"))
"""Vasool, one baseline that moves, and the one arm that cannot. Three seeds
and two configurations: what is under test is the block's shape and F6's
arithmetic, not any number in it."""


@pytest.fixture(scope="module")
def grid(tmp_path_factory):
    """A miniature §7 grid: the reference plus one swept configuration."""
    config = next(
        c for c in sweep_configurations() if c.name.startswith("retry_success_transient@")
    )
    swept = collect(
        out=tmp_path_factory.mktemp("grid"),
        configs=[REFERENCE, config],
        arms=SWEEP_ARMS,
        seeds=[0, 1, 2],
        cohort=Cohort.DEVELOPMENT.value,
        pepper=PEPPER,
        unseal=None,
        workers=1,
        progress=False,
    )
    reference = reference_differences(swept)
    return sweep_verdicts(swept[config.name], reference, config=config)


class TestSweepBlockShape:
    """§10, 2026-08-24. The block used to carry one boolean per arm, which
    collapses §7 to a sign test and throws the effect size away — 83
    configurations rendered one distinct block. The shape is pinned here
    because nothing else would have caught that: every value in it was
    correct."""

    def test_the_configuration_is_named_by_kind_target_and_factor(self, grid):
        assert grid["kind"] == "outcome"
        assert grid["target"] == "retry_success_transient"
        assert grid["factor"] in (0.5, 0.75, 1.25, 1.5)

    def test_every_arm_carries_an_interval_beside_the_boolean(self, grid):
        assert set(grid["arms"]) == {"naive_retry", "A4"}
        for arm, entry in grid["arms"].items():
            assert isinstance(entry["survives"], bool), arm
            assert entry["interval"]["n_seeds"] == 3, arm

    def test_the_interval_is_the_object_the_headline_comparison_emits(self, grid, tmp_path):
        """§7's grid and §6a's paired comparison must serialise one shape. Two
        shapes for the same quantity is how a report card ends up comparing a
        magnitude against a boolean without noticing."""
        headline = compare(gather(tmp_path, range(3), arms=SWEEP_ARMS)[BASE_CONFIG])
        expected = set(headline["naive_retry"]["recovery_rate"])
        for arm, entry in grid["arms"].items():
            assert set(entry["interval"]) == expected, arm

    def test_a_zero_difference_arm_is_noted_and_no_other_arm_is(self, grid):
        """A4 differs from Vasool only in how the guard chain resolves a
        refusal, which never changes an outcome here, so its interval is
        [0, 0] and `survives` is false for a reason no parameter touches."""
        a4 = grid["arms"]["A4"]
        assert (a4["interval"]["point"], a4["interval"]["low"], a4["interval"]["high"]) == (0.0, 0.0, 0.0)
        assert a4["survives"] is False
        assert a4["detail"] == ZERO_DIFFERENCE_DETAIL
        assert "detail" not in grid["arms"]["naive_retry"]


def _grid(configs: int = 3, flips: dict[int, set[str]] | None = None) -> dict:
    """A synthetic §7 grid. Every arm survives everywhere except where told."""
    flips = flips or {}
    return {
        f"knob@{i}": {
            "arms": {
                arm: {"survives": arm not in flips.get(i, set()), "interval": {}}
                for arm in F6_DENOMINATOR
            }
        }
        for i in range(configs)
    }


class TestF6:
    """§9's F6 under §10's registered rule, 2026-08-24."""

    def test_the_denominator_is_the_eight_comparisons_a_parameter_can_move(self):
        assert F6_DENOMINATOR == (
            "naive_retry", "retry_plus_contact", "vasool_ungated", "A1", "A2", "A3", "A4", "A5",
        )
        assert F6_THRESHOLD == 5

    def test_a_grid_in_which_everything_survives_does_not_fire(self):
        verdict = f6_verdict(_grid())
        assert verdict["fired"] is False
        assert verdict["flipped_count"] == 0
        assert verdict["configurations"] == 3

    def test_four_of_eight_does_not_fire_and_five_does(self):
        """§9's "more than half", on a denominator of eight."""
        four = {"naive_retry", "retry_plus_contact", "A1", "A2"}
        assert f6_verdict(_grid(flips={0: four}))["fired"] is False
        assert f6_verdict(_grid(flips={0: four | {"A3"}}))["fired"] is True

    def test_one_configuration_is_enough_to_flip_a_comparison(self):
        """§9: "under some ±50% sweep" — failing anywhere in the grid counts,
        and the configurations it failed in are reported, because §7 exists to
        say which parameter made a conclusion an artifact."""
        verdict = f6_verdict(_grid(configs=3, flips={1: {"A5"}}))
        assert verdict["flipped"] == {"A5": ["knob@1"]}
        assert verdict["fired"] is False

    def test_a4_now_counts_and_reports(self):
        """A4 was previously excluded due to a stale reason. Now it counts."""
        verdict = f6_verdict(_grid(flips={i: {"A4"} for i in range(3)}))
        assert verdict["fired"] is False
        assert verdict["flipped"] == {"A4": ["knob@0", "knob@1", "knob@2"]}
        assert "A4" in verdict["denominator"]
        assert "excluded" not in verdict

    def test_a_missing_comparison_raises_rather_than_shrinking_the_numerator(self):
        """Skipping an absent arm would make F6 harder to fire, which is the
        one direction an error here must not go."""
        grid = _grid()
        del grid["knob@1"]["arms"]["A3"]
        with pytest.raises(KeyError, match="harder to fire"):
            f6_verdict(grid)


class TestSweepTargetSelection:
    """`--sweep-target` and `--skip-base`: CLI surface over §7's grid.

    Neither may change which configurations exist. `sweep_configurations()`
    stays the registered grid; these choose which of it runs.
    """

    def test_every_registered_knob_and_composite_is_addressable(self):
        targets = sweep_targets()
        assert len(targets) == 23, "20 swept scalars plus §10's three mix composites"
        assert "retry_success_instrument_dead" not in targets, "§10: not swept"
        assert "mix:recoverable_heavy" in targets

    def test_no_target_is_the_whole_registered_grid(self):
        assert selected_grid([]) == sweep_configurations()

    def test_a_target_selects_its_four_points_and_the_reference(self):
        """§10, 2026-08-23: survival is judged against an unswept reference on
        the same seeds, so a subset without it would have nothing to judge
        against."""
        chosen = selected_grid(["amount_sigma_log"])
        assert [c.name for c in chosen] == [
            "reference",
            "amount_sigma_log@0.5",
            "amount_sigma_log@0.75",
            "amount_sigma_log@1.25",
            "amount_sigma_log@1.5",
        ]

    def test_several_targets_compose(self):
        chosen = selected_grid(["amount_sigma_log", "mix:recoverable_heavy"])
        assert [c.name for c in chosen] == [
            "reference",
            "amount_sigma_log@0.5",
            "amount_sigma_log@0.75",
            "amount_sigma_log@1.25",
            "amount_sigma_log@1.5",
            "mix:recoverable_heavy",
        ]

    def test_an_unregistered_target_is_refused_before_anything_runs(self, tmp_path):
        """Silently running nothing would produce an F6 verdict off an empty
        grid, which is the failure this whole flag has to avoid."""
        with pytest.raises(SystemExit) as exit:
            main(["--sweep-target", "not_a_parameter", "--out", str(tmp_path)], pepper=PEPPER)
        assert exit.value.code == 2
        assert not list(tmp_path.iterdir())

    def test_skip_base_without_sweeps_is_refused(self, tmp_path):
        with pytest.raises(SystemExit) as exit:
            main(["--skip-base", "--out", str(tmp_path)], pepper=PEPPER)
        assert exit.value.code == 2
        assert not list(tmp_path.iterdir())

    def test_f6_is_not_reported_off_a_subset(self):
        """§10, 2026-08-24 registers F6 against the whole grid. A subset counts
        fewer flips than the rule allows, so `fired: false` from one would be a
        verdict off work that was never done."""
        assert "whole" in F6_PARTIAL_GRID and "subset" in F6_PARTIAL_GRID
