"""The attack registry, and the discipline that keeps it honest.

Two things are being enforced here, and only one of them is about whether the
attacks pass.

**An attack must not be able to score itself.** The survival criterion was
registered before any attack was written, and `criterion.judge` is the only
thing in the codebase that can produce a `Survival`. So `attacks.py` may not
contain an `assert`, may not name `judge` or `Survival`, and every `run` is
typed to return None with its return value discarded by the harness. An attack
that could decide its own verdict is an attack that would be quietly shaped
until it passed.

**A registered expectation is part of the attack.** Nine of the twenty-two are
registered as expected failures — three of those are already recorded as open
failures in the project's own documentation, and the rest came out of reading
the policy plane. The suite asserts actual == registered, not actual ==
survived, so a known failure keeps the suite green while it stands, and going
red is exactly what should happen the day someone fixes one without saying so.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from windtunnel.adversary.attacks import ATTACKS
from windtunnel.adversary.criterion import UNIVERSAL_CLAUSES, Expectation
from windtunnel.adversary.harness import run_all, run_attack

ATTACKS_FILE = pathlib.Path(__file__).resolve().parents[2] / "windtunnel/adversary/attacks.py"


@pytest.fixture(scope="module")
def results():
    return run_all()


class TestTheRegistry:
    def test_the_count_is_what_was_registered(self):
        assert len(ATTACKS) == 22

    def test_every_id_is_unique(self):
        ids = [a.id for a in ATTACKS]
        assert len(ids) == len(set(ids))

    def test_every_attack_names_what_it_targets_and_where_the_weakness_is_recorded(self):
        for attack in ATTACKS:
            assert attack.title and attack.targets and attack.source, attack.id

    def test_every_attack_declares_evidence_of_its_own(self):
        """The three universal clauses hold for every attack by construction.
        An attack that added nothing to them would be scored purely on whether
        the run happened to trip §2a, which is not an attack — it is a run."""
        for attack in ATTACKS:
            assert attack.evidence, f"{attack.id} declares no evidence"

    def test_both_outcomes_are_registered(self):
        outcomes = {a.expectation for a in ATTACKS}
        assert outcomes == {Expectation.SURVIVES, Expectation.FAILS}


class TestAnAttackCannotScoreItself:
    @pytest.fixture(scope="class")
    def tree(self):
        return ast.parse(ATTACKS_FILE.read_text())

    def test_no_attack_asserts_anything(self, tree):
        offenders = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
        assert not offenders, f"assert at attacks.py:{offenders}"

    def test_the_scoring_names_appear_nowhere_in_the_attacks(self, tree):
        forbidden = {"judge", "Survival", "Clause", "run_all", "run_attack"}
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                used |= {alias.name for alias in node.names}
        assert not (used & forbidden), sorted(used & forbidden)

    def test_every_run_returns_none(self, tree):
        """Typed and enforced: the harness discards the return value, so an
        attack cannot hand a verdict back even by accident."""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("a"):
                assert isinstance(node.returns, ast.Constant) and node.returns.value is None, (
                    f"{node.name} must be annotated -> None"
                )

    def test_the_scan_actually_covers_something(self, tree):
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert len(functions) >= len(ATTACKS)


class TestEveryAttackIsScoredTheSameWay:
    def test_every_verdict_carries_the_three_universal_clauses(self, results):
        for result in results:
            names = {c.name for c in result.survival.clauses}
            assert names >= set(UNIVERSAL_CLAUSES), result.attack.id

    def test_the_verdict_is_the_conjunction_of_its_clauses(self, results):
        for result in results:
            assert result.survival.survived == all(c.held for c in result.survival.clauses)

    def test_every_attack_ran_against_a_ledger_that_exists(self, results):
        """A scene with no receipts at all would pass every clause vacuously.
        Every attack must actually reach the policy plane."""
        for result in results:
            assert result.receipts > 0, result.attack.id


class TestOutcomes:
    def test_each_attack_matches_its_registered_expectation(self, results):
        drifted = [
            f"{r.attack.id} registered {r.attack.expectation.value}, actually "
            f"{'survived' if r.survival.survived else 'failed'}"
            for r in results
            if r.survival.survived is not (r.attack.expectation is Expectation.SURVIVES)
        ]
        assert not drifted, "\n".join(drifted)

    def test_the_known_open_failures_still_fail(self, results):
        """docs/taxonomy.md §9.3, §9.10 and vasool/events/schemas.py's own
        KNOWN LIMITATION. These are the attacks with a known answer, and they
        are what proves the harness can detect a real failure at all — if they
        ever pass, either the gap was closed or the harness went blind."""
        by_id = {r.attack.id: r for r in results}
        for attack_id in ("A01", "A07", "A08"):
            assert not by_id[attack_id].survival.survived, attack_id

    def test_every_failure_names_the_clause_it_failed(self, results):
        for result in results:
            if not result.survival.survived:
                assert result.survival.failed(), result.attack.id


class TestDeterminism:
    def test_every_attack_replays_to_the_same_ledger(self):
        """CLAUDE.md invariant 5, per attack. Runs each one twice."""
        for attack in ATTACKS:
            first = run_attack(attack)
            second = run_attack(attack)
            assert first.digest == second.digest, attack.id
            assert first.survival.survived == second.survival.survived, attack.id
