"""EVALUATION.md §6a's paired bootstrap and §6b's `pass^k`.

**Everything is a per-seed difference.** §6a: every arm runs on the same
seeded universe — same customers, same arrivals, same outcome draws — so the
arms are paired by seed, and comparing marginal intervals throws that
structure away. It calls that "a conservative test that would miss real
differences the pairing can detect", and rules it out. `marginal_interval`
exists here anyway, for one purpose: the report card states each arm's own
rate, and a test uses it to show the paired interval concluding where the
marginals would not.

**Superiority is claimed iff the interval excludes zero** — and in the right
direction. An interval wholly *below* zero also excludes it, and reporting
that as superiority would be a sign error in the headline number.

**On this module's own randomness.** The bootstrap resamples seeds, which
needs a generator, and it deliberately does not use windtunnel/rng.py.
That module's addressing discipline exists so every arm sees identical coins
in a shared world; there is no analogue here, because a bootstrap replicate is
not something two arms have to agree about. What is required is
reproducibility, which `BOOTSTRAP_SEED` and numpy's explicit `default_rng`
give: the same inputs produce the same interval, asserted by a test. Nothing
in this module touches the ledger, so architectural invariant 5 is not in play.

**`pass^k` is computed in closed form, over seeds.** With *m* of *n* seeds
satisfying §2a's predicate, the fraction of size-*k* seed subsets in which
every run passes is exactly `C(m, k) / C(n, k)`. Sampling subsets instead
would put Monte Carlo noise into the number F4 turns on, for no benefit.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

BOOTSTRAP_RESAMPLES = 10_000
"""Replicates per interval. Large enough that the percentile endpoints are
stable to well under a tenth of a percentage point, which is finer than any
difference the report card quotes."""

BOOTSTRAP_SEED = 20260823
"""Fixed so two runs of the evaluator report the same interval. Not a
registered parameter — it changes no number's expectation, only which
replicates are drawn."""

CONFIDENCE = 0.95
"""§6a: "report the 95% percentile interval of the difference"."""

PASS_K_VALUES: tuple[int, ...] = (1, 5, 10, 25, 50, 100)
"""§6b: "Reported for k ∈ {1, 5, 10, 25, 50, 100}"."""


@dataclass(frozen=True, slots=True)
class Interval:
    """A point estimate and its percentile interval."""

    point: float
    low: float
    high: float
    level: float = CONFIDENCE

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0.0 or self.high < 0.0


@dataclass(frozen=True, slots=True)
class PairedComparison:
    """§6a's per-seed difference between two arms on one metric."""

    metric: str
    n_seeds: int
    differences: tuple[float, ...]
    interval: Interval

    @property
    def superior(self) -> bool:
        """§6a: superiority iff the interval excludes zero — and, since an
        interval below zero excludes it too, iff the difference is positive."""
        return self.interval.excludes_zero and self.interval.point > 0.0


def _bootstrap(values: Sequence[float]) -> Interval:
    """Percentile interval for the mean, by resampling `values` with
    replacement. `values` is already the per-seed difference vector, so
    resampling it is resampling seeds — §6a's unit."""
    sample = np.asarray(values, dtype=float)
    n = sample.size
    if n == 0:
        raise ValueError("cannot bootstrap an empty sample")

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    draws = rng.integers(0, n, size=(BOOTSTRAP_RESAMPLES, n))
    means = sample[draws].mean(axis=1)

    tail = (1.0 - CONFIDENCE) / 2.0
    low, high = np.percentile(means, [100 * tail, 100 * (1.0 - tail)])
    return Interval(point=float(sample.mean()), low=float(low), high=float(high))


def paired_difference(
    treatment: Mapping[int, float], baseline: Mapping[int, float], *, metric: str
) -> PairedComparison:
    """§6a: `d_s = metric(treatment, s) − metric(baseline, s)`, bootstrapped.

    Both sides must cover exactly the same seeds. Intersecting them quietly
    would drop worlds from the comparison and report an `n` nobody chose —
    and the arms not having run on the same seeds is a bug upstream, not
    something to paper over here.
    """
    if set(treatment) != set(baseline):
        raise ValueError(
            "paired inference needs the same seeds on both sides: "
            f"{len(set(treatment) ^ set(baseline))} seed(s) appear on only one"
        )

    seeds = sorted(treatment)
    differences = tuple(treatment[s] - baseline[s] for s in seeds)
    return PairedComparison(
        metric=metric,
        n_seeds=len(seeds),
        differences=differences,
        interval=_bootstrap(differences),
    )


def marginal_interval(values: Mapping[int, float] | Sequence[float]) -> Interval:
    """One arm's own interval. Reported per arm on the report card; never used
    to compare two arms — see the module docstring."""
    sample = list(values.values()) if isinstance(values, Mapping) else list(values)
    return _bootstrap(sample)


def pass_k(outcomes: Sequence[bool], ks: Sequence[int] = PASS_K_VALUES) -> dict[int, float]:
    """§6b: the fraction of size-k *seed* subsets in which every run passes.

    `outcomes` is one boolean per seed — did §2a's predicate hold in that
    world. It must not be one boolean per repeat of a single seed: invariant 5
    makes repeats byte-identical, so that vector is constant and `pass^k` is
    1.0 by construction, measuring the determinism guarantee rather than the
    agent. §6b says determinism is checked separately, and it is.
    """
    n = len(outcomes)
    m = sum(1 for passed in outcomes if passed)
    results: dict[int, float] = {}
    for k in ks:
        if k > n:
            raise ValueError(
                f"pass^{k} needs at least {k} seeds and there are {n} — "
                "reporting 0.0 would read as a failure rather than as a "
                "measurement that was never taken"
            )
        results[k] = math.comb(m, k) / math.comb(n, k) if k <= m else 0.0
    return results


def survives(reference: PairedComparison, swept: PairedComparison) -> bool:
    """§7: does a conclusion hold under a swept parameter?

    A conclusion survives when the swept interval still excludes zero *in the
    same direction* as the unswept one. Three ways to fail, all of them
    §7-reportable: the swept interval crosses zero, it reverses sign, or there
    was no conclusion to begin with — the last because calling a null result
    robust would invent a finding out of nothing.

    The reference must be computed on the same seeds as the swept run. §10
    registers exactly that: §7 runs on seeds 0..199, and survival is judged
    against an unswept reference recomputed on the same 200 seeds, so a
    conclusion cannot "flip" merely because the sweep had less power than the
    headline comparison.
    """
    if not reference.interval.excludes_zero:
        return False
    if not swept.interval.excludes_zero:
        return False
    return (reference.interval.point > 0) == (swept.interval.point > 0)
