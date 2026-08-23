"""EVALUATION.md §7's sensitivity grid, as configurations a run can be given.

§7: "Every parameter in §4 is swept independently at −50%, −25%, +25%, +50% of
its registered value, holding others fixed", and §7 is where this evaluation's
credibility actually lives — a conclusion that holds across ±50% on a guessed
parameter is worth something, a point estimate from a guessed parameter is
worth nothing. Eight of the nine outcome parameters are guesses, and so are all
twelve world-shape ones, so this is not a footnote to the result. It is most of
the result.

**Two registered departures from "every parameter, four points", both in §10.**

  - `retry_success_instrument_dead` is not swept. It is 0.0 and the factors are
    multiplicative, so all four runs would be byte-identical to the unswept
    one. Reporting them as passed sweeps would overstate how much of the
    outcome model has been tested; the report card states the arithmetic
    instead.
  - §3d's mix is swept as three composite configurations rather than as
    thirteen per-share knobs. A per-share sweep has no defined meaning:
    `windtunnel/rng.py::choose` refuses a table that does not sum to 1.0
    exactly, and §7 registers no renormalisation rule, so "scale by ±50%" is
    undefined on a simplex without one. The three composites test what §3d
    itself says is at risk — "weight LIQUIDITY up and every arm improves" — in
    both directions, plus the source split that decides which of three failure
    classes the single largest reason resolves to.

**The grid carries its own reference.** §10 registers §7 as running on seeds
0..199 rather than 0..999, and registers the mitigation in the same breath: a
200-seed interval is about 2.24× wider than a 1000-seed one, so a conclusion
could cross zero from lost power rather than from the parameter, and F6 fires
on flips. `REFERENCE` is the unswept configuration, run on the same 200 seeds,
so `inference.survives` compares like with like.

**Why a sweep isolates its parameter.** `windtunnel/rng.py` is coordinate-
addressed and `bernoulli` is monotone in p, so raising a probability can only
turn a False into a True — it does not re-roll the world underneath. A swept
world parameter likewise changes only the draws that read it. That property is
what makes a one-parameter sweep mean what §7 says it means.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from windtunnel.outcome import OutcomeModel
from windtunnel.parameters import (
    OUTCOME_PARAMETERS,
    PAYMENT_FAILED_SOURCE_MIX,
    REASON_MIX,
    WORLD_PARAMETERS,
    Parameter,
    swept,
)
from windtunnel.runner import RunResult
from windtunnel.universe import Mix

FACTORS: tuple[float, ...] = (0.5, 0.75, 1.25, 1.5)
"""§7's four-point grid: −50%, −25%, +25%, +50%."""

UNSWEPT: dict[str, str] = {
    "retry_success_instrument_dead": (
        "registered at 0.0, and every factor is multiplicative: 0.0 × 0.5 = "
        "0.0 × 1.5 = 0.0. Four runs identical to the unswept one would be "
        "theatre. §4's one non-guess is definitional, and a definition does "
        "not have a sensitivity (EVALUATION.md §10, 2026-08-23)."
    )
}
"""Parameters deliberately outside the grid, with the registered reason. A
name appearing here without a §10 row is exactly the drift §3c exists to
prevent, so tests/windtunnel/test_sweeps.py asserts this dict's contents
against both registries."""

DEAD_INSTRUMENT_REASONS: tuple[str, ...] = (
    "card_declined",
    "card_expired",
    "card_disabled_for_online_payments",
    "payment_risk_check_failed",
)
"""The unrecoverable end of §3d's mix: the three INSTRUMENT_DEAD reasons plus
the explicit RISK_BLOCK one. Named as a block because §3d's stated risk is
about mass moving between the recoverable and unrecoverable ends, not about
any single reason."""

LIQUIDITY_REASON = "insufficient_fund"
"""The recoverable end. Note the singular — Razorpay emits `insufficient_fund`
(taxonomy §5), and the plural would silently miss the row."""


class SweepKind(StrEnum):
    REFERENCE = "reference"
    OUTCOME = "outcome"
    WORLD = "world"
    MIX = "mix"


