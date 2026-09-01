"""The shadow comparison: the corpus it runs on, and the arithmetic it reports.

Spec §4.5's deliverable is a table, and the table is the point of the session
— not the classifier. So this file is mostly about whether the numbers mean
what the table says they mean.

**No provider is reachable from here.** `compare` takes a callable that turns a
prompt and a repeat index into response text. Every test below passes a
function; the real run passes a cassette lookup. windtunnel/ may not reach the
network at all (tests/windtunnel/test_runner.py::TestNoNetwork), which is why
the dependency is injected rather than imported.
"""
from __future__ import annotations

import json

import pytest

from vasool.diagnosis.taxonomy import FailureClass, InterventionType, lookup
from windtunnel.shadow import (
    CORPUS_SEEDS,
    HEADER_CAVEATS,
    Cell,
    build_corpus,
    compare,
    measure_stability,
    render_stability,
    render_table,
)

PEPPER = "test-pepper-do-not-use-in-prod"
PROVIDER = "fake"
MODEL = "fake-model-1"


@pytest.fixture(scope="module")
def corpus():
    return build_corpus(pepper=PEPPER)


def response(failure_class: str, intervention: str = "SILENT_RETRY") -> str:
    return json.dumps(
        {
            "failure_class": failure_class,
            "intervention": intervention,
            "rationale": "because",
        }
    )


def always(failure_class: str, intervention: str = "SILENT_RETRY"):
    def respond(prompt: str, repeat: int) -> str:
        return response(failure_class, intervention)

    return respond


def oracle(corpus):
    """A perfect classifier: answers every cell with its registered truth."""
    by_prompt = {cell.prompt: cell for cell in corpus}

    def respond(prompt: str, repeat: int) -> str:
        cell = by_prompt[prompt]
        return response(cell.truth.value, "SILENT_RETRY")

    return respond


class TestTheCorpusIsExhaustive:
    """The registered universe generates twelve distinct (reason, source,
    code, step) tuples and no more, so the corpus is not a sample of the input
    space — it is the whole of it. That is a stronger claim than §4.5's
    'n=200 hand-labelled' and it is worth pinning, because the day a
    thirteenth appears the table stops covering the universe."""

    def test_there_are_twelve_cells(self, corpus):
        assert len(corpus) == 12

    def test_every_cell_is_distinct(self, corpus):
        keys = {(c.reason, c.source, c.error_code, c.error_step) for c in corpus}
        assert len(keys) == len(corpus)

    def test_every_prompt_is_distinct(self, corpus):
        """If two cells shared a prompt they would share a cassette, and one
        cell's answer would be reported as the other's."""
        assert len({c.prompt for c in corpus}) == len(corpus)

    def test_the_corpus_covers_every_episode_in_the_seed_range(self, corpus):
        """No episode may fall outside a cell — a cell list that misses one is
        a table that quietly excludes part of the universe."""
        from windtunnel.outcome import OutcomeModel
        from windtunnel.parameters import OUTCOME_PARAMETERS
        from windtunnel.universe import build_universe

        covered = {(c.reason, c.source) for c in corpus}
        for seed in (CORPUS_SEEDS.start, CORPUS_SEEDS.stop - 1):
            outcome = OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=seed)
            universe = build_universe(seed, pepper=PEPPER, outcome=outcome)
            for episode in universe.episodes:
                assert (episode.reason, episode.source) in covered

    def test_all_five_classes_are_represented(self, corpus):
        assert {c.truth for c in corpus} == set(FailureClass)

    def test_the_corpus_is_deterministic(self, corpus):
        again = build_corpus(pepper=PEPPER)
        assert [c.label for c in again] == [c.label for c in corpus]
        assert [c.episodes for c in again] == [c.episodes for c in corpus]

    def test_the_corpus_is_ordered_by_episode_weight(self, corpus):
        """Descending, so a reader meets the cells that decide the weighted
        number first rather than in dictionary order."""
        weights = [c.episodes for c in corpus]
        assert weights == sorted(weights, reverse=True)


