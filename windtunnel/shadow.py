"""The shadow comparison: rules vs LLM, measured against the registered truth.

docs/VASOOL-design-spec.md §4.5 asks for a table, and says the right thing
about it: *ship the hybrid and publish the table showing where the LLM lost.*
docs/taxonomy.md §8 puts it more bluntly — **the measurement is the
deliverable, not the LLM.** So this module is the measurement, and the
classifier it measures lives in vasool/diagnosis/llm.py behind a prompt and a
parser.

**Nothing here reaches a provider.** `compare` takes a callable from (prompt,
repeat) to response text. The real run passes a cassette lookup; the tests
pass a function. windtunnel/ may not reach the network at all
(tests/windtunnel/test_runner.py::TestNoNetwork), which is why the dependency
is injected rather than imported — and it is also why replay needs no
cooperation from this module: a cassette store *is* a legal `respond`.

**The corpus is the whole input space, not a sample of it.** The registered
arm is fields-only, so a prompt is a pure function of four error strings, and
the registered universe produces exactly twelve distinct combinations of them
across roughly 8,900 episodes in ten seeds. Sampling three hundred episodes
would have recorded twelve answers and replicated them three hundred times.
Enumerating the twelve is both cheaper and a stronger claim than §4.5's
"n=200 hand-labelled": every input the universe can generate is covered, and
`tests/windtunnel/test_shadow.py` fails the day a thirteenth appears.

**Ground truth is `PlannedEpisode.failure_class`, and that is degenerate on
purpose.** That property resolves through the same taxonomy lookup the rules
classifier uses, on the same two fields, so the Rules column is 1.000 by
construction and cannot be anything else. It is still computed rather than
written down, so that the day the world grows a cause independent of the
table the number moves on its own. What the artifact must never do is let a
reader mistake a definition for a finding, which is why HEADER_CAVEATS is
rendered above the table rather than filed in a document.

**Agreement is a secondary column and the code proves why.** When the rules
are the truth, agreement between the classifiers is numerically identical to
the LLM's accuracy — so a table reporting only agreement could not show where
the LLM loses, which is the only thing worth reporting.
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from vasool.clock import VirtualClock
from vasool.diagnosis.llm import LLMVerdict, VerdictRejected, build_prompt, parse_verdict
from vasool.diagnosis.rules import classify
from vasool.diagnosis.taxonomy import FailureClass, InterventionType, lookup
from windtunnel.outcome import OutcomeModel
from windtunnel.parameters import OUTCOME_PARAMETERS
from windtunnel.payloads import available_pairs
from windtunnel.split import split_customers
from windtunnel.universe import EPOCH, build_universe

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED_DIR = REPO_ROOT / "data" / "observed_payloads"
STUBBED_DIR = REPO_ROOT / "data" / "stubbed_payloads"

CORPUS_SEEDS = range(0, 10)
"""Seeds the cell weights are counted over.

Ten rather than one because the two rare cells — the risk-block pair — carry
single-digit episode counts per seed, and a weight that small is noise. Ten is
enough to stabilise them and cheap enough to run in under a second, since
nothing here calls a provider: the seeds decide the *weights*, never the cell
list, which is identical on every seed.
"""

REPEATS = 15
"""How many times each cell is asked.

Sized against the free tier this comparison runs on. Twelve cells at fifteen
repeats is 180 requests, which fits inside the tightest daily quota reported
for that tier with headroom for retries, and takes about eighteen minutes at
ten requests per minute. Raising it is incremental — windtunnel/cassette.py
keys on the repeat index, so k=40 records only the twenty-five new repeats.

Repeats exist because a provider has no determinism setting. Rather than
pretend otherwise, the run asks the same question k times and reports how
often the answer changed, which is the `consistency` column.
"""

HEADER_CAVEATS: tuple[str, ...] = (
    "The LLM arm runs on a model chosen for cost — a free tier is the budget "
    "this comparison had. A stronger model might close the gap, and nothing "
    "measured here bounds one that was not run.",
    "Nine of the ten error reasons are _SIMULATED stubs, documentation-derived "
    "rather than captured; only payment_failed is reproducible against live "
    "test mode (docs/VERIFIED.md). EVALUATION.md §11 registers that this "
    "comparison does not generalise beyond these ten.",
    "The Rules column is 1.000 by construction, not by measurement: the "
    "registered truth is PlannedEpisode.failure_class, which resolves through "
    "the same taxonomy lookup the rules classifier reads. Read it as the "
    "definition of truth in this world, never as evidence the table is right.",
)
"""The three limits that have to travel with the numbers.