@dataclass(frozen=True, slots=True)
class WorldSpec:
    """Everything a run needs that §7 can perturb. Immutable, and produced
    fresh per configuration, so a sweep can never leak into the run after it."""

    outcome_parameters: dict[str, Parameter]
    world_parameters: dict[str, Parameter]
    reason_mix: Mix
    source_mix: Mix

    def outcome_model(self, seed: int) -> OutcomeModel:
        """This configuration's outcome model, seeded for one run.

        Seeded per run because out-of-band settlement is decided from it while
        the universe is being built, before any arm runs — so every arm on a
        seed has to be handed the same model (EVALUATION.md §5).
        """
        return OutcomeModel(parameters=self.outcome_parameters, seed=seed)

    def run_kwargs(self, seed: int) -> dict:
        """Every keyword `windtunnel.runner.run_seed` needs for this
        configuration, on this seed.

        `seed` is required, and the outcome model is built here rather than
        left to the caller, because the alternative is the worst failure this
        module can have: a caller who assembles the world parameters and
        forgets the outcome model gets a run that is silently unswept, and §7
        then reports "survives all sweeps" for a conclusion nothing ever
        tested. Making the model impossible to omit is worth the coupling.
        """
        return {
            "outcome": self.outcome_model(seed),
            "parameters": self.world_parameters,
            "reason_mix": self.reason_mix,
            "source_mix": self.source_mix,
        }


def rescale(table: Mix, factors: Mapping[str, float]) -> Mix:
    """Scale named shares, then renormalise the rest to bring the table to 1.0.

    The rule §10 registers, and the only one that makes "scale a share by
    ±50%" well defined: named shares move by their factor, and every unnamed
    share moves by a single common factor, so the shape of the remainder is
    preserved rather than reshuffled.

    The registered order is preserved because `choose` walks the table in
    order — reordering would not change the distribution, but it would change
    which draw maps to which reason, and with it every world.

    The last share absorbs the floating-point residue, so the accumulation
    `choose` performs lands on 1.0 rather than a hair under it. Without that,
    one draw in ~10^16 falls past the end of the table and raises.
    """
    named_total = sum(share * factors[name] for name, share in table if name in factors)
    unnamed_total = sum(share for name, share in table if name not in factors)
    remainder = 1.0 - named_total
    if remainder <= 0.0 or unnamed_total <= 0.0:
        raise ValueError(
            f"scaling {sorted(factors)} leaves a remainder of {remainder:.4f} for the "
            f"{unnamed_total:.4f} of unnamed share — a mix shift has to leave room "
            "for the rest of the table (EVALUATION.md §10)"
        )

    common = remainder / unnamed_total
    scaled = [
        (name, share * factors[name] if name in factors else share * common)
        for name, share in table
    ]
    running = sum(share for _, share in scaled[:-1])
    return tuple(scaled[:-1]) + ((scaled[-1][0], 1.0 - running),)


@dataclass(frozen=True, slots=True)
class MixShift:
    """One of §10's three registered composite mix configurations."""

    name: str
    rationale: str
    reason_factors: tuple[tuple[str, float], ...] = ()
    source_factors: tuple[tuple[str, float], ...] = ()

    @property
    def kind(self) -> SweepKind:
        return SweepKind.MIX

    @property
    def target(self) -> str:
        return self.name

    @property
    def factor(self) -> float | None:
        """A composite has no single factor. None rather than 1.0, so a report
        that groups by factor cannot file it under "unswept"."""
        return None

    def spec(self) -> WorldSpec:
        return WorldSpec(
            outcome_parameters=OUTCOME_PARAMETERS,
            world_parameters=WORLD_PARAMETERS,
            reason_mix=(
                rescale(REASON_MIX, dict(self.reason_factors))
                if self.reason_factors
                else REASON_MIX
            ),
            source_mix=(
                rescale(PAYMENT_FAILED_SOURCE_MIX, dict(self.source_factors))
                if self.source_factors
                else PAYMENT_FAILED_SOURCE_MIX
            ),
        )