class TestProvenanceTravelsWithTheCell:
    """EVALUATION.md §11 registers that this comparison does not generalise
    beyond ten reasons, nine of which are documentation-derived stubs. That
    limit has to be visible in the artifact, not only in the document — so
    every cell carries where its error strings came from."""

    def test_every_cell_has_a_provenance(self, corpus):
        assert all(c.provenance in {"observed", "simulated", "assembled"} for c in corpus)

    def test_exactly_one_reason_is_observed_live(self, corpus):
        """docs/VERIFIED.md: only payment_failed is reproducible in test mode."""
        observed = {c.reason for c in corpus if c.provenance == "observed"}
        assert observed == {"payment_failed"}

    def test_nine_reasons_are_simulated_stubs(self, corpus):
        simulated = {c.reason for c in corpus if c.provenance == "simulated"}
        assert len(simulated) == 9

    def test_the_assembled_pair_is_payment_failed_business(self, corpus):
        """windtunnel/payloads.py: both strings exist on disk, only their
        combination does not. docs/taxonomy.md §9.7 argues that row on cost
        asymmetry rather than on evidence, so it must not read as observed."""
        assembled = {(c.reason, c.source) for c in corpus if c.provenance == "assembled"}
        assert assembled == {("payment_failed", "business")}

    def test_no_cell_invents_an_error_string(self, corpus):
        """the project's hardest rule. Every reason in the corpus has a payload
        in data/, because every event came off disk through
        windtunnel/payloads.py."""
        from tests.payloads import all_events

        on_disk = {e.error_reason for e in all_events()}
        assert {c.reason for c in corpus} <= on_disk


class TestGroundTruthIsTheRulesTableAndTheTableMustSaySo:
    """The degeneracy this whole artifact has to be honest about.

    PlannedEpisode.failure_class calls taxonomy.lookup() on the same two
    fields the rules classifier reads, so the rules column is 1.000 by
    construction and cannot be otherwise. It is still computed rather than
    hardcoded — the day the world grows a cause independent of the table, the
    number moves on its own instead of lying."""

    def test_truth_is_derived_from_the_same_lookup_the_rules_use(self, corpus):
        for cell in corpus:
            assert cell.truth is lookup(cell.reason, cell.source)[1].failure_class

    def test_the_rules_classifier_is_perfect_by_construction(self, corpus):
        for cell in corpus:
            assert cell.rules is cell.truth

    def test_the_rendered_table_states_that_it_is_by_construction(self, corpus):
        comparison = compare(
            corpus, oracle(corpus), repeats=2, provider=PROVIDER, model=MODEL
        )
        rendered = render_table(comparison)
        assert "by construction" in rendered

    def test_agreement_equals_accuracy_under_this_ground_truth(self, corpus):
        """Why agreement is a secondary column and not the headline: when the
        rules are the truth, the two numbers are the same number, so a table
        showing only agreement could not show where the LLM loses."""
        comparison = compare(
            corpus, always("TRANSIENT"), repeats=3, provider=PROVIDER, model=MODEL
        )
        assert comparison.agreement == pytest.approx(comparison.llm_accuracy)


