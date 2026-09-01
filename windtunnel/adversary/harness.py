"""Runs the attacks and scores them. The only caller of `criterion.judge`.

Three lines of this file are the whole discipline: build a fresh arena, call
the attack, hand the arena to `judge`. The attack's return value is discarded
— it is typed `None` and `tests/adversary/test_attacks.py` proves nothing in
`attacks.py` even names the scoring function — so there is no path by which an
attack can decide its own verdict.

A fresh `Arena` per attack, never a shared one. Each attack owns its ledger,
which is what makes `Result.digest` a per-attack replay check (the project rules
invariant 5) rather than a property of whatever order the suite happened to
run in.
"""
from __future__ import annotations

from dataclasses import dataclass

from windtunnel.adversary.arena import Arena
from windtunnel.adversary.attacks import ATTACKS
from windtunnel.adversary.criterion import Attack, Expectation, Survival, judge


@dataclass(frozen=True, slots=True)
class Result:
    """One attack, scored."""

    attack: Attack
    survival: Survival
    receipts: int
    dispatched: int
    digest: str
    """The ledger hash. Two runs of one attack must produce the same one."""

    @property
    def as_registered(self) -> bool:
        """Whether the outcome is the one registered before the run.

        Both directions matter. A registered failure that starts surviving is
        news — either the gap was closed or the attack went blind — and is not
        something a green suite should swallow.
        """
        return self.survival.survived is (self.attack.expectation is Expectation.SURVIVES)


def run_attack(attack: Attack) -> Result:
    arena = Arena()
    attack.run(arena)  # return value deliberately discarded — see module docstring
    survival = judge(arena, attack_id=attack.id, evidence=attack.evidence)
    return Result(
        attack=attack,
        survival=survival,
        receipts=len(arena.ledger()),
        dispatched=len(arena.dispatched()),
        digest=arena.ledger_digest(),
    )


def run_all(attacks: tuple[Attack, ...] = ATTACKS) -> tuple[Result, ...]:
    return tuple(run_attack(attack) for attack in attacks)


def summary(results: tuple[Result, ...]) -> dict:
    """What `tools/redteam.py` prints and writes.

    Reports survival honestly: the count, and every failure named with the
    clause it failed on. A perfect score would read as a weak adversary, so
    the shape of this output makes a clean sheet as visible as a failure.
    """
    survived = [r for r in results if r.survival.survived]
    failed = [r for r in results if not r.survival.survived]
    return {
        "attacks": len(results),
        "survived": len(survived),
        "failed": len(failed),
        "as_registered": all(r.as_registered for r in results),
        "results": [
            {
                "id": r.attack.id,
                "title": r.attack.title,
                "targets": r.attack.targets,
                "source": r.attack.source,
                "expectation": r.attack.expectation.value,
                "survived": r.survival.survived,
                "as_registered": r.as_registered,
                "receipts": r.receipts,
                "dispatched": r.dispatched,
                "ledger_sha256": r.digest,
                "clauses": [
                    {"name": c.name, "held": c.held, "detail": c.detail}
                    for c in r.survival.clauses
                ],
            }
            for r in results
        ],
    }
