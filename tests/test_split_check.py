"""tools/split_check.py, held to the registration rather than to itself.

A robustness check is worth exactly what its registration is worth, so nothing
here trusts the tool's own account. The peppers, seeds and metric are read back
out of docs/EVALUATION.md §10's row of 2026-09-14; the verdict is recomputed
from the stored intervals instead of read from the stored boolean; and a run
over fewer than the registered seeds cannot write a file with the registered
check's name.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "split_check.py"
RESULT = REPO_ROOT / "out" / "robustness" / "split_check.json"
MANIFEST = REPO_ROOT / "out" / "development" / "evaluation.json"


def _load_tool():
    spec = importlib.util.spec_from_file_location("_split_check", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SPLIT = _load_tool()


def _registration() -> str:
    protocol = (REPO_ROOT / "docs" / "EVALUATION.md").read_text()
    rows = [line for line in protocol.splitlines()
            if line.startswith("| 2026-09-14 | **A check that would expose a lucky split")]
    assert len(rows) == 1, "the registering row is missing or duplicated"
    return rows[0]


class TestTheToolIsTheRegistration:
    def test_the_peppers_are_the_five_literals_the_row_registers(self):
        assert "`split-check-1` to `split-check-5`" in _registration()
        assert SPLIT.SPLIT_CHECK_PEPPERS == tuple(f"split-check-{i}" for i in range(1, 6))

    def test_the_seeds_arms_and_metric_are_the_row_s(self):
        row = _registration()
        assert "`0..999`" in row
        assert "`paired_vs_vasool.retry_plus_contact.recovery_rate`" in row
        assert (SPLIT.TREATMENT, SPLIT.BASELINE, SPLIT.PRIMARY) == (
            "vasool", "retry_plus_contact", "recovery_rate",
        )
        assert len(SPLIT.REGISTERED_SEEDS) == 1000 and SPLIT.REGISTERED_SEEDS[0] == 0

    def test_no_check_pepper_is_a_world_already_in_use(self):
        from tests.payloads import TEST_PEPPER
        from windtunnel.adversary.arena import ADVERSARY_PEPPER
        from windtunnel.pepper import REGISTERED_PEPPER

        in_use = {REGISTERED_PEPPER, TEST_PEPPER, ADVERSARY_PEPPER}
        assert not set(SPLIT.SPLIT_CHECK_PEPPERS) & in_use

    def test_it_reads_no_environment(self):
        text = TOOL.read_text()
        named = [n for n in ast.walk(ast.parse(text))
                 if isinstance(n, ast.Constant) and n.value == "VASOOL_ID_PEPPER"]
        assert not named
        for needle in ("os.environ", "getenv", "load_dotenv"):
            assert needle not in text, needle


class TestTheRegisteredOutcome:
    """§10: robust if all five paired 95% intervals exclude zero with the
    published sign. Exercised on made-up intervals, so the rule is tested
    independently of whatever the real run returns."""

    BELOW = {"low": -0.20, "high": -0.10}

    def test_five_intervals_below_zero_are_robust_for_a_negative_point(self):
        assert SPLIT.registered_outcome(-0.16, [self.BELOW] * 5)

    def test_one_interval_reaching_zero_is_not(self):
        assert not SPLIT.registered_outcome(-0.16, [self.BELOW] * 4 + [{"low": -0.1, "high": 0.0}])

    def test_one_interval_excluding_zero_on_the_wrong_side_is_not(self):
        assert not SPLIT.registered_outcome(-0.16, [self.BELOW] * 4 + [{"low": 0.01, "high": 0.1}])

    def test_the_rule_mirrors_for_a_positive_published_point(self):
        above = {"low": 0.10, "high": 0.20}
        assert SPLIT.registered_outcome(0.16, [above] * 5)
        assert not SPLIT.registered_outcome(0.16, [above] * 4 + [self.BELOW])


class TestAShortRunCannotImpersonateTheCheck:
    def test_fewer_seeds_than_registered_write_only_a_partial_file(self, tmp_path):
        assert SPLIT.main(["--seeds", "2", "--workers", "2", "--out", str(tmp_path)]) == 0
        assert not (tmp_path / "split_check.json").exists()
        report = json.loads((tmp_path / "split_check_partial.json").read_text())
        assert [c["pepper"] for c in report["checks"]] == list(SPLIT.SPLIT_CHECK_PEPPERS)
        assert all(c["paired"]["n_seeds"] == 2 for c in report["checks"])
        assert report["seeds"]["count"] == 2


class TestTheCommittedResult:
    def _result(self) -> dict:
        if not RESULT.exists():
            pytest.skip("the registered check has not been run on this tree")
        return json.loads(RESULT.read_text())

    def test_its_verdict_is_what_its_own_intervals_say(self):
        result = self._result()
        expected = SPLIT.registered_outcome(
            result["published"]["paired"]["point"], [c["paired"] for c in result["checks"]]
        )
        assert result["robust"] is expected

    def test_it_ran_every_registered_pepper_on_every_registered_seed(self):
        result = self._result()
        assert [c["pepper"] for c in result["checks"]] == list(SPLIT.SPLIT_CHECK_PEPPERS)
        assert result["seeds"] == {"first": 0, "last": 999, "count": 1000}
        assert all(c["paired"]["n_seeds"] == 1000 for c in result["checks"])

    def test_it_was_measured_against_the_numbers_that_are_published(self):
        """If a later re-measurement moves the headline, the robustness claim
        is about numbers no longer on the page — re-run the check, or say in
        README which figures it describes."""
        result, manifest = self._result(), json.loads(MANIFEST.read_text())
        assert result["published"]["paired"] == (
            manifest["paired_vs_vasool"]["retry_plus_contact"]["recovery_rate"]
        )
        assert result["published"]["vasool_recovery_rate_mean"] == (
            manifest["per_arm"]["vasool"]["recovery_rate_mean"]
        )
        assert result["published"]["pepper_sha256"] == manifest["pepper_sha256"]

    def test_readme_quotes_the_verdict_and_the_spread_the_artifact_holds(self):
        """README states the result in prose; the prose is held to the file
        the way the red-team count is held to redteam.json."""
        result = self._result()
        readme = (REPO_ROOT / "README.md").read_text().replace("−", "-")
        low, high = sorted((result["spread"]["point_min"], result["spread"]["point_max"]))
        assert f"between {low * 100:.2f} and {high * 100:.2f} points" in readme
        assert ("The conclusion held in all five" in readme) is result["robust"]
        verdict = "true" if result["robust"] else "false"
        assert f"`out/robustness/split_check.json` → `robust` | `{verdict}` |" in readme

    def test_the_text_table_is_the_json_rendered(self):
        result = self._result()
        assert RESULT.with_suffix(".txt").read_text() == SPLIT.render(result)