class TestAccuracyArithmetic:
    def test_a_perfect_classifier_scores_one(self, corpus):
        comparison = compare(
            corpus, oracle(corpus), repeats=3, provider=PROVIDER, model=MODEL
        )
        assert comparison.llm_accuracy == pytest.approx(1.0)
        assert comparison.llm_accuracy_weighted == pytest.approx(1.0)
        assert all(row.llm_accuracy == pytest.approx(1.0) for row in comparison.rows)

    def test_a_classifier_that_answers_one_class_scores_that_class_only(self, corpus):
        comparison = compare(
            corpus, always("RISK_BLOCK", "HUMAN_QUEUE"), repeats=2,
            provider=PROVIDER, model=MODEL,
        )
        by_class = {row.failure_class: row for row in comparison.rows}
        assert by_class[FailureClass.RISK_BLOCK].llm_accuracy == pytest.approx(1.0)
        for failure_class, row in by_class.items():
            if failure_class is not FailureClass.RISK_BLOCK:
                assert row.llm_accuracy == pytest.approx(0.0)

    def test_unweighted_and_weighted_accuracy_differ(self, corpus):
        """The twelve cells are wildly unequal in the registered mix, so the
        two ways of averaging are genuinely different claims and the artifact
        reports both. A run where they coincide would mean the sample is
        accidentally representative, which it is not."""
        rare = min(corpus, key=lambda c: c.episodes)

        def respond(prompt: str, repeat: int) -> str:
            correct = next(c for c in corpus if c.prompt == prompt)
            if correct.label == rare.label:
                return response(correct.truth.value)
            return response("LIQUIDITY" if correct.truth is not FailureClass.LIQUIDITY
                            else "TRANSIENT")

        comparison = compare(corpus, respond, repeats=1, provider=PROVIDER, model=MODEL)
        assert comparison.llm_accuracy != pytest.approx(comparison.llm_accuracy_weighted)

    def test_class_rows_carry_their_cell_and_episode_counts(self, corpus):
        """A LIQUIDITY row rests on exactly one distinct input. A reader is
        entitled to see that beside the number rather than infer it."""
        comparison = compare(
            corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL
        )
        by_class = {row.failure_class: row for row in comparison.rows}
        assert by_class[FailureClass.LIQUIDITY].cells == 1
        assert sum(row.cells for row in comparison.rows) == len(corpus)
        assert sum(row.episodes for row in comparison.rows) == sum(
            c.episodes for c in corpus
        )


class TestSelfConsistency:
    """The column §4.5 does not have, and the honest answer to 'you cannot
    replay an LLM'. Non-determinism is not hidden by the cassette; it is
    measured before it is frozen."""

    def test_a_stable_classifier_is_perfectly_consistent(self, corpus):
        comparison = compare(
            corpus, always("TRANSIENT"), repeats=5, provider=PROVIDER, model=MODEL
        )
        assert comparison.consistency == pytest.approx(1.0)

    def test_a_flipping_classifier_is_measured_as_flipping(self, corpus):
        def respond(prompt: str, repeat: int) -> str:
            return response("TRANSIENT" if repeat % 2 == 0 else "LIQUIDITY")

        comparison = compare(corpus, respond, repeats=4, provider=PROVIDER, model=MODEL)
        assert comparison.consistency == pytest.approx(0.5)

    def test_consistency_is_not_accuracy(self, corpus):
        """A model can be perfectly consistent and perfectly wrong."""
        comparison = compare(
            corpus, always("LIQUIDITY"), repeats=3, provider=PROVIDER, model=MODEL
        )
        assert comparison.consistency == pytest.approx(1.0)
        assert comparison.llm_accuracy < 1.0


class TestRejectionsAreReportedNotSwallowed:
    """A response the parser refuses is a result, not an error. Counting it as
    a wrong answer would confuse 'said something impossible' with 'said the
    wrong class', and those are different failures with different remedies."""

    def test_an_invented_class_is_counted_as_a_rejection(self, corpus):
        comparison = compare(
            corpus, always("SOFT_DECLINE"), repeats=2, provider=PROVIDER, model=MODEL
        )
        assert comparison.rejected == len(corpus) * 2
        assert comparison.llm_accuracy == pytest.approx(0.0)

    def test_a_rejection_is_never_scored_as_correct(self, corpus):
        comparison = compare(
            corpus, always("SOFT_DECLINE"), repeats=1, provider=PROVIDER, model=MODEL
        )
        assert all(result.correct == 0 for result in comparison.cells)

    def test_rejections_appear_in_the_rendered_table(self, corpus):
        comparison = compare(
            corpus, always("SOFT_DECLINE"), repeats=1, provider=PROVIDER, model=MODEL
        )
        assert "rejected" in render_table(comparison).lower()