Rendered into the artifact rather than filed in a document, because a reader
meets the artifact and may never meet the document. Each is asserted
separately in tests/windtunnel/test_shadow.py, so deleting one is a failing
test rather than a quiet regression.
"""

Respond = Callable[[str, int], str]
"""(prompt, repeat index) -> raw response text. The only thing this module
knows about how an answer is obtained."""


@dataclass(frozen=True, slots=True)
class Cell:
    """One distinct classification question the registered universe can ask.

    A cell, not an episode, is the unit of measurement: with a fields-only
    prompt every episode sharing these four strings asks the identical
    question, so an episode-level table would report twelve answers with an n
    of nine thousand.
    """

    reason: str
    source: str
    error_code: str
    error_step: str
    episodes: int
    """How many development-cohort episodes over CORPUS_SEEDS land in this
    cell. The weight, never the sample size."""

    truth: FailureClass
    """PlannedEpisode.failure_class — the registered truth. See the module
    docstring on why the rules cannot lose against it."""

    rules: FailureClass
    rules_intervention: InterventionType | None
    provenance: str
    """`observed`, `simulated`, or `assembled` — where these error strings came
    from. Travels to the artifact so §11's limit is visible beside the row it
    limits."""

    prompt: str

    @property
    def label(self) -> str:
        return f"{self.reason}__{self.source}"


def _provenance(reason: str, source: str) -> str:
    """Where this cell's error strings came from.

    Three answers, and the third matters. `observed` and `simulated` are the
    two directories CLAUDE.md names. `assembled` is the pair whose two halves
    each exist on disk but whose combination never has —
    windtunnel/payloads.py builds it by reading each string from a different
    envelope, and docs/taxonomy.md §9.7 argues that row from cost asymmetry
    rather than from evidence. Reporting it as observed would be the circular
    claim §5 warns about: our own stub read back to us.
    """
    if (reason, source) not in available_pairs():
        return "assembled"
    for directory in (OBSERVED_DIR, STUBBED_DIR):
        for path in sorted(directory.glob("*.json")):
            fixture = json.loads(path.read_text())
            entity = fixture["body"]["payload"]["payment"]["entity"]
            if (entity["error_reason"], entity["error_source"]) == (reason, source):
                return "simulated" if fixture.get("_SIMULATED") else "observed"
    raise LookupError(f"no payload on disk for {(reason, source)!r}")


def build_corpus(*, pepper: str, seeds: Iterable[int] = CORPUS_SEEDS) -> tuple[Cell, ...]:
    """Enumerate every distinct classification question, with its weight.

    Weights come from the **development cohort only** (EVALUATION.md §3c).
    Nothing here is fitted, so the seal is not strictly at risk — but a weight
    counted over sealed customers is still a number read off the holdout, and
    the protocol is cheaper to honour than to argue about. The cell *list* is
    identical either way.

    Pure and deterministic: no clock beyond the pinned VirtualClock the rules
    classifier needs, no environment, no network.
    """
    clock = VirtualClock(EPOCH)
    counts: Counter[tuple[str, str, str, str]] = Counter()
    events: dict[tuple[str, str, str, str], object] = {}

    for seed in seeds:
        outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=seed)
        universe = build_universe(seed, pepper=pepper, outcome=outcome)
        development = split_customers(universe).development
        for episode in universe.episodes:
            if episode.customer.customer_id not in development:
                continue
            event = episode.event
            key = (
                event.error_reason,
                event.error_source,
                event.error_code,
                event.error_step,
            )
            counts[key] += 1
            events.setdefault(key, event)

    cells = []
    for key, episodes in counts.items():
        reason, source, code, step = key
        event = events[key]
        diagnosis = classify(event, clock=clock, attempt=1)
        cells.append(
            Cell(
                reason=reason,
                source=source,
                error_code=code,
                error_step=step,
                episodes=episodes,
                truth=lookup(reason, source)[1].failure_class,
                rules=diagnosis.failure_class,
                rules_intervention=diagnosis.intervention,
                provenance=_provenance(reason, source),
                prompt=build_prompt(event),
            )
        )
    # Heaviest first, so a reader meets the cells that decide the weighted
    # number before the ones that barely move it. Label breaks ties, because
    # a corpus that reorders between runs is a corpus that cannot be diffed.
    return tuple(sorted(cells, key=lambda c: (-c.episodes, c.label)))


@dataclass(frozen=True, slots=True)
class CellResult:
    """What the LLM said about one cell, k times."""

    cell: Cell
    verdicts: tuple[LLMVerdict | None, ...]
    """One entry per repeat. `None` is a response the parser refused — an
    invented class, a malformed document, prose. Counted separately from a
    wrong answer, because "said something impossible" and "said the wrong
    class" are different failures with different remedies."""

    @property
    def repeats(self) -> int:
        return len(self.verdicts)

    @property
    def correct(self) -> int:
        return sum(
            1 for v in self.verdicts if v is not None and v.failure_class is self.cell.truth
        )

    @property
    def rejected(self) -> int:
        return sum(1 for v in self.verdicts if v is None)

    @property
    def accuracy(self) -> float:
        return self.correct / self.repeats if self.repeats else 0.0

    @property
    def agreement(self) -> int:
        """Repeats where the LLM matched the *rules*, not the truth."""
        return sum(
            1 for v in self.verdicts if v is not None and v.failure_class is self.cell.rules
        )

    @property
    def consistency(self) -> float:
        """How often the modal answer was given. 1.0 means it never changed
        its mind; a rejection counts as its own answer, because a model that
        reliably emits nonsense is consistent about it."""
        if not self.repeats:
            return 0.0
        answers = Counter(v.failure_class if v else None for v in self.verdicts)
        return max(answers.values()) / self.repeats

    @property
    def intervention_matches(self) -> int:
        return sum(
            1
            for v in self.verdicts
            if v is not None and v.intervention is self.cell.rules_intervention
        )

    @property
    def unsafe_risk_block_actions(self) -> int:
        """Repeats proposing an automated action on a risk-declined payment.

        docs/taxonomy.md §2 and §4.4: this is the one class where the correct
        product decision is to do nothing, and getting the *class* right is
        not enough — the action has to be HUMAN_QUEUE. Counted apart from
        accuracy because it is a safety number, not a quality one.
        """
        if self.cell.truth is not FailureClass.RISK_BLOCK:
            return 0
        return sum(
            1
            for v in self.verdicts
            if v is not None and v.intervention is not InterventionType.HUMAN_QUEUE
        )


