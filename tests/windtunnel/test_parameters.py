"""windtunnel/parameters.py against docs/EVALUATION.md.

EVALUATION.md is pre-registered and append-only, and its whole value is that
it was written before the numbers existed. That is worth nothing if the
simulator can quietly run on different values than the document registers —
"the evaluation" and "tuning until the numbers look good" become the same
activity again, which is the failure mode §1 names.

So the correspondence is mechanical rather than a matter of care: these tests
parse §3d's and §4's tables out of the markdown and assert the code matches,
in both directions. Editing either side alone fails here.

§4 also requires that "a parameter with no tag fails a test". It cannot even
be constructed — Parameter.provenance has no default — so what is tested here
is the stronger claim that every tag in the code is the tag the document
registers.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from windtunnel.parameters import (
    OUTCOME_PARAMETERS,
    PAYMENT_FAILED_SOURCE_MIX,
    REASON_MIX,
    WORLD_PARAMETERS,
    Parameter,
    Provenance,
    swept,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
EVALUATION = REPO_ROOT / "docs" / "EVALUATION.md"


def _rows(section: str, next_section: str) -> list[list[str]]:
    """Every markdown table row between two headings, as unescaped cells."""
    text = EVALUATION.read_text()
    body = text.split(section, 1)[1].split(next_section, 1)[0]
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        # Split on unescaped pipes only: §4's row labels contain "\|" inside
        # P(success \| ...), and a naive split would tear them in half.
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        rows.append([c.replace("\\|", "|") for c in cells])
    return rows[1:]  # drop the header row


def _section_4_table() -> dict[str, tuple[float, str]]:
    """§4's registered parameters: label -> (value, provenance tag)."""
    registered = {}
    for label, value, provenance in _rows("## 4. The outcome model", "**Seven of eight"):
        number = float(re.search(r"\*\*([\d.]+)", value).group(1))
        tag = re.search(r"\[(\w+)\]", provenance).group(1)
        registered[label] = (number, tag)
    return registered


def _section_3d_table() -> dict[str, float]:
    """§3d's registered reason mix: error_reason -> share."""
    registered = {}
    for reason_cell, share_cell, _class_cell in _rows("### 3d. Universe", "Two consequences"):
        reason = re.search(r"`([\w_]+)`", reason_cell).group(1)
        registered[reason] = float(re.search(r"\*\*([\d.]+)\*\*", share_cell).group(1))
    return registered


class TestSection4Correspondence:
    def test_the_document_still_registers_eight_parameters(self):
        """If §4 grows a row through the §10 amendment procedure, this fails
        and whoever added it has to teach the simulator about it — rather
        than the simulator silently ignoring a registered parameter."""
        assert len(_section_4_table()) == 8

    def test_every_registered_parameter_exists_in_the_code(self):
        registered = set(_section_4_table())
        in_code = {p.registered_as for p in OUTCOME_PARAMETERS.values() if p.registered_as}
        assert registered == in_code

    @pytest.mark.parametrize("label", sorted(_section_4_table()))
    def test_each_registered_value_and_tag_matches_the_code(self, label: str):
        value, tag = _section_4_table()[label]
        parameter = next(p for p in OUTCOME_PARAMETERS.values() if p.registered_as == label)
        assert parameter.value == value
        assert parameter.provenance.value == tag

    def test_the_guess_fraction_is_what_the_document_says_it_is(self):
        """§4 calls 7/8 "itself a headline result". If the code drifts to a
        different mix of tags, the report card's most prominent honesty claim
        becomes false."""
        registered = [p for p in OUTCOME_PARAMETERS.values() if p.registered_as]
        guesses = [p for p in registered if p.provenance is Provenance.GUESS]
        assert (len(guesses), len(registered)) == (7, 8)


class TestSection3dCorrespondence:
    def test_every_registered_reason_appears_in_the_mix(self):
        assert set(_section_3d_table()) == {reason for reason, _ in REASON_MIX}

    def test_every_share_matches_the_registered_share(self):
        registered = _section_3d_table()
        assert dict(REASON_MIX) == registered

    def test_the_shares_sum_to_exactly_one(self):
        """Not "approximately one". §3d is registered, and a mix that needs
        renormalising is a registration error to surface rather than a
        rounding artifact to absorb — windtunnel.rng.choose raises on it."""
        assert round(sum(share for _, share in REASON_MIX), 10) == 1.0

    def test_the_payment_failed_source_split_matches_the_registered_seventy_twenty_five_five(self):
        assert dict(PAYMENT_FAILED_SOURCE_MIX) == {
            "gateway": 0.70,
            "bank": 0.25,
            "business": 0.05,
        }

    def test_the_source_split_is_the_one_written_into_the_document(self):
        """Parsed out of §3d's own parenthetical rather than trusted from the
        code, so the 70/25/5 cannot drift on one side only."""
        row = next(
            cells for cells in _rows("### 3d. Universe", "Two consequences")
            if "payment_failed" in cells[0]
        )
        assert re.search(r"(\d+)/(\d+)/(\d+)", row[0]).groups() == ("70", "25", "5")

    def test_the_generic_case_dominates_as_the_document_argues(self):
        """§3d shapes the mix so "the generic case dominates — which is the
        one thing docs/VERIFIED.md does establish"."""
        shares = dict(REASON_MIX)
        assert shares["payment_failed"] == max(shares.values())


