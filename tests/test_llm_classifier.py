"""The LLM classifier's two pure halves: the prompt, and the parser.

Nothing here reaches the network. `vasool/diagnosis/llm.py` builds a string
and parses a string, and that is deliberately all it does — the provider
client lives in tools/gemini.py and the boundary is scanned by
tests/test_shadow_boundary.py.

**The parser is the load-bearing half.** CLAUDE.md invariant 1 says the LLM
never calls a tool; the mechanism that makes that structural rather than
aspirational is that the only thing an LLM response can become is an
LLMVerdict, whose fields are the closed enums of vasool/diagnosis/taxonomy.py.
An invented class or an invented action dies here, at the boundary, instead of
reaching anything. So most of this file is about rejection.
"""
from __future__ import annotations

import json

import pytest

from vasool.diagnosis.llm import (
    INSTRUCTIONS,
    MAX_RATIONALE_CHARS,
    RESPONSE_SCHEMA,
    VERDICT_KEYS,
    LLMVerdict,
    VerdictRejected,
    build_prompt,
    parse_verdict,
)
from vasool.diagnosis.taxonomy import (
    FailureClass,
    InterventionType,
    known_reasons,
)
from tests.payloads import all_events, event_for, one_event_per_pair


def verdict_json(
    failure_class: str = "TRANSIENT",
    intervention: str = "SILENT_RETRY",
    rationale: str = "the rail failed",
) -> str:
    return json.dumps(
        {
            "failure_class": failure_class,
            "intervention": intervention,
            "rationale": rationale,
        }
    )


class TestPromptIsFieldsOnly:
    """Session 7 registers a fields-only arm. What that has to mean precisely:
    the prompt carries the four error strings and nothing about the customer.

    This is not only a scope decision. Google's pricing page states that
    free-tier data is used to improve their products, and the model this
    comparison runs on is a free-tier one — so a field that never enters the
    prompt is a field that never leaves the machine.
    """

    def test_the_prompt_carries_the_four_error_fields(self):
        event = event_for("card_expired")
        prompt = build_prompt(event)
        assert event.error_reason in prompt
        assert event.error_source in prompt
        assert event.error_code in prompt
        assert event.error_step in prompt

    @pytest.mark.parametrize("event", one_event_per_pair(), ids=lambda e: e.error_reason)
    def test_no_customer_data_reaches_the_prompt(self, event):
        prompt = build_prompt(event)
        for secret in (
            event.customer_id,
            event.entity_id,
            event.merchant_id,
            str(event.amount_paise),
            event.event_id,
        ):
            assert secret not in prompt, f"{secret!r} leaked into the prompt"

    def test_the_prompt_is_a_pure_function_of_the_four_fields(self):
        """Two events differing only in identity must produce the identical
        prompt. This is what collapses the registered universe's ~8,900
        episodes to twelve distinct prompts, and it is what makes the cassette
        layer's key stable across seeds."""
        events = [e for e in all_events() if e.error_reason == "card_expired"]
        assert events, "fixture set changed"
        prompts = {build_prompt(e) for e in events}
        assert len(prompts) == 1

    def test_the_prompt_is_stable_across_calls(self):
        event = event_for("insufficient_fund")
        assert build_prompt(event) == build_prompt(event)


class TestInstructionsDoNotLeakTheAnswer:
    """The comparison is worthless if the prompt contains docs/taxonomy.md §4.

    §8 is explicit that every row of §4 is a dictionary lookup. Pasting that
    dictionary into the prompt would measure whether the model can copy a
    table, which is not a question anyone needs answered. So the instructions
    carry §2 — what the five classes *mean* — and never a reason-to-class
    mapping.
    """

    @pytest.mark.parametrize("reason", sorted(known_reasons()))
    def test_no_canonical_reason_appears_in_the_instructions(self, reason):
        assert reason not in INSTRUCTIONS, (
            f"{reason!r} appears in the instruction template — the prompt is "
            "leaking §4's lookup, and the comparison would measure copying"
        )

    def test_the_five_classes_are_named(self):
        """The enum is closed, so the model has to be told the members. That is
        not leaking the answer; it is the difference between a closed
        vocabulary and free text."""
        for member in FailureClass:
            assert member.value in INSTRUCTIONS

    def test_the_five_interventions_are_named(self):
        for member in InterventionType:
            assert member.value in INSTRUCTIONS


class TestParserAcceptsAWellFormedVerdict:
    def test_a_clean_verdict_parses(self):
        verdict = parse_verdict(verdict_json())
        assert verdict == LLMVerdict(
            failure_class=FailureClass.TRANSIENT,
            intervention=InterventionType.SILENT_RETRY,
            rationale="the rail failed",
        )

    def test_a_fenced_verdict_parses(self):
        """Models wrap JSON in markdown fences even when asked not to. That is
        a formatting quirk, not a malformed verdict, and rejecting it would
        report a model failure that did not happen."""
        fenced = f"```json\n{verdict_json()}\n```"
        assert parse_verdict(fenced).failure_class is FailureClass.TRANSIENT

    def test_surrounding_whitespace_is_tolerated(self):
        assert parse_verdict(f"\n  {verdict_json()}  \n").rationale == "the rail failed"

    def test_case_variants_of_a_real_member_are_tolerated(self):
        """Folding case is normalisation, not interpretation — the same call
        taxonomy.normalise() makes on an error_reason. `risk_block` is the
        model formatting a member of the closed enum; it is not the model
        inventing a class, which is what this parser exists to catch."""
        verdict = parse_verdict(verdict_json("risk_block", "human_queue"))
        assert verdict.failure_class is FailureClass.RISK_BLOCK
        assert verdict.intervention is InterventionType.HUMAN_QUEUE

    def test_a_long_rationale_is_truncated_not_rejected(self):
        """A rationale is prose. Over-long prose is not a protocol violation,
        and rejecting it would throw away a verdict whose class was fine."""
        verdict = parse_verdict(verdict_json(rationale="x" * (MAX_RATIONALE_CHARS * 3)))
        assert len(verdict.rationale) == MAX_RATIONALE_CHARS

    def test_the_verdict_is_frozen(self):
        verdict = parse_verdict(verdict_json())
        with pytest.raises(Exception):
            verdict.failure_class = FailureClass.RISK_BLOCK  # type: ignore[misc]


