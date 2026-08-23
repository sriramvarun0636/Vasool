"""Coordinate-addressed randomness. No generator state anywhere.

Every variate in this simulator is a pure function of its coordinates —
(seed, episode, what-is-being-decided, index) — rather than of how many draws
happened to be taken before it. Nothing here holds state, nothing is seeded
once and consumed in order, and there is no `random.Random` instance to pass
around.

**Why, precisely.** EVALUATION.md §5 runs all four arms on "the identical
seeded universe with identical arrivals and identical outcome draws", and §6a
computes every comparison as a per-seed difference between arms. With a
stateful generator that pairing is fiction: `naive_retry` retries failures
Vasool never touches, so it takes more draws, and from its first extra draw
every subsequent variate in that arm is a different number than the one Vasool
saw. The arms would share a universe and disagree about the coin flips inside
it, and `d_s = metric(Vasool, s) − metric(baseline, s)` would be measuring that
disagreement along with the effect.

Addressed draws make the pairing structural instead. "Does episode E's second
attempt succeed?" has exactly one answer per seed; every arm that asks gets it,
and an arm that never asks changes nothing for the arms that do.

Two consequences worth stating because they are load-bearing elsewhere:

  - **`bernoulli` is monotone in p.** It compares a fixed `u` against `p`, so
    raising `p` can only turn a False into a True. §7 sweeps every registered
    parameter ±50%, and this is what makes a sweep isolate the parameter rather
    than re-rolling the world underneath it.
  - **The basis string is separator-delimited.** Without it, ("ab", "c") and
    ("a", "bc") would be the same address — the ordinary way an addressing
    scheme silently correlates two things meant to be independent.

sha256 rather than `hash()`, for the same reason
vasool/policy/guards/contact_window.py::window_jitter uses it: `hash()` is
salted per process, so a ledger would replay differently tomorrow.
"""
from __future__ import annotations

import hashlib
import math
from statistics import NormalDist
from typing import Sequence, TypeVar

T = TypeVar("T")

_TWO_64 = float(1 << 64)

_UNIT_EPSILON = 1e-12
"""How far a variate is held away from the open ends of (0, 1).

`draw` returns a value in [0, 1), and both `NormalDist.inv_cdf(0)` and
`log(0)` are undefined, so anything transforming a draw through an inverse
CDF has to exclude the endpoint. Clamping rather than rejecting keeps the
function total: a rejection loop would make the number of draws consumed
depend on their values, which is exactly the call-order dependence this
module exists to remove.
"""


def draw(seed: int, *coordinates: object) -> float:
    """A uniform variate on [0, 1), addressed by its coordinates.

    `seed` is spelled as its own argument rather than folded into
    `coordinates` only because every caller has one and forgetting it would
    silently produce a universe that ignores the seed — a failure that would
    show up as "every seed gives the same answer", far downstream.
    """
    basis = "|".join([str(seed), *(str(c) for c in coordinates)])
    digest = hashlib.sha256(basis.encode()).digest()
    return int.from_bytes(digest[:8], "big") / _TWO_64


def _unit(seed: int, coordinates: tuple[object, ...]) -> float:
    """A draw held strictly inside (0, 1) — see _UNIT_EPSILON."""
    return min(max(draw(seed, *coordinates), _UNIT_EPSILON), 1.0 - _UNIT_EPSILON)


def bernoulli(probability: float, seed: int, *coordinates: object) -> bool:
    """Monotone in `probability` — see this module's docstring on why §7's
    sweep depends on that."""
    return draw(seed, *coordinates) < probability


def uniform(low: float, high: float, seed: int, *coordinates: object) -> float:
    return low + (high - low) * draw(seed, *coordinates)


def integer(low: int, high: int, seed: int, *coordinates: object) -> int:
    """An integer in [low, high], both ends included."""
    return low + int(draw(seed, *coordinates) * (high - low + 1))


def exponential(mean: float, seed: int, *coordinates: object) -> float:
    return -mean * math.log(_unit(seed, coordinates))


def lognormal(median: float, sigma: float, seed: int, *coordinates: object) -> float:
    """A log-normal parameterised by its median rather than by the mean of its
    log, because the median is the number a reader can sanity-check against
    an intuition about invoice sizes."""
    z = NormalDist().inv_cdf(_unit(seed, coordinates))
    return median * math.exp(sigma * z)


def poisson(lam: float, seed: int, *coordinates: object) -> int:
    """Inverse-CDF Poisson.

    Fine for the small lambdas this simulator uses (episodes per customer);
    it walks the CDF term by term, so it would be the wrong algorithm for a
    large lambda. It is used instead of a rejection method for the reason
    given in _UNIT_EPSILON: the number of draws consumed must not depend on
    the values drawn.
    """
    u = draw(seed, *coordinates)
    cumulative = term = math.exp(-lam)
    k = 0
    while u >= cumulative and k < 1000:
        k += 1
        term *= lam / k
        cumulative += term
    return k


def choose(options: Sequence[tuple[T, float]], seed: int, *coordinates: object) -> T:
    """Pick one option at its registered share.

    Shares are walked in the order given and are not renormalised: the
    distributions this picks from are pre-registered in EVALUATION.md §3d and
    must sum to 1.0 exactly. A table that does not is a registration error to
    surface, not a rounding artifact to absorb quietly — so a draw past the
    end of the table raises rather than falling back on the last option.
    """
    u = draw(seed, *coordinates)
    cumulative = 0.0
    for option, share in options:
        cumulative += share
        if u < cumulative:
            return option
    raise ValueError(
        f"draw {u} fell past the end of a distribution summing to {cumulative} — "
        "shares must sum to 1.0 (EVALUATION.md §3d)"
    )
