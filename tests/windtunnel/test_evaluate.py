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
    REGISTERED_SEEDS,
    SWEEP_SEEDS,
    _Base,
    _shard,
    collect,
    compare,
    determinism_check,
    falsification,
    main,
)
from windtunnel.split import Cohort, HoldoutSealed

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
        """CLAUDE.md invariant 5 and §9's F7, at ledger-digest level."""
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
        """CLAUDE.md: never write a secret's value into any file. The customer
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
