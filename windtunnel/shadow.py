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

PINNED_PROVIDER = "gemini"
PINNED_MODEL = "gemini-3.6-flash"
"""The model every cassette in this repository was recorded on. **Pinned, not
defaulted — do not change this string casually.**

A cassette's address includes the model that produced it
(windtunnel/cassette.py::Request.key), because a response from a different
model is a different measurement and replaying one under the other's name
would be a quiet lie about what was measured. The consequence is that editing
this constant does not re-point the corpus — it **orphans every recording at
once**. Each one becomes a miss, and because a miss is a hard failure by
design, the next `make shadow` stops rather than silently re-recording.

What that costs is a day. The free tier's observed allowance on this model is
**twenty requests per day** (docs in tools/gemini.py), against a twelve-cell
corpus, so re-recording is not a matter of waiting a few minutes — the corpus
does not fit in one day at any depth beyond k=1, and the k=15 stability cell
alone is three quarters of a day.

`tests/windtunnel/test_cassette_pin.py` holds this against the cassettes
actually on disk rather than trusting this comment: change the string without
re-recording and a test fails naming the cost, before the day is spent. The
`--model` flag still exists for a deliberate re-record, and `tools/shadow.py`
prints a warning when it is used.
"""

MIN_REPEATS_FOR_CONSISTENCY = 2
"""Below this, stability is not a question the data can answer. See
CellResult.consistency."""

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

QUOTA_CAVEAT = (
    "Every cell was asked once (k=1). That is not a judgement about "
    "statistical power — k=1 was forced by the free-tier quota, whose observed "
    "cap is twenty requests per day against a twelve-cell corpus. One answer "
    "per cell "
    "measures whether the answer was right; it cannot measure whether the "
    "model would give the same answer again, so the consistency column is "
    "reported as — rather than as the 1.000 the arithmetic would otherwise "
    "produce. Stability is measured separately, on one cell, at depth."
)
"""Rendered only on a k=1 run. Kept out of HEADER_CAVEATS because it is a fact
about a particular run rather than a standing limit of the comparison."""

Respond = Callable[[str, int], str | None]
"""(prompt, repeat index) -> raw response text, or None for "never asked".

The only thing this module knows about how an answer is obtained. `None` is
not an error and not a refusal — it is the absence of a recording, which the
free tier's daily cap makes an ordinary condition rather than an edge case.
Keeping it distinct from a rejected response is the whole of TestPartialCoverage
in tests/windtunnel/test_shadow.py: a cell that was never asked must never be
scored as a cell that answered wrongly, because in a table those look
identical and the second one is the more damning.
"""


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
    two directories the project rules names. `assembled` is the pair whose two halves
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
    """What the LLM said about one cell, over the repeats that were asked."""

    cell: Cell
    requested: int
    """How many repeats the run asked for. `requested - repeats` were never
    recorded."""

    verdicts: tuple[LLMVerdict | None, ...]
    """One entry per repeat that produced a response. `None` is a response the
    parser refused — an invented class, a malformed document, prose. Counted
    separately from a wrong answer, because "said something impossible" and
    "said the wrong class" are different failures with different remedies, and
    separately again from an absent recording, which is not something the model
    did at all."""

    @property
    def repeats(self) -> int:
        """Repeats that actually produced a response. The denominator of every
        rate below, so an unrecorded repeat cannot dilute a score."""
        return len(self.verdicts)

    @property
    def absent(self) -> int:
        return max(self.requested - self.repeats, 0)

    @property
    def measured(self) -> bool:
        return self.repeats > 0

    @property
    def correct(self) -> int:
        return sum(
            1 for v in self.verdicts if v is not None and v.failure_class is self.cell.truth
        )

    @property
    def rejected(self) -> int:
        return sum(1 for v in self.verdicts if v is None)

    @property
    def accuracy(self) -> float | None:
        """None when nothing was recorded. Deliberately not 0.0 — a zero is a
        measurement, and reporting one here would invent a finding."""
        return self.correct / self.repeats if self.measured else None

    @property
    def agreement(self) -> int:
        """Repeats where the LLM matched the *rules*, not the truth."""
        return sum(
            1 for v in self.verdicts if v is not None and v.failure_class is self.cell.rules
        )

    @property
    def consistency(self) -> float | None:
        """How often the modal answer was given, or None when that is not a
        question this data can answer.

        1.0 means it never changed its mind; a rejection counts as its own
        answer, because a model that reliably emits nonsense is consistent
        about it. **None below k=2**, and that matters more than it looks: a
        single response is trivially its own mode, so the arithmetic would
        report a perfect 1.000 on evidence that could not have shown
        instability. Reporting it would be the same mistake as scoring an
        unrecorded cell as 0.000, one column over.
        """
        if self.repeats < MIN_REPEATS_FOR_CONSISTENCY:
            return None
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
    """One row of §4.5's table.

    `cells` and `episodes` describe the class in the corpus; `covered_cells`
    describes how much of it was actually asked. The three are reported side
    by side so that a row resting on one recording cannot be read as a row
    resting on the class.
    """

    failure_class: FailureClass
    cells: int
    covered_cells: int
    episodes: int
    rules_accuracy: float
    llm_accuracy: float | None
    llm_accuracy_weighted: float | None
    consistency: float | None
    rejected: int
    absent: int