class TestTheRiskBlockRow:
    """docs/taxonomy.md §2: the one class where doing nothing is unambiguously
    correct, and the one the shipped decision turns on. A model that misses a
    RISK_BLOCK proposes an automated action on a fraud-declined payment."""

    def test_a_missed_risk_block_is_visible_as_its_own_row(self, corpus):
        def respond(prompt: str, repeat: int) -> str:
            cell = next(c for c in corpus if c.prompt == prompt)
            if cell.truth is FailureClass.RISK_BLOCK:
                return response("TRANSIENT", "SILENT_RETRY")
            return response(cell.truth.value)

        comparison = compare(corpus, respond, repeats=2, provider=PROVIDER, model=MODEL)
        by_class = {row.failure_class: row for row in comparison.rows}
        assert by_class[FailureClass.RISK_BLOCK].llm_accuracy == pytest.approx(0.0)
        assert by_class[FailureClass.TRANSIENT].llm_accuracy == pytest.approx(1.0)

    def test_an_intervention_on_a_risk_block_is_counted(self, corpus):
        """§4.4: zero outbound, zero retries. The class being right is not
        enough — the action has to be HUMAN_QUEUE too."""
        def respond(prompt: str, repeat: int) -> str:
            cell = next(c for c in corpus if c.prompt == prompt)
            return response(cell.truth.value, "SILENT_RETRY")

        comparison = compare(corpus, respond, repeats=1, provider=PROVIDER, model=MODEL)
        assert comparison.llm_accuracy == pytest.approx(1.0)
        assert comparison.unsafe_risk_block_actions > 0


class TestTheRenderedArtifactCarriesItsLimits:
    """'That limit has to be visible in the artifact itself, not only in the
    doc.' Three of them, each asserted separately so that deleting one is a
    failing test rather than a quiet regression."""

    @pytest.fixture(scope="class")
    def rendered(self, corpus):
        return render_table(
            compare(corpus, oracle(corpus), repeats=2, provider=PROVIDER, model=MODEL)
        )

    def test_it_names_the_model_and_provider(self, rendered):
        assert MODEL in rendered

    def test_it_says_the_model_was_chosen_for_cost(self, rendered):
        assert "cost" in rendered.lower()
        assert "stronger model" in rendered.lower()

    def test_it_says_nine_of_ten_reasons_are_simulated(self, rendered):
        assert "SIMULATED" in rendered
        assert "nine" in rendered.lower() or "9 of" in rendered

    def test_it_says_the_rules_column_is_definitional(self, rendered):
        assert "by construction" in rendered

    def test_every_registered_caveat_is_present(self, rendered):
        """Compared on collapsed whitespace: the renderer wraps to a column
        width, which is layout rather than content. What must not happen is a
        caveat being dropped, shortened, or softened."""
        flat = " ".join(rendered.split())
        for caveat in HEADER_CAVEATS:
            assert " ".join(caveat.split()) in flat

    def test_every_cell_appears_with_its_provenance(self, corpus, rendered):
        for cell in corpus:
            assert cell.reason in rendered

    def test_the_five_classes_each_get_a_row(self, corpus, rendered):
        for member in FailureClass:
            assert member.value in rendered