class TestParserRejectsEverythingElse:
    """CLAUDE.md invariant 1, enforced at the type boundary."""

    def test_an_invented_class_is_rejected(self):
        with pytest.raises(VerdictRejected, match="failure_class"):
            parse_verdict(verdict_json(failure_class="SOFT_DECLINE"))

    def test_an_invented_intervention_is_rejected(self):
        """The clearest case in the file. A model that decides the right answer
        is a refund must not be able to say so in a field anything downstream
        could read — taxonomy.py's InterventionType has no REFUND member, and
        adding one is a taxonomy change that belongs in the document first."""
        with pytest.raises(VerdictRejected, match="intervention"):
            parse_verdict(verdict_json(intervention="REFUND"))

    def test_a_plausible_near_miss_is_still_rejected(self):
        """`RETRY` is not `SILENT_RETRY`. Fuzzy-matching a near miss to the
        closest real member would be the parser inventing a verdict on the
        model's behalf."""
        with pytest.raises(VerdictRejected):
            parse_verdict(verdict_json(intervention="RETRY"))

    def test_a_missing_key_is_rejected(self):
        with pytest.raises(VerdictRejected):
            parse_verdict(json.dumps({"failure_class": "TRANSIENT"}))

    def test_an_extra_key_is_rejected(self):
        """An extra key means the response is not the shape that was asked
        for, and a parser that shrugs at unrequested fields is how a
        `tool_call` key eventually arrives unnoticed."""
        payload = json.loads(verdict_json())
        payload["confidence"] = 0.9
        with pytest.raises(VerdictRejected):
            parse_verdict(json.dumps(payload))

    def test_prose_is_rejected(self):
        with pytest.raises(VerdictRejected):
            parse_verdict("I think this is a transient gateway failure.")

    def test_empty_output_is_rejected(self):
        with pytest.raises(VerdictRejected):
            parse_verdict("")

    def test_a_json_list_is_rejected(self):
        with pytest.raises(VerdictRejected):
            parse_verdict(json.dumps([json.loads(verdict_json())]))

    def test_a_null_class_is_rejected(self):
        with pytest.raises(VerdictRejected):
            parse_verdict(json.dumps({
                "failure_class": None,
                "intervention": "SILENT_RETRY",
                "rationale": "x",
            }))

    def test_an_empty_rationale_is_rejected(self):
        """§4's own rule, applied to the LLM: a rationale is what an audit
        trail needs, and a blank one is not a rationale."""
        with pytest.raises(VerdictRejected):
            parse_verdict(verdict_json(rationale="   "))


class TestResponseSchemaMatchesTheParser:
    """The schema is what the provider is told to emit; the parser is what we
    accept. If they drift, every response is rejected for a reason that looks
    like a model failure."""

    def test_the_schema_names_exactly_the_parsed_keys(self):
        assert set(RESPONSE_SCHEMA["properties"]) == set(VERDICT_KEYS)
        assert set(RESPONSE_SCHEMA["required"]) == set(VERDICT_KEYS)

    def test_the_schema_enumerates_the_closed_vocabularies(self):
        assert set(RESPONSE_SCHEMA["properties"]["failure_class"]["enum"]) == {
            m.value for m in FailureClass
        }
        assert set(RESPONSE_SCHEMA["properties"]["intervention"]["enum"]) == {
            m.value for m in InterventionType
        }

    def test_a_response_matching_the_schema_parses(self):
        """Round-trip: build the minimal document the schema describes and put
        it through the parser."""
        document = {
            "failure_class": FailureClass.INSTRUMENT_DEAD.value,
            "intervention": InterventionType.REAUTH_LINK.value,
            "rationale": "the card cannot authorise again",
        }
        assert parse_verdict(json.dumps(document)).failure_class is (
            FailureClass.INSTRUMENT_DEAD
        )


class TestNoPathToAProposal:
    """The session's one deliberate departure from spec §4.5.

    §4.5 says the LLM classifier emits a Proposal. A Proposal is precisely the
    object actions/executor.py consumes, so constructing one from a model
    response would leave invariant 1 resting on nobody calling the wrong
    function. LLMVerdict is a separate type with no conversion, which makes the
    invariant a fact about the type graph instead.
    """

    def test_the_module_does_not_import_the_proposal_type(self):
        import vasool.diagnosis.llm as llm

        assert not hasattr(llm, "Proposal")
        assert not hasattr(llm, "proposals_from")

    def test_the_verdict_carries_no_route_to_an_action(self):
        verdict = parse_verdict(verdict_json())
        for attribute in ("execute", "to_proposal", "proposals", "apply"):
            assert not hasattr(verdict, attribute)