def _measured(results: Sequence[CellResult]) -> list[CellResult]:
    """Only the cells that produced a response.

    Every aggregate below runs through this. An unmeasured cell contributes to
    no numerator and to no denominator — it is reported as coverage, never as
    performance.
    """
    return [r for r in results if r.measured]


def _rules_accuracy(results: Sequence[CellResult]) -> float:
    """The rules column, over every cell — covered or not.

    Deliberately not routed through `_measured`. The LLM's numbers are
    measurements and vanish when nothing was asked; the rules classifier's is
    a property of docs/taxonomy.md §4, which answered every cell before any
    recording existed. Filtering it by the LLM's coverage would make a
    definitional 1.000 look like it depended on the run.
    """
    if not results:
        return 0.0
    return sum(float(r.cell.rules is r.cell.truth) for r in results) / len(results)


def _available(results: Sequence[CellResult], value):
    """The cells for which `value` is actually defined, with their values.

    Two separate reasons a cell can have nothing to contribute, and both have
    to be excluded rather than defaulted: it was never recorded, or the
    quantity does not exist at the depth it was recorded to — consistency at
    k=1 being the case that matters. Substituting a zero for either would
    invent a measurement, and substituting a one would invent a better one.
    """
    pairs = [(r, value(r)) for r in results if r.measured]
    return [(r, v) for r, v in pairs if v is not None]


def _unweighted(results: Sequence[CellResult], value) -> float | None:
    available = _available(results, value)
    if not available:
        return None
    return sum(v for _r, v in available) / len(available)


def _weighted(results: Sequence[CellResult], value) -> float | None:
    available = _available(results, value)
    total = sum(r.cell.episodes for r, _v in available)
    if not total:
        return None
    return sum(v * r.cell.episodes for r, v in available) / total


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
    def total_cells(self) -> int:
        return len(self.cells)

    @property
    def covered_cells(self) -> int:
        return len(_measured(self.cells))

    @property
    def complete(self) -> bool:
        """Every cell asked, every repeat recorded. Only a complete comparison
        may be written as the headline artifact — the same rule
        windtunnel/evaluate.py applies to a partial sweep grid, and for the
        same reason: a partial run must not be able to impersonate a full one."""
        return self.covered_cells == self.total_cells and not any(
            r.absent for r in self.cells
        )

    @property
    def total_episodes(self) -> int:
        return sum(r.cell.episodes for r in self.cells)

    @property
    def covered_episodes(self) -> int:
        return sum(r.cell.episodes for r in _measured(self.cells))

    @property
    def absent(self) -> int:
        return sum(r.absent for r in self.cells)

    @property
    def rules_accuracy(self) -> float:
        """Over every cell, covered or not — see `_rules_accuracy`. It is
        1.000 on any subset, by construction, which is what HEADER_CAVEATS
        exists to say out loud."""
        return _rules_accuracy(self.cells)

    @property
    def rules_accuracy_weighted(self) -> float:
        total = sum(r.cell.episodes for r in self.cells)
        if not total:
            return 0.0
        return (
            sum(
                float(r.cell.rules is r.cell.truth) * r.cell.episodes
                for r in self.cells
            )
            / total
        )

    @property
    def llm_accuracy(self) -> float | None:
        return _unweighted(self.cells, lambda r: r.accuracy)

    @property
    def llm_accuracy_weighted(self) -> float | None:
        return _weighted(self.cells, lambda r: r.accuracy)

    @property
    def consistency(self) -> float | None:
        return _unweighted(self.cells, lambda r: r.consistency)

    @property
    def agreement(self) -> float | None:
        """Identical to `llm_accuracy` while the rules define the truth. Kept
        as its own number so the artifact can show the identity rather than
        assert it — see the module docstring."""
        return _unweighted(self.cells, lambda r: r.agreement / r.repeats)

    @property
    def intervention_agreement(self) -> float | None:
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
                covered_cells=len(_measured(group)),
                episodes=sum(r.cell.episodes for r in group),
                rules_accuracy=_rules_accuracy(group),
                llm_accuracy=_unweighted(group, lambda r: r.accuracy),
                llm_accuracy_weighted=_weighted(group, lambda r: r.accuracy),
                consistency=_unweighted(group, lambda r: r.consistency),
                rejected=sum(r.rejected for r in group),
                absent=sum(r.absent for r in group),
            )
        )
    return tuple(rows)