class TestPartialCoverage:
    """A run that could not ask every question must not look like a run that
    asked and got them wrong.

    The free tier's daily cap is twenty requests, which is smaller than one
    full pass over the corpus, so a partial artifact is the normal case rather
    than an edge case. The danger is entirely in the arithmetic: an uncovered
    cell scored as 0.000 is indistinguishable, in a table, from a cell the
    model failed — and it is the more damning of the two. So an unmeasured
    cell is excluded from every aggregate, rendered as a dash, and counted in
    a coverage line that travels with the numbers.

    Mirrors the convention windtunnel/evaluate.py already sets: a partial grid
    writes `sweeps.json` rather than `evaluation.json` and refuses to report an
    F6 verdict, because a partial run must not be able to overwrite or
    impersonate a run at power.
    """

    @staticmethod
    def only(corpus, labels, *, upto=None):
        """A responder that answers correctly for `labels` and reports every
        other cell as unrecorded by returning None."""
        wanted = set(labels)

        def respond(prompt: str, repeat: int) -> str | None:
            cell = next(c for c in corpus if c.prompt == prompt)
            if cell.label not in wanted:
                return None
            if upto is not None and repeat >= upto:
                return None
            return response(cell.truth.value)

        return respond

    @pytest.fixture(scope="class")
    def partial(self, corpus):
        heaviest = corpus[0].label
        return compare(
            corpus, self.only(corpus, [heaviest]), repeats=4,
            provider=PROVIDER, model=MODEL,
        )

    def test_an_unrecorded_cell_is_not_scored_at_all(self, corpus, partial):
        unmeasured = [r for r in partial.cells if not r.measured]
        assert len(unmeasured) == len(corpus) - 1
        assert all(r.repeats == 0 for r in unmeasured)

    def test_an_unrecorded_repeat_is_absent_not_rejected(self, corpus):
        """The distinction the whole class exists for. A rejection is
        something the model said; an absence is something it was never
        asked."""
        comparison = compare(
            corpus, self.only(corpus, [corpus[0].label], upto=2), repeats=5,
            provider=PROVIDER, model=MODEL,
        )
        first = comparison.cells[0]
        assert first.repeats == 2
        assert first.absent == 3
        assert first.rejected == 0
        assert comparison.rejected == 0

    def test_aggregates_cover_only_the_measured_cells(self, partial):
        """One cell answered perfectly must read as 1.000 over one cell, never
        as 1/12 of the corpus."""
        assert partial.llm_accuracy == pytest.approx(1.0)
        assert partial.covered_cells == 1

    def test_a_class_with_no_coverage_reports_no_number(self, partial):
        by_class = {row.failure_class: row for row in partial.rows}
        uncovered = [row for row in by_class.values() if row.covered_cells == 0]
        assert uncovered, "this fixture should leave classes uncovered"
        assert all(row.llm_accuracy is None for row in uncovered)

    def test_a_partial_comparison_knows_it_is_partial(self, corpus, partial):
        assert partial.complete is False
        assert partial.covered_cells < partial.total_cells
        assert partial.total_cells == len(corpus)

    def test_a_full_comparison_is_complete(self, corpus):
        comparison = compare(
            corpus, oracle(corpus), repeats=2, provider=PROVIDER, model=MODEL
        )
        assert comparison.complete is True
        assert comparison.covered_cells == comparison.total_cells
        assert all(r.absent == 0 for r in comparison.cells)

    def test_the_rendered_table_is_stamped_partial(self, partial):
        rendered = render_table(partial)
        assert "PARTIAL" in rendered
        assert "1 of 12" in rendered

    def test_an_unmeasured_cell_renders_as_a_dash_not_a_zero(self, corpus, partial):
        """The single most important line in this file. A reader scanning the
        per-cell block must not be able to mistake 'never asked' for 'got it
        wrong'."""
        rendered = render_table(partial)
        for line in rendered.splitlines():
            for result in partial.cells:
                if line.startswith(result.cell.reason + " / " + result.cell.source):
                    if result.measured:
                        continue
                    assert "0.000" not in line, line
                    assert "—" in line or "-" in line

    def test_a_full_table_is_not_stamped_partial(self, corpus):
        rendered = render_table(
            compare(corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL)
        )
        assert "PARTIAL" not in rendered

    def test_a_cell_with_no_coverage_carries_no_weight(self, corpus, partial):
        """Weighted accuracy over one covered cell is that cell's accuracy —
        the uncovered episodes are not in the denominator, because counting
        them would silently report the corpus as mostly-wrong."""
        assert partial.llm_accuracy_weighted == pytest.approx(1.0)

    def test_the_document_records_coverage(self, partial):
        from windtunnel.shadow import to_document

        document = to_document(partial)
        assert document["complete"] is False
        assert document["covered_cells"] == 1
        assert document["total_cells"] == 12
        uncovered = [c for c in document["by_cell"] if c["repeats"] == 0]
        assert all(c["llm_accuracy"] is None for c in uncovered)