MIX_SHIFTS: tuple[MixShift, ...] = (
    MixShift(
        name="mix:recoverable_heavy",
        rationale=(
            "§3d's own stated risk, in the direction it names: 'weight "
            "LIQUIDITY up and every arm improves'. The recoverable end up 50%, "
            "the unrecoverable end down 50%, remainder renormalised."
        ),
        reason_factors=((LIQUIDITY_REASON, 1.5),)
        + tuple((reason, 0.5) for reason in DEAD_INSTRUMENT_REASONS),
    ),
    MixShift(
        name="mix:recoverable_light",
        rationale=(
            "The mirror. A world where less of the failure mix is recoverable "
            "at all — which is also where `card_expired` is most common and "
            "F2's flagship claim has the most to work with."
        ),
        reason_factors=((LIQUIDITY_REASON, 0.5),)
        + tuple((reason, 1.5) for reason in DEAD_INSTRUMENT_REASONS),
    ),
    MixShift(
        name="mix:generic_skews_dead",
        rationale=(
            "§3d's 70/25/5 source split is what makes one registered reason — "
            "0.30 of all episodes, the single largest — resolve to three "
            "different failure classes. Tilting it toward `bank` moves the "
            "generic case from TRANSIENT toward INSTRUMENT_DEAD without "
            "changing how often the reason occurs."
        ),
        source_factors=(("bank", 1.5),),
    ),
)
"""§10, registered 2026-08-23. Three, not thirteen — see the module docstring."""


@dataclass(frozen=True, slots=True)
class ScalarSweep:
    """One registered parameter, at one of §7's four points."""

    name: str
    kind: SweepKind
    target: str
    factor: float

    def spec(self) -> WorldSpec:
        if self.kind is SweepKind.OUTCOME:
            return WorldSpec(
                outcome_parameters=swept(OUTCOME_PARAMETERS, self.target, self.factor),
                world_parameters=WORLD_PARAMETERS,
                reason_mix=REASON_MIX,
                source_mix=PAYMENT_FAILED_SOURCE_MIX,
            )
        return WorldSpec(
            outcome_parameters=OUTCOME_PARAMETERS,
            world_parameters=swept(WORLD_PARAMETERS, self.target, self.factor),
            reason_mix=REASON_MIX,
            source_mix=PAYMENT_FAILED_SOURCE_MIX,
        )


@dataclass(frozen=True, slots=True)
class Reference:
    """The unswept configuration, run on the sweep's own seeds.

    §10's mitigation: without it, "flips" would partly mean "had less power",
    because the sweep runs on 200 seeds and the headline comparison on 1000.
    """

    name: str = "reference"
    rationale: str = (
        "Every parameter at its registered value. Run on the sweep's seed "
        "range so survival is judged against an equally-powered comparison "
        "(EVALUATION.md §10, 2026-08-23)."
    )

    @property
    def kind(self) -> SweepKind:
        return SweepKind.REFERENCE

    @property
    def target(self) -> str:
        return "none"

    @property
    def factor(self) -> float | None:
        return None

    def spec(self) -> WorldSpec:
        return WorldSpec(
            outcome_parameters=OUTCOME_PARAMETERS,
            world_parameters=WORLD_PARAMETERS,
            reason_mix=REASON_MIX,
            source_mix=PAYMENT_FAILED_SOURCE_MIX,
        )


REFERENCE = Reference()

SweepConfig = ScalarSweep | MixShift | Reference


def sweep_configurations() -> tuple[SweepConfig, ...]:
    """§7's grid: the reference, every registered scalar at four points, and
    §10's three mix composites.

    Order is the reference first, then outcome parameters, then world
    parameters, then the mix — deterministic, so a resumed run picks up where
    it left off rather than reshuffling the work.
    """
    configs: list[SweepConfig] = [REFERENCE]
    for kind, registry in (
        (SweepKind.OUTCOME, OUTCOME_PARAMETERS),
        (SweepKind.WORLD, WORLD_PARAMETERS),
    ):
        for name in registry:
            if name in UNSWEPT:
                continue
            for factor in FACTORS:
                configs.append(
                    ScalarSweep(
                        name=f"{name}@{factor:g}", kind=kind, target=name, factor=factor
                    )
                )
    configs.extend(MIX_SHIFTS)
    return tuple(configs)


def parameters_touched(run: RunResult) -> frozenset[str]:
    """Which registered parameters actually decided something in this run.

    A union over `Ruling.depends_on`, which `windtunnel/outcome.py` fills at
    the moment each decision is made — so this is a record of what happened
    rather than a re-derivation of which rule would have applied. §7 needs it
    to say which conclusions a given parameter could have touched: a parameter
    no ruling in either arm consulted cannot have moved that comparison, and
    saying so from the run's own record is stronger than saying it from the
    code.

    Note what this does *not* cover: world-shape parameters shape the universe
    before any ruling is made, so they never appear here. Their attribution is
    structural — every arm sees the same swept universe — and the sweep result
    itself is the evidence.
    """
    return frozenset(name for ruling in run.rulings for name in ruling.depends_on)