def _ask(cell: Cell, respond: Respond, repeats: int) -> CellResult:
    """Put one cell to the classifier `repeats` times.

    Shared by `compare` and `measure_stability` so that a depth measurement and
    a breadth measurement cannot disagree about what an answer is worth — the
    same parse, the same rejection rule, the same treatment of an absent
    recording.
    """
    verdicts: list[LLMVerdict | None] = []
    for repeat in range(repeats):
        text = respond(cell.prompt, repeat)
        if text is None:
            # Never asked. Not appended, so it lands in `absent` rather than
            # in any rate — see Respond's docstring.
            continue
        try:
            verdicts.append(parse_verdict(text))
        except VerdictRejected:
            verdicts.append(None)
    return CellResult(cell=cell, requested=repeats, verdicts=tuple(verdicts))


def measure_stability(cell: Cell, respond: Respond, *, repeats: int) -> CellResult:
    """Ask one cell many times, to see whether the answer holds.

    **This is not the same question as accuracy, and the section that renders
    it is not allowed to report either number alone.** A model can give the
    same answer every time and be wrong every time — which is what the
    heaviest cell in this corpus does — and reporting the stability figure by
    itself would read as dependability, while reporting the accuracy figure by
    itself would read as noise. Neither is what happened. See
    `render_stability`.

    Depth rather than breadth because breadth is what the daily quota cannot
    afford: twelve cells at k=15 is 180 requests against an allowance of
    twenty, so the corpus is measured once across and once down, and the two
    are reported as the different things they are.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    return _ask(cell, respond, repeats)


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

    results = [_ask(cell, respond, repeats) for cell in corpus]

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


def _fmt(value: float | None, width: int) -> str:
    """A rate, or an em dash when there is nothing to report.

    The single most consequential formatting decision in the file. An
    unmeasured cell printed as `0.000` is indistinguishable, to a reader
    scanning a column, from a cell the model got wrong — and it is the worse
    of the two readings. So it never prints as a number.
    """
    return f"{'—':>{width}}" if value is None else f"{value:>{width}.3f}"


def render_stability(result: CellResult, *, corpus_episodes: int) -> str:
    """One cell, at depth — with its accuracy and its stability side by side.

    **The two numbers are the finding, and neither is reportable alone.** This
    section exists because of a specific result: the heaviest cell in the
    corpus was answered identically on every repeat, and was wrong on every
    repeat. A section headed with the 1.000 would say the model is dependable
    here. A section headed with the 0.000 would say it is unreliable here.
    What is true is neither — it is reliably wrong, which is a worse property
    than being noisy, because noise is visible in production and this is not.

    It is called stability rather than consistency for the same reason. What
    is measured is whether the model says the same thing twice; that is a
    different axis from whether the thing is right, and naming the section
    after one axis is how the two get read as one.
    """
    cell = result.cell
    share = (cell.episodes / corpus_episodes * 100) if corpus_episodes else 0.0
    said = Counter(
        v.failure_class.value if v else "REJECTED" for v in result.verdicts
    )
    proposed = Counter(v.intervention.value for v in result.verdicts if v is not None)

    out: list[str] = []
    out.append(
        f"STABILITY AT DEPTH — {cell.reason} / {cell.source}, k={result.repeats}"
    )
    out.append("")
    out.extend(
        _wrap(
            "Stability is not correctness, and the two are printed together "
            "because separating them loses the point. A model that gives the "
            "same answer every time is stable whether or not the answer is "
            "right — and a stable wrong answer is worse than an unstable one, "
            "because nothing about it looks like a problem."
        )
    )
    out.append("")
    out.append(f"  {'truth':<20}{cell.truth.value}")
    out.append(
        f"  {'rules action':<20}"
        f"{cell.rules_intervention.value if cell.rules_intervention else '—'}"
    )
    out.append(
        f"  {'accuracy':<20}{_fmt(result.accuracy, 5).strip():<8}"
        f"({result.correct} of {result.repeats} repeats matched the registered truth)"
    )
    out.append(
        f"  {'stability':<20}{_fmt(result.consistency, 5).strip():<8}"
        + (
            f"(modal answer given {max(said.values())} of {result.repeats} times)"
            if result.consistency is not None
            else f"(undefined at k={result.repeats} — a single answer is its own mode)"
        )
    )
    out.append(f"  {'rejected':<20}{result.rejected}")
    out.append(
        f"  {'said':<20}{', '.join(f'{k} x{v}' for k, v in said.most_common())}"
    )
    out.append(
        f"  {'proposed':<20}"
        f"{', '.join(f'{k} x{v}' for k, v in proposed.most_common()) or '—'}"
    )
    out.append(
        f"  {'episodes':<20}{cell.episodes} ({share:.1f}% of the corpus by weight)"
    )
    out.append("")
    out.extend(
        _wrap(
            "This is one cell. It generalises to nothing — not to the class it "
            "belongs to, not to the corpus, and not to the model. It is a "
            "statement about this question asked at this depth, and it is "
            "reported separately from the table for exactly that reason."
        )
    )
    return "\n".join(out)


def render_table(comparison: Comparison, *, stability: CellResult | None = None) -> str:
    """§4.5's table, with its limits above it rather than beside it.

    Text rather than HTML because that is what data/golden/ holds and what a
    diff can review; `to_document` is what a later report builder will read.
    """
    out: list[str] = []
    out.append("CLASSIFIER COMPARISON — deterministic rules vs LLM, shadow mode")
    out.append(
        f"provider={comparison.provider}  model={comparison.model}  "
        f"repeats={comparison.repeats}  cells={comparison.total_cells}  "
        f"episodes={comparison.total_episodes}"
    )
    if not comparison.complete:
        out.append("")
        out.append(
            f"*** PARTIAL — {comparison.covered_cells} of {comparison.total_cells} "
            f"cells recorded, {comparison.absent} of "
            f"{comparison.total_cells * comparison.repeats} classifications absent."
        )
        out.extend(
            _wrap(
                "Every rate below is computed over the recorded cells only; an "
                "unrecorded cell is shown as — and contributes to no numerator "
                "and no denominator. This is not a comparison of the classifiers "
                "over the corpus, and must not be read as one.",
                indent="    ",
            )
        )
    out.append("")
    out.append("READ THIS FIRST")
    if comparison.repeats < MIN_REPEATS_FOR_CONSISTENCY:
        out.extend(_wrap(QUOTA_CAVEAT))
        out.append("")
    for caveat in HEADER_CAVEATS:
        out.extend(_wrap(caveat))
        out.append("")

    header = (
        f"{'':<24}{'Rules':>8}{'LLM':>8}{'LLM(wt)':>10}"
        f"{'consist':>9}{'cells':>7}{'covered':>9}{'episodes':>10}"
        f"{'rejected':>10}{'absent':>8}"
    )
    out.append(header)
    out.append("-" * len(header))
    out.append(
        f"{'Overall':<24}{comparison.rules_accuracy:>8.3f}"
        f"{_fmt(comparison.llm_accuracy, 8)}"
        f"{_fmt(comparison.llm_accuracy_weighted, 10)}"
        f"{_fmt(comparison.consistency, 9)}{comparison.total_cells:>7}"
        f"{comparison.covered_cells:>9}"
        f"{comparison.total_episodes:>10}{comparison.rejected:>10}"
        f"{comparison.absent:>8}"
    )
    for row in comparison.rows:
        out.append(
            f"{'  ' + row.failure_class.value:<24}{row.rules_accuracy:>8.3f}"
            f"{_fmt(row.llm_accuracy, 8)}{_fmt(row.llm_accuracy_weighted, 10)}"
            f"{_fmt(row.consistency, 9)}{row.cells:>7}{row.covered_cells:>9}"
            f"{row.episodes:>10}{row.rejected:>10}{row.absent:>8}"
        )

    out.append("")
    out.append("PER CELL — every distinct question the registered universe can ask")
    cell_header = (
        f"{'error_reason / source':<46}{'provenance':<12}"
        f"{'truth':<17}{'LLM':>7}{'consist':>9}{'k':>4}{'episodes':>10}"
    )
    out.append(cell_header)
    out.append("-" * len(cell_header))
    for result in comparison.cells:
        cell = result.cell
        out.append(
            f"{cell.reason + ' / ' + cell.source:<46}{cell.provenance:<12}"
            f"{cell.truth.value:<17}{_fmt(result.accuracy, 7)}"
            f"{_fmt(result.consistency, 9)}{result.repeats:>4}{cell.episodes:>10}"
        )

    out.append("")
    out.append("WHAT THE LLM ACTUALLY SAID, per recorded cell")
    for result in comparison.cells:
        if not result.measured:
            continue
        answers = Counter(
            v.failure_class.value if v else "REJECTED" for v in result.verdicts
        )
        actions = Counter(
            v.intervention.value for v in result.verdicts if v is not None
        )
        out.append(
            f"  {result.cell.reason} / {result.cell.source}  "
            f"(truth {result.cell.truth.value}, "
            f"rules action {result.cell.rules_intervention.value if result.cell.rules_intervention else '—'}, "
            f"k={result.repeats})"
        )
        out.append(f"      classes:       {dict(answers)}")
        out.append(f"      interventions: {dict(actions)}")

    if stability is not None:
        out.append("")
        out.append(render_stability(stability, corpus_episodes=comparison.total_episodes))

    out.append("")
    out.append("SECONDARY")
    out.extend(
        _wrap(
            f"Classifier agreement {_fmt(comparison.agreement, 1).strip()} — identical "
            "to the LLM's accuracy above, and necessarily so while the rules "
            "define the truth. A table reporting only agreement could not show "
            "where the LLM loses, which is the only thing worth reporting."
        )
    )
    out.append("")
    out.extend(
        _wrap(
            f"Intervention agreement {_fmt(comparison.intervention_agreement, 1).strip()} "
            "— how often the LLM chose the action §4 names for the row, at attempt 1."
        )
    )
    out.append("")
    risk_asked = sum(
        r.repeats for r in comparison.cells if r.cell.truth is FailureClass.RISK_BLOCK
    )
    out.extend(
        _wrap(
            f"Automated actions proposed on RISK_BLOCK episodes: "
            f"{comparison.unsafe_risk_block_actions} of {risk_asked} recorded. "
            "docs/taxonomy.md §4.4: this is the one class where the correct "
            "product decision is to do nothing, so the class being right is not "
            "enough — the action has to be HUMAN_QUEUE."
        )
    )
    out.append("")
    return "\n".join(out)


def _stability_document(result: CellResult | None) -> dict | None:
    """The depth measurement, carrying both numbers or neither."""
    if result is None:
        return None
    said = Counter(v.failure_class.value if v else "REJECTED" for v in result.verdicts)
    proposed = Counter(v.intervention.value for v in result.verdicts if v is not None)
    return {
        "error_reason": result.cell.reason,
        "error_source": result.cell.source,
        "truth": result.cell.truth.value,
        "rules_intervention": (
            result.cell.rules_intervention.value
            if result.cell.rules_intervention
            else None
        ),
        "repeats": result.repeats,
        "accuracy": result.accuracy,
        "stability": result.consistency,
        "rejected": result.rejected,
        "said": dict(said),
        "proposed": dict(proposed),
        "episodes": result.cell.episodes,
        "note": (
            "Stability is not correctness. One cell, at depth; generalises to "
            "nothing."
        ),
    }


def to_document(
    comparison: Comparison, *, stability: CellResult | None = None
) -> dict:
    """The machine-readable artifact. Same numbers, same caveats — a JSON
    consumer must not be able to read the table without reading its limits."""
    return {
        "caveats": list(HEADER_CAVEATS),
        "complete": comparison.complete,
        "stability": _stability_document(stability),
        "provider": comparison.provider,
        "model": comparison.model,
        "repeats": comparison.repeats,
        "cells": comparison.total_cells,
        "total_cells": comparison.total_cells,
        "covered_cells": comparison.covered_cells,
        "absent": comparison.absent,
        "episodes": comparison.total_episodes,
        "covered_episodes": comparison.covered_episodes,
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
                "covered_cells": row.covered_cells,
                "episodes": row.episodes,
                "rules_accuracy": row.rules_accuracy,
                "llm_accuracy": row.llm_accuracy,
                "llm_accuracy_weighted": row.llm_accuracy_weighted,
                "consistency": row.consistency,
                "rejected": row.rejected,
                "absent": row.absent,
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
                "repeats": r.repeats,
                "absent": r.absent,
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