@dataclass(frozen=True, slots=True)
class ClassRow:
    """One row of §4.5's table."""

    failure_class: FailureClass
    cells: int
    episodes: int
    rules_accuracy: float
    llm_accuracy: float
    llm_accuracy_weighted: float
    consistency: float
    rejected: int


def _unweighted(results: Sequence[CellResult], value) -> float:
    return sum(value(r) for r in results) / len(results) if results else 0.0


def _weighted(results: Sequence[CellResult], value) -> float:
    total = sum(r.cell.episodes for r in results)
    if not total:
        return 0.0
    return sum(value(r) * r.cell.episodes for r in results) / total


@dataclass(frozen=True, slots=True)
class Comparison:
    """The finished measurement. Everything the artifact renders comes from
    here, so nothing in the table can be a number the object does not hold."""

    provider: str
    model: str
    repeats: int
    cells: tuple[CellResult, ...]
    rows: tuple[ClassRow, ...]

    @property
    def total_episodes(self) -> int:
        return sum(r.cell.episodes for r in self.cells)

    @property
    def rules_accuracy(self) -> float:
        return _unweighted(self.cells, lambda r: float(r.cell.rules is r.cell.truth))

    @property
    def rules_accuracy_weighted(self) -> float:
        return _weighted(self.cells, lambda r: float(r.cell.rules is r.cell.truth))

    @property
    def llm_accuracy(self) -> float:
        return _unweighted(self.cells, lambda r: r.accuracy)

    @property
    def llm_accuracy_weighted(self) -> float:
        return _weighted(self.cells, lambda r: r.accuracy)

    @property
    def consistency(self) -> float:
        return _unweighted(self.cells, lambda r: r.consistency)

    @property
    def agreement(self) -> float:
        """Identical to `llm_accuracy` while the rules define the truth. Kept
        as its own number so the artifact can show the identity rather than
        assert it — see the module docstring."""
        return _unweighted(self.cells, lambda r: r.agreement / r.repeats)

    @property
    def intervention_agreement(self) -> float:
        return _unweighted(self.cells, lambda r: r.intervention_matches / r.repeats)

    @property
    def rejected(self) -> int:
        return sum(r.rejected for r in self.cells)

    @property
    def unsafe_risk_block_actions(self) -> int:
        return sum(r.unsafe_risk_block_actions for r in self.cells)


