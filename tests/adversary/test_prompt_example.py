"""The prompt must describe a syntax the compiler accepts.

§10, 2026-09-23. The gate registered on 2026-09-22 required three hand-written
attacks to compile and run before any model was called, and they did — but they
were written by a person reading grammar.py and compile.py. It proved the
grammar can express a new attack. It never proved the *prompt* can, and that is
the claim the hundred proposals rest on: the first six all parsed, none
compiled, and all six were rejected on a field whose type the prompt never
stated.

So the example travels through the real prompt and comes back out. Nothing here
reads H001 from disk: it is found in the assembled text, by the heading the
model sees, parsed with the parser the model's own reply is parsed with, and
compiled by the compiler that judges the model's own reply. A prompt that
describes a shape `compile.py` rejects fails from here on.
"""

import json

import pytest

from windtunnel.adversary import grammar, propose
from windtunnel.adversary.compile import Compiled, compile_attack

from tools.generate import the_prompt, worked_example


@pytest.fixture(scope="module")
def prompt() -> str:
    return the_prompt()


class TestTheWorkedExample:
    def test_the_example_is_in_the_prompt_under_its_heading(self, prompt):
        assert prompt.count(propose.EXAMPLE_HEADING) == 1

    def test_the_example_compiles_out_of_the_assembled_prompt(self, prompt):
        """The whole point: parsed and compiled from the text the model reads,
        not from the file on disk."""
        after = prompt.split(propose.EXAMPLE_HEADING, 1)[1]
        doc = propose.extract_json(after)
        assert doc is not None, "the example is not JSON the model's own parser can read"

        result = compile_attack(doc)
        assert isinstance(result, Compiled), (
            f"the prompt shows an example the compiler rejects: "
            f"{getattr(result, 'reason', result)!r}. The prompt and compile.py "
            f"have drifted apart, which is what cost the first six proposals."
        )

    def test_the_example_shows_the_three_constructions_that_were_guessed(self, prompt):
        """Each of these was invented, differently and wrongly, by the six
        proposals spent on 2026-09-23."""
        doc = propose.extract_json(prompt.split(propose.EXAMPLE_HEADING, 1)[1])
        assert isinstance(doc["targets"], str), "targets was guessed as a list"
        assert all(isinstance(s, dict) and "op" in s for s in doc["steps"]), "the step key was guessed as 'operation'"
        assert isinstance(doc["evidence"], list), "evidence was guessed as an object"

    def test_the_example_is_the_file_the_gate_runs(self):
        """Retyping it here is how it would drift back out of sync."""
        assert json.loads(worked_example())["id"] == "H001"


class TestTheGrammarStatesItsSyntax:
    def test_every_top_level_field_has_a_stated_type(self):
        document = grammar.describe()["document"]
        assert set(document) == set(grammar.TOP_LEVEL), (
            "a field the compiler requires is not described, or one that is "
            "described is not a field"
        )
        for field, stated in document.items():
            assert stated, f"{field} is named but not described"

    def test_the_step_key_and_the_evidence_key_are_stated(self):
        """`"op"` did not occur anywhere in the 74,915-character prompt the six
        proposals were written against."""
        text = json.dumps(grammar.describe())
        assert '\\"op\\"' in text, "the prompt never names the key every step is written with"
        assert '\\"kind\\"' in text, "the prompt never names the key every evidence entry is written with"

    def test_the_description_is_still_only_data(self, prompt):
        """The air gap: the grammar reaches the model as text, so nothing in
        what it renders may be executable."""
        json.dumps(grammar.describe())
        assert "import " not in json.dumps(grammar.describe())
