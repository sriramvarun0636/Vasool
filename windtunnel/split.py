"""EVALUATION.md §3c: the 40/60 customer-level split, and the seal on the 60.

**What the split buys, precisely.** §3c is explicit that this is not a
train/test split in the machine-learning sense — nothing here is fitted, the
classifier is a dictionary. What it protects against is narrower and still
real: tuning thresholds until the numbers improve and then reporting them as
though the thresholds had been chosen in advance. taxonomy.md contains at
least four such knobs, each currently justified by argument rather than data.

**Why the seal is a phrase and not a boolean.** §3c says the holdout is
evaluated once, and the failure that rule exists to prevent is not a
deliberate peek — it is an absent-minded one, in a debugging session, three
weeks from now. A `bool` flag is as easy to pass by reflex as deliberately,
and defaults to whatever the call site happens to have lying around. Typing
`UNSEAL_PHRASE` means typing the clause being invoked, and it cannot be
arrived at by an argument being threaded through from somewhere else.

The seal is not a security control. Anyone reading this module can defeat it
in a second, and that is fine — it is aimed at the author's own inattention,
which is the actual threat, and CLAUDE.md's git discipline means the peek
would be in a diff either way.

**Randomisation unit: the customer** (§3a), because episodes from one customer
share a payment instrument, a consent record, a DND status, a contact history
and a frequency-cap budget, and `FrequencyCapGuard` and `PromiseToPayGuard`
explicitly couple them. **Stratified on the first episode's failure class**
(§3b), because the five classes differ enormously in recoverability and an
unstratified split can hand one side a materially easier population by chance.

Assignment is coordinate-addressed like everything else here
(windtunnel/rng.py): a customer's side is a function of the seed and their id,
never of how many customers were assigned before them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from vasool.diagnosis.taxonomy import FailureClass
from windtunnel.rng import draw
from windtunnel.universe import Universe

DEVELOPMENT_SHARE = 0.40
"""§3c: "Development set: 40% of customers ... Holdout: 60%, sealed"."""

UNSEAL_PHRASE = "EVALUATION.md §3c: the holdout is evaluated once"
"""The literal string `Split.holdout` demands. See the module docstring."""

SPLIT_STREAM = "split"
"""Draw stream name, distinct from every stream windtunnel/outcome.py and
windtunnel/universe.py use, so that changing the split can never move a
settlement coin or an arrival time."""


class HoldoutSealed(RuntimeError):
    """Raised on an attempt to read the holdout without the unseal phrase."""


class Cohort(StrEnum):
    """Which side of §3c a number came from. Closed, and carrying its own
    output directory so a holdout figure cannot land in a development report
    by sharing a path."""

    DEVELOPMENT = "development"
    HOLDOUT = "holdout"

    @property
    def directory(self) -> str:
        """Where this cohort's results are written, relative to out/."""
        return self.value


@dataclass(frozen=True, slots=True)
class StratumBalance:
    """One row of the balance table §3b's stratification exists to make
    publishable. A small table showing the two sides matched on the baseline
    covariate is the evidence that the split did what it claims."""

    stratum: FailureClass | None
    """None for a customer whose episodes all fell past the arrival window —
    they have no first episode, so they have no key (`Universe.stratum_of`)."""

    development: int
    holdout: int


@dataclass(frozen=True, slots=True)
class Split:
    """One universe's customers, divided. The holdout is behind a method."""

    seed: int
    development: frozenset[str]
    strata: tuple[StratumBalance, ...]

    _holdout: frozenset[str] = field(repr=False)
    """Named with a leading underscore and excluded from `repr` so it does not
    appear in a traceback, a log line or a debugger's default view. `holdout`
    is the only route to it."""

    _stratum_by_customer: dict[str, FailureClass | None] = field(repr=False)

    @property
    def development_size(self) -> int:
        return len(self.development)

    @property
    def holdout_size(self) -> int:
        """The *count* is not sealed. Knowing 300 customers are held back
        tells you nothing about their outcomes, and the report card has to
        state the split it used."""
        return len(self._holdout)

    def holdout(self, *, unseal: str | None = None) -> frozenset[str]:
        """The sealed 60%.

        Requires `UNSEAL_PHRASE`. §3c: the holdout is evaluated once, and if a
        bug is found afterwards that invalidates it, the fix and the re-run are
        both recorded in §10 with the reason. A silent re-run is the failure
        mode that rule exists to prevent, and a silent *first* run is worse.
        """
        if unseal != UNSEAL_PHRASE:
            raise HoldoutSealed(
                "the holdout is sealed. EVALUATION.md §3c evaluates it once, and "
                "records the run in §10 — pass unseal=UNSEAL_PHRASE only when "
                "that is what is happening, and write the §10 row first. "
                f"(seed {self.seed}, {len(self._holdout)} customers)"
            )
        return self._holdout

    def side_of(self, customer_id: str) -> Cohort:
        """Which cohort a customer is in. Safe to call: naming a side is not
        reading the holdout's results."""
        return Cohort.DEVELOPMENT if customer_id in self.development else Cohort.HOLDOUT

    def stratum_of(self, customer_id: str) -> FailureClass | None:
        return self._stratum_by_customer[customer_id]


def _strata(universe: Universe) -> dict[str, FailureClass | None]:
    """§3b's key for every customer, in one pass.

    `Universe.stratum_of` is the canonical definition and scans the whole
    episode list per customer; this is the same answer computed once.
    tests/windtunnel/test_split.py asserts the two agree, so the fast path
    cannot drift from the definition.
    """
    first: dict[int, FailureClass] = {}
    for episode in universe.episodes:
        first.setdefault(episode.customer.index, episode.failure_class)
    return {c.customer_id: first.get(c.index) for c in universe.customers}


def split_customers(universe: Universe) -> Split:
    """§3c, applied to one universe.

    Within each stratum, customers are ordered by an addressed draw — a seeded
    permutation — and the first 40% become the development set. Rounding is
    per stratum rather than globally, so a small stratum is still split rather
    than landing wholly on one side.
    """
    stratum_by_customer = _strata(universe)

    grouped: dict[FailureClass | None, list[str]] = {}
    for customer in universe.customers:
        grouped.setdefault(stratum_by_customer[customer.customer_id], []).append(
            customer.customer_id
        )

    development: set[str] = set()
    balances: list[StratumBalance] = []
    for stratum in sorted(grouped, key=lambda s: "" if s is None else s.value):
        members = sorted(
            grouped[stratum], key=lambda cid: (draw(universe.seed, SPLIT_STREAM, cid), cid)
        )
        take = round(len(members) * DEVELOPMENT_SHARE)
        development.update(members[:take])
        balances.append(
            StratumBalance(stratum=stratum, development=take, holdout=len(members) - take)
        )

    everyone = {c.customer_id for c in universe.customers}
    return Split(
        seed=universe.seed,
        development=frozenset(development),
        strata=tuple(balances),
        _holdout=frozenset(everyone - development),
        _stratum_by_customer=stratum_by_customer,
    )
