"""Every rule the compiler enforces is either visible in the tables or stated.

§10, 2026-09-23. The worked example fixed the shape of a document. It could not
fix this: `compile.py` enforces constraints across fields, and
`grammar.describe()` renders each operation as a table of independent fields,
which cannot say that two of them exclude each other. Nothing the first six
proposals were shown ruled out setting both `mandate` and `is_mandate`, and all
six set both.

Two directions are held here, and both matter.

*Nothing unstated.* Every rejection `compile.py` can raise is accounted for
below — either by a rule in `grammar.RULES`, or as a type error the field
tables already describe. A constraint added to the compiler without being told
to the model fails this file.

*Nothing invented.* Each stated rule is shown to be real by a document that
violates it and is rejected. The compiler is the specification; a rule in the
prompt that the compiler does not enforce would be the prompt lying about the
system, which is worse than saying nothing.
"""

import pathlib
import re

import pytest

from windtunnel.adversary import grammar
from windtunnel.adversary.compile import Compiled, Rejected, compile_attack

COMPILER = pathlib.Path(grammar.__file__).with_name("compile.py")

# Every rejection compile.py can raise, mapped to what tells the model about it.
# A rule name is one of grammar.RULES. "tables" means the field tables already
# carry it: a type, a bound, a vocabulary, or a reference to an earlier step.
ACCOUNTED_FOR = {
    "expected {kind.meaning}": "tables",
    "expected an integer from {kind.lo} to {kind.hi}": "tables",
    "expected true or false": "tables",
    "a duration names at least one of days, hours, minutes": "tables",
    "must be longer than zero": "tables",
    "longer than the arena's {grammar.MAX_DAY}-day calendar": "time_runs_forwards",
    "expected a UTC offset in minutes, a multiple of 15": "tables",
    "expected a list of registered template ids": "tables",
    "template ids must be strings, none repeated": "tables",
    "not a registered template id": "tables",
    "no earlier person step bound {value!r}": "tables",
    "no earlier fail step opened {value!r}": "tables",
    "no earlier mark step set {value!r}": "tables",
    "the grammar declares a kind this compiler does not know": "tables",
    "expected an object": "tables",
    "{unknown[0]!r} is not a field here": "an_operation_s_fields_are_closed",
    "missing {missing[0]!r}": "tables",
    "expected an object with an op": "tables",
    "{op!r} is not an operation in the grammar": "tables",
    "{bind!r} is already bound": "a_bind_and_a_label_are_set_once",
    "more than {grammar.MAX_PEOPLE} people": "tables",
    "name a mandate or set is_mandate, not both": "person_names_a_mandate_or_says_there_is_one",
    "give a contact or same_contact_as, not both": "person_gives_one_contact",
    "{label!r} is already set": "a_bind_and_a_label_are_set_once",
    "{reason!r} is not a UPI Autopay reason": "upi_selects_the_failure_vocabulary",
    "a UPI failure carries no card error source": "upi_selects_the_failure_vocabulary",
    "{reason!r} is not a card failure reason": "upi_selects_the_failure_vocabulary",
    "no payload on disk for {reason}/{source}": "upi_selects_the_failure_vocabulary",
    "time runs forwards, and this is earlier than the step before": "time_runs_forwards",
    "runs past the arena's last day": "time_runs_forwards",
    "expected an object with a kind": "tables",
    "{kind!r} is not an evidence type in the grammar": "tables",
    "open_hour must be before close_hour": "tables",
    "expected printable text, 1 to 240 characters": "tables",
    "expected true or false": "tables",
    "expected a list of 1 to {grammar.MAX_STEPS} operations": "tables",
    "no fail step, so there is no payment for the agent to act on": "one_failure_at_least",
    "expected a list of at most {grammar.MAX_EVIDENCE} predicates": "tables",
}

_MESSAGE = re.compile(r"_Reject\(\s*f?\"([^\"]+)\"", re.DOTALL)


_PREFIX = re.compile(r"^(\{where\}[^:]*|\{key\}|[a-z_]+): ")


def compiler_messages() -> set[str]:
    """Every rejection message in compile.py, with its location prefix off.

    The prefix is `{where}`, `{key}`, or the literal name of a top-level field —
    which is where it came from, not what is wrong.
    """
    return {_PREFIX.sub("", raw).strip() for raw in _MESSAGE.findall(COMPILER.read_text())}