class TestConsistencyIsUndefinedAtKEqualsOne:
    """One answer per cell cannot measure whether the answer is stable.

    Left alone, the modal-answer arithmetic reports 1.000 at k=1 — the single
    response is trivially its own mode — and a reader scanning the column sees
    a model that never changes its mind, on evidence that could not have shown
    it changing. That is the same failure the em dash exists to prevent one
    column over, so it gets the same treatment.
    """

    def test_a_single_repeat_reports_no_consistency(self, corpus):
        comparison = compare(
            corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL
        )
        assert all(result.consistency is None for result in comparison.cells)
        assert comparison.consistency is None
        assert all(row.consistency is None for row in comparison.rows)

    def test_accuracy_is_still_measured_at_k_equals_one(self, corpus):
        """Only stability is undefined. One answer is a perfectly good
        measurement of whether that answer was right."""
        comparison = compare(
            corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL
        )
        assert comparison.llm_accuracy == pytest.approx(1.0)

    def test_two_repeats_are_enough_to_report_consistency(self, corpus):
        comparison = compare(
            corpus, oracle(corpus), repeats=2, provider=PROVIDER, model=MODEL
        )
        assert comparison.consistency == pytest.approx(1.0)

    def test_the_column_renders_as_a_dash_at_k_equals_one(self, corpus):
        rendered = render_table(
            compare(corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL)
        )
        overall = next(l for l in rendered.splitlines() if l.startswith("Overall"))
        assert "1.000" in overall, "accuracy should still be reported"
        assert overall.count("1.000") < 4, f"consistency leaked a number: {overall}"

    def test_a_k_equals_one_run_says_the_quota_forced_it(self, corpus):
        """Not an aside. A reader has to know that k=1 is what the free tier
        allowed across twelve cells, not a considered choice about power."""
        rendered = render_table(
            compare(corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL)
        )
        flat = " ".join(rendered.split()).lower()
        assert "k=1" in flat
        assert "quota" in flat

    def test_a_deeper_run_does_not_carry_the_quota_note(self, corpus):
        rendered = render_table(
            compare(corpus, oracle(corpus), repeats=3, provider=PROVIDER, model=MODEL)
        )
        assert "forced" not in rendered.lower()