def _rows(results: Sequence[CellResult]) -> tuple[ClassRow, ...]:
    rows = []
    for failure_class in FailureClass:
        group = [r for r in results if r.cell.truth is failure_class]
        if not group:
            continue
        rows.append(
            ClassRow(
                failure_class=failure_class,
                cells=len(group),
                episodes=sum(r.cell.episodes for r in group),
                rules_accuracy=_unweighted(
                    group, lambda r: float(r.cell.rules is r.cell.truth)
                ),
                llm_accuracy=_unweighted(group, lambda r: r.accuracy),
                llm_accuracy_weighted=_weighted(group, lambda r: r.accuracy),
                consistency=_unweighted(group, lambda r: r.consistency),
                rejected=sum(r.rejected for r in group),
            )
        )
    return tuple(rows)


def compare(
    corpus: Sequence[Cell],
    respond: Respond,
    *,
    repeats: int = REPEATS,
    provider: str,
    model: str,
) -> Comparison:
    """Ask every cell `repeats` times and score both classifiers.

    `respond` is called once per (cell, repeat) and returns raw text. It may
    be a cassette lookup, in which case a missing recording raises rather than
    silently calling anything — see windtunnel/cassette.py.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")

    results = []
    for cell in corpus:
        verdicts: list[LLMVerdict | None] = []
        for repeat in range(repeats):
            try:
                verdicts.append(parse_verdict(respond(cell.prompt, repeat)))
            except VerdictRejected:
                verdicts.append(None)
        results.append(CellResult(cell=cell, verdicts=tuple(verdicts)))

    return Comparison(
        provider=provider,
        model=model,
        repeats=repeats,
        cells=tuple(results),
        rows=_rows(results),
    )


# ---------------------------------------------------------------------------
# the artifact
# ---------------------------------------------------------------------------
def _wrap(text: str, width: int = 76, indent: str = "  ") -> list[str]:
    lines, current = [], indent
    for word in text.split():
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = indent
        current += word + " "
    if current.strip():
        lines.append(current.rstrip())
    return lines


def render_table(comparison: Comparison) -> str:
    """§4.5's table, with its limits above it rather than beside it.

    Text rather than HTML because that is what data/golden/ holds and what a
    diff can review; windtunnel/shadow.py's JSON is what a later report
    builder will read.
    """
    out: list[str] = []
    out.append("CLASSIFIER COMPARISON — deterministic rules vs LLM, shadow mode")
    out.append(
        f"provider={comparison.provider}  model={comparison.model}  "
        f"repeats={comparison.repeats}  cells={len(comparison.cells)}  "
        f"episodes={comparison.total_episodes}"
    )
    out.append("")
    out.append("READ THIS FIRST")
    for caveat in HEADER_CAVEATS:
        out.extend(_wrap(caveat))
        out.append("")

    header = (
        f"{'':<24}{'Rules':>8}{'LLM':>8}{'LLM(wt)':>10}"
        f"{'consist':>9}{'cells':>7}{'episodes':>10}{'rejected':>10}"
    )
    out.append(header)
    out.append("-" * len(header))
    out.append(
        f"{'Overall':<24}{comparison.rules_accuracy:>8.3f}"
        f"{comparison.llm_accuracy:>8.3f}{comparison.llm_accuracy_weighted:>10.3f}"
        f"{comparison.consistency:>9.3f}{len(comparison.cells):>7}"
        f"{comparison.total_episodes:>10}{comparison.rejected:>10}"
    )
    for row in comparison.rows:
        out.append(
            f"{'  ' + row.failure_class.value:<24}{row.rules_accuracy:>8.3f}"
            f"{row.llm_accuracy:>8.3f}{row.llm_accuracy_weighted:>10.3f}"
            f"{row.consistency:>9.3f}{row.cells:>7}{row.episodes:>10}"
            f"{row.rejected:>10}"
        )

    out.append("")
    out.append("PER CELL — every distinct question the registered universe can ask")
    cell_header = (
        f"{'error_reason / source':<46}{'provenance':<12}"
        f"{'truth':<17}{'LLM':>7}{'consist':>9}{'episodes':>10}"
    )
    out.append(cell_header)
    out.append("-" * len(cell_header))
    for result in comparison.cells:
        cell = result.cell
        out.append(
            f"{cell.reason + ' / ' + cell.source:<46}{cell.provenance:<12}"
            f"{cell.truth.value:<17}{result.accuracy:>7.3f}"
            f"{result.consistency:>9.3f}{cell.episodes:>10}"
        )

    out.append("")
    out.append("SECONDARY")
    out.extend(
        _wrap(
            f"Classifier agreement {comparison.agreement:.3f} — identical to the "
            "LLM's accuracy above, and necessarily so while the rules define the "
            "truth. A table reporting only agreement could not show where the "
            "LLM loses, which is the only thing worth reporting."
        )
    )
    out.append("")
    out.extend(
        _wrap(
            f"Intervention agreement {comparison.intervention_agreement:.3f} — how "
            "often the LLM chose the action §4 names for the row, at attempt 1."
        )
    )
    out.append("")
    out.extend(
        _wrap(
            f"Automated actions proposed on RISK_BLOCK episodes: "
            f"{comparison.unsafe_risk_block_actions} of "
            f"{sum(r.repeats for r in comparison.cells if r.cell.truth is FailureClass.RISK_BLOCK)}. "
            "docs/taxonomy.md §4.4: this is the one class where the correct "
            "product decision is to do nothing, so the class being right is not "
            "enough — the action has to be HUMAN_QUEUE."
        )
    )
    out.append("")
    return "\n".join(out)


def to_document(comparison: Comparison) -> dict:
    """The machine-readable artifact. Same numbers, same caveats — a JSON
    consumer must not be able to read the table without reading its limits."""
    return {
        "caveats": list(HEADER_CAVEATS),
        "provider": comparison.provider,
        "model": comparison.model,
        "repeats": comparison.repeats,
        "cells": len(comparison.cells),
        "episodes": comparison.total_episodes,
        "overall": {
            "rules_accuracy": comparison.rules_accuracy,
            "rules_accuracy_weighted": comparison.rules_accuracy_weighted,
            "llm_accuracy": comparison.llm_accuracy,
            "llm_accuracy_weighted": comparison.llm_accuracy_weighted,
            "consistency": comparison.consistency,
            "agreement": comparison.agreement,
            "intervention_agreement": comparison.intervention_agreement,
            "rejected": comparison.rejected,
            "unsafe_risk_block_actions": comparison.unsafe_risk_block_actions,
        },
        "by_class": [
            {
                "failure_class": row.failure_class.value,
                "cells": row.cells,
                "episodes": row.episodes,
                "rules_accuracy": row.rules_accuracy,
                "llm_accuracy": row.llm_accuracy,
                "llm_accuracy_weighted": row.llm_accuracy_weighted,
                "consistency": row.consistency,
                "rejected": row.rejected,
            }
            for row in comparison.rows
        ],
        "by_cell": [
            {
                "error_reason": r.cell.reason,
                "error_source": r.cell.source,
                "error_code": r.cell.error_code,
                "error_step": r.cell.error_step,
                "provenance": r.cell.provenance,
                "episodes": r.cell.episodes,
                "truth": r.cell.truth.value,
                "rules": r.cell.rules.value,
                "rules_intervention": (
                    r.cell.rules_intervention.value if r.cell.rules_intervention else None
                ),
                "llm_accuracy": r.accuracy,
                "llm_consistency": r.consistency,
                "llm_rejected": r.rejected,
                "llm_classes": sorted(
                    Counter(
                        v.failure_class.value if v else "REJECTED" for v in r.verdicts
                    ).items()
                ),
            }
            for r in comparison.cells
        ],
    }