class TestProvenanceDiscipline:
    def test_a_parameter_cannot_be_constructed_without_a_tag(self):
        with pytest.raises(TypeError):
            Parameter(name="untagged", value=0.5)  # type: ignore[call-arg]

    def test_every_parameter_in_either_registry_carries_a_tag(self):
        for registry in (OUTCOME_PARAMETERS, WORLD_PARAMETERS):
            for name, parameter in registry.items():
                assert isinstance(parameter.provenance, Provenance), name

    def test_every_parameter_says_where_it_is_registered(self):
        for registry in (OUTCOME_PARAMETERS, WORLD_PARAMETERS):
            for name, parameter in registry.items():
                assert parameter.registered_in, name

    def test_every_parameter_carries_its_reasoning(self):
        """§4: "so a reader can attack the reasoning rather than the number".
        A bare float in the source defeats that."""
        for registry in (OUTCOME_PARAMETERS, WORLD_PARAMETERS):
            for name, parameter in registry.items():
                assert len(parameter.note) > 40, name

    def test_nothing_invented_here_is_tagged_derived(self):
        """[derived] means "computed from a [cited] figure by stated
        arithmetic" (§4). There are no cited figures in this project, so
        anything derived from a guess is itself a guess — letting [derived]
        launder a number would make the headline guess fraction a lie.

        The single exception is §4's own registered INSTRUMENT_DEAD zero,
        which the pre-registered document tags [derived] on definitional
        grounds. Re-tagging it here would be editing the code to make a
        pre-registered document look consistent, which is backwards.
        """
        derived = [
            p
            for registry in (OUTCOME_PARAMETERS, WORLD_PARAMETERS)
            for p in registry.values()
            if p.provenance is Provenance.DERIVED
        ]
        assert [p.name for p in derived] == ["retry_success_instrument_dead"]

    def test_everything_added_beyond_section_4_is_a_guess(self):
        added = [
            p
            for registry in (OUTCOME_PARAMETERS, WORLD_PARAMETERS)
            for p in registry.values()
            if not p.registered_as
        ]
        assert added, "the §10 additions should be registered here"
        assert all(p.provenance is Provenance.GUESS for p in added)


class TestSweeping:
    def test_a_sweep_changes_one_parameter_and_nothing_else(self):
        """§7 sweeps "every parameter independently ... holding others
        fixed". A sweep that perturbed a second parameter would confound the
        sensitivity result with an effect it was supposed to isolate."""
        base = OUTCOME_PARAMETERS
        after = swept(base, "retry_success_transient", 1.5)

        assert after["retry_success_transient"].value == pytest.approx(0.525)
        assert all(after[k].value == base[k].value for k in base if k != "retry_success_transient")

    def test_the_original_registry_is_never_mutated(self):
        before = OUTCOME_PARAMETERS["retry_success_transient"].value
        swept(OUTCOME_PARAMETERS, "retry_success_transient", 0.5)
        assert OUTCOME_PARAMETERS["retry_success_transient"].value == before

    def test_a_swept_probability_cannot_leave_the_unit_interval(self):
        """§7's +50% on a 0.97 rate would otherwise register 1.455 as a
        probability. Clamping keeps the sweep meaningful at the top of the
        range instead of producing a certainty the parameter cannot express.
        """
        after = swept(WORLD_PARAMETERS, "consent_on_file_rate", 1.5)
        assert after["consent_on_file_rate"].value == 1.0

    def test_a_sweep_of_an_unknown_parameter_is_an_error(self):
        with pytest.raises(KeyError):
            swept(OUTCOME_PARAMETERS, "no_such_parameter", 1.5)

    def test_a_non_probability_parameter_is_not_clamped(self):
        after = swept(OUTCOME_PARAMETERS, "salary_window_uplift", 1.5)
        assert after["salary_window_uplift"].value == pytest.approx(3.0)