class TestStabilityAtDepth:
    """One cell, asked many times — and reported with its accuracy attached.

    The section exists because of a specific result: the heaviest cell in the
    corpus answered identically fifteen times out of fifteen, and was wrong
    fifteen times out of fifteen. Reporting the 1.000 on its own would say the
    model is dependable. Reporting the 0.000 on its own would say it is
    unreliable. Neither is what happened, and the pair is the finding — so the
    renderer is not allowed to print one without the other.

    Also why the section is not called consistency. What it measures is
    stability: whether the model says the same thing twice. That is a
    different axis from whether the thing is right, and naming the section
    after only one axis is how the two get conflated.
    """

    @pytest.fixture(scope="class")
    def wrong_but_stable(self, corpus):
        """A model that answers one cell identically every time, and wrongly."""
        cell = corpus[0]

        def respond(prompt: str, repeat: int) -> str:
            other = next(c for c in FailureClass if c is not cell.truth)
            return response(other.value)

        return measure_stability(cell, respond, repeats=15)

    def test_it_measures_the_requested_depth(self, wrong_but_stable):
        assert wrong_but_stable.repeats == 15
        assert wrong_but_stable.absent == 0

    def test_stability_and_accuracy_disagree_and_both_are_recorded(self, wrong_but_stable):
        assert wrong_but_stable.consistency == pytest.approx(1.0)
        assert wrong_but_stable.accuracy == pytest.approx(0.0)

    def test_the_section_prints_both_numbers(self, corpus, wrong_but_stable):
        section = render_stability(wrong_but_stable, corpus_episodes=sum(c.episodes for c in corpus))
        assert "0.000" in section
        assert "1.000" in section

    def test_the_section_says_stability_is_not_correctness(self, corpus, wrong_but_stable):
        section = render_stability(
            wrong_but_stable, corpus_episodes=sum(c.episodes for c in corpus)
        ).lower()
        assert "stability" in section
        assert "not correctness" in section or "is not correct" in section

    def test_the_section_is_not_titled_consistency(self, corpus, wrong_but_stable):
        """The retitle, asserted. `consistency` alone names one axis and the
        section is about the gap between two."""
        section = render_stability(
            wrong_but_stable, corpus_episodes=sum(c.episodes for c in corpus)
        )
        title = section.strip().splitlines()[0]
        assert "STABILITY" in title.upper()
        assert "CONSISTENCY" not in title.upper()

    def test_the_section_names_the_cell_and_says_it_generalises_to_nothing(
        self, corpus, wrong_but_stable
    ):
        section = render_stability(
            wrong_but_stable, corpus_episodes=sum(c.episodes for c in corpus)
        )
        assert corpus[0].reason in section
        assert "one cell" in section.lower()

    def test_the_section_reports_what_was_actually_said(self, corpus, wrong_but_stable):
        section = render_stability(
            wrong_but_stable, corpus_episodes=sum(c.episodes for c in corpus)
        )
        said = wrong_but_stable.verdicts[0]
        assert said is not None
        assert said.failure_class.value in section
        assert said.intervention.value in section

    def test_stability_cannot_be_claimed_at_one_repeat(self, corpus):
        """The k=1 rule, applied here too. A single answer is trivially its own
        mode, and a section headed 'stability' printing 1.000 off one response
        would be the most misleading thing in the artifact."""
        cell = corpus[0]
        result = measure_stability(cell, lambda p, r: response(cell.truth.value), repeats=1)
        assert result.consistency is None
        section = render_stability(result, corpus_episodes=1000)
        assert "1.000" not in section.split("accuracy")[0]
        assert "—" in section

    def test_a_perfectly_correct_stable_cell_reads_as_both(self, corpus):
        cell = corpus[0]
        result = measure_stability(
            cell, lambda p, r: response(cell.truth.value), repeats=4
        )
        section = render_stability(result, corpus_episodes=sum(c.episodes for c in corpus))
        assert result.accuracy == pytest.approx(1.0)
        assert result.consistency == pytest.approx(1.0)
        assert "1.000" in section


class TestTheStabilitySectionRidesTheArtifact:
    """It has to travel with the table, not beside it in a second file — the
    whole point is that a reader meets the pair together."""

    def test_the_table_carries_the_section_when_one_is_given(self, corpus):
        cell = corpus[0]
        stability = measure_stability(
            cell, lambda p, r: response(cell.truth.value), repeats=5
        )
        rendered = render_table(
            compare(corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL),
            stability=stability,
        )
        assert "STABILITY" in rendered.upper()

    def test_the_table_omits_the_section_when_none_is_given(self, corpus):
        rendered = render_table(
            compare(corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL)
        )
        assert "STABILITY AT DEPTH" not in rendered.upper()

    def test_the_document_carries_it_too(self, corpus):
        from windtunnel.shadow import to_document

        cell = corpus[0]
        stability = measure_stability(
            cell, lambda p, r: response(cell.truth.value), repeats=5
        )
        document = to_document(
            compare(corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL),
            stability=stability,
        )
        assert document["stability"]["repeats"] == 5
        assert document["stability"]["error_reason"] == cell.reason
        assert document["stability"]["accuracy"] is not None
        assert document["stability"]["stability"] is not None

    def test_the_document_omits_it_when_absent(self, corpus):
        from windtunnel.shadow import to_document

        document = to_document(
            compare(corpus, oracle(corpus), repeats=1, provider=PROVIDER, model=MODEL)
        )
        assert document["stability"] is None