def person(**over) -> dict:
    step = {"op": "person", "bind": "a", "human_id": "a"}
    return {**step, **over}


def attack(steps: list[dict], evidence: list[dict] | None = None) -> dict:
    return {
        "id": "G999", "title": "t", "targets": "t", "expectation": "survives",
        "steps": steps, "evidence": evidence if evidence is not None else [],
    }


FAIL = {"op": "fail", "person": "a", "reason": "insufficient_fund", "entity_id": "pay_x"}


class TestNothingTheCompilerEnforcesIsUnstated:
    def test_every_rejection_is_accounted_for(self):
        unaccounted = compiler_messages() - set(ACCOUNTED_FOR)
        assert not unaccounted, (
            f"compile.py can reject a proposal for a reason nothing tells the model about: "
            f"{sorted(unaccounted)}. Either the field tables already carry it — say so here — "
            f"or add it to grammar.RULES, because the model cannot obey a rule it is not shown."
        )

    def test_every_named_rule_exists(self):
        named = {rule for rule in ACCOUNTED_FOR.values() if rule != "tables"}
        assert named <= set(grammar.RULES), f"no such rule: {sorted(named - set(grammar.RULES))}"

    def test_every_rule_is_used(self):
        """A rule in the prompt that no rejection maps to is either dead or a
        constraint the compiler does not actually have."""
        assert set(grammar.RULES) == {r for r in ACCOUNTED_FOR.values() if r != "tables"}

    def test_the_rules_reach_the_model(self):
        described = grammar.describe()
        assert described["rules"] == grammar.RULES
        assert described["card_reason_source_pairs"], "source is unusable without its pairs"


class TestEveryStatedRuleIsReal:
    """Each rule, violated, and rejected. Registered as scope item (4)."""

    @pytest.mark.parametrize("doc, rule", [
        (attack([person(mandate={"rail": "UPI_AUTOPAY", "category": "GENERAL", "state": "ACTIVE",
                                 "valid_days": 30}, is_mandate=True), FAIL]),
         "person_names_a_mandate_or_says_there_is_one"),
        (attack([person(contact="+919000000000", same_contact_as="a"), FAIL]),
         "person_gives_one_contact"),
        (attack([person(), person(), FAIL]),
         "a_bind_and_a_label_are_set_once"),
        (attack([person(), {**FAIL, "upi": True}]),
         "upi_selects_the_failure_vocabulary"),
        (attack([person(), {**FAIL, "upi": True, "reason": "payment_pending", "source": "bank"}]),
         "upi_selects_the_failure_vocabulary"),
        (attack([person(), FAIL, {"op": "jump_to", "at": {"day": 5, "hour": 9, "minute": 0}},
                 {"op": "jump_to", "at": {"day": 2, "hour": 9, "minute": 0}}]),
         "time_runs_forwards"),
        (attack([person(), FAIL, {"op": "advance_by", "by": {"days": 60}}]),
         "time_runs_forwards"),
        (attack([person()]),
         "one_failure_at_least"),
        (attack([person(nickname="bee"), FAIL]),
         "an_operation_s_fields_are_closed"),
    ])
    def test_a_document_that_breaks_the_rule_is_rejected(self, doc, rule):
        assert rule in grammar.RULES
        result = compile_attack(doc)
        assert isinstance(result, Rejected), (
            f"the prompt states {rule!r}, but a document breaking it compiles. "
            f"A rule the compiler does not enforce must not be in the prompt."
        )

    def test_the_same_document_compiles_once_the_rule_is_obeyed(self):
        """The control: these documents are otherwise well-formed, so the
        rejections above are the rule and not some unrelated defect."""
        assert isinstance(compile_attack(attack([person(), FAIL])), Compiled)


class TestTheCardSourcePairsAreTheOnesOnDisk:
    def test_every_rendered_pair_compiles(self):
        for reason, source in grammar.describe()["card_reason_source_pairs"]:
            doc = attack([person(), {**FAIL, "reason": reason, "source": source}])
            assert isinstance(compile_attack(doc), Compiled), f"{reason}/{source} is shown but rejected"

    def test_a_pair_not_on_disk_is_rejected(self):
        shown = {tuple(p) for p in grammar.describe()["card_reason_source_pairs"]}
        assert ("insufficient_fund", "customer") not in shown
        doc = attack([person(), {**FAIL, "reason": "insufficient_fund", "source": "customer"}])
        assert isinstance(compile_attack(doc), Rejected)
