"""§2.6's adversary generator, held to what docs/EVALUATION.md §10 registered on 2026-09-22.

Seven things are checked here, and each is a claim the row or the design doc
makes that would otherwise be a sentence:

1. The grammar is the criterion's vocabulary and nothing wider.
2. The compiler never evaluates anything, and says so in its syntax tree.
3. The compiler is total — no input crashes it — and rejects what it should.
4. The grammar says what the hand-written attacks say: seven registered attacks
   re-expressed in it leave byte-identical ledgers.
5. The gate the design doc makes non-optional: three hand-written attacks,
   written before any model call, compile, run, survive and are novel — and
   each one fails when the guard it targets is switched off, so its evidence
   is not blind.
6. The novelty signature ignores what a renaming changes.
7. The runner scores a proposal exactly as the registered criterion reads, and
   the budget is one hundred.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import dataclasses
import json
import pathlib
import random
from unittest import mock

import pytest

from vasool.actions import comms
from vasool.policy.guards.autopay_peak_hours import AutopayPeakHoursGuard
from vasool.policy.guards.dlt_template import DLTTemplateGuard
from vasool.policy.guards.mandate_state import MandateStateGuard
from windtunnel.adversary import compile as compiler
from windtunnel.adversary import criterion, grammar, propose
from windtunnel.adversary.attacks import ATTACKS
from windtunnel.adversary.compile import Compiled, Rejected, compile_attack
from windtunnel.adversary.harness import run_attack
from windtunnel.adversary.novelty import is_novel, known_signatures, signature_of_attack
from windtunnel.cassette import CassetteMiss

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HANDWRITTEN = REPO_ROOT / "windtunnel" / "adversary" / "handwritten"
ADVERSARY_CASSETTES = REPO_ROOT / "data" / "cassettes" / "adversary"


def _at(day: int, hour: int, minute: int = 0) -> dict:
    return {"day": day, "hour": hour, "minute": minute}


def _person(bind: str, **fields) -> dict:
    return {"op": "person", "bind": bind, "human_id": fields.pop("human_id", bind), **fields}


def _doc(steps: list, evidence: list | None = None, **top) -> dict:
    return {"id": top.pop("id", "H900"), "title": "t", "targets": "t", "expectation": "survives",
            "steps": steps, "evidence": evidence or [], **top}


def _handwritten(name: str) -> dict:
    return json.loads((HANDWRITTEN / f"{name}.json").read_text())


def _compiled(doc: dict) -> Compiled:
    result = compile_attack(doc)
    assert isinstance(result, Compiled), getattr(result, "reason", result)
    return result


# -- 1 -------------------------------------------------------------------------

class TestTheGrammarIsTheCriterionsVocabulary:
    def test_every_evidence_kind_is_a_criterion_class_with_the_same_fields(self):
        for kind, fields in grammar.EVIDENCE.items():
            cls = getattr(criterion, kind)
            assert {f.name for f in dataclasses.fields(cls)} == set(fields), kind

    def test_the_compilers_tables_have_exactly_the_grammars_keys(self):
        assert set(compiler._DISPATCH) == set(grammar.OPS)
        assert set(compiler._EVIDENCE_TYPES) == set(grammar.EVIDENCE)
        for kind, constructor in compiler._EVIDENCE_TYPES.items():
            assert constructor is getattr(criterion, kind), kind

    def test_no_operation_can_author_a_payload_or_an_open_ended_change(self):
        assert "deliver" not in grammar.OPS
        assert "set_merchant" not in grammar.OPS
        assert set(grammar.OPS["mandate_event"]) == {"person", "trigger"}

    def test_the_description_is_data(self):
        text = json.dumps(grammar.describe())
        assert "grammar_version" in text and "NoExecutionOnEntityAfter" in text

    def test_days_stop_where_the_arena_calendar_does(self):
        assert grammar.MAX_DAY == 30, "Arena.ist builds September dates; a 31st is not one"


# -- 2 -------------------------------------------------------------------------

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr"}


@pytest.mark.parametrize("module", ["compile.py", "grammar.py", "novelty.py", "propose.py"])
def test_the_generator_never_turns_text_into_code(module):
    tree = ast.parse((REPO_ROOT / "windtunnel" / "adversary" / module).read_text())
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & FORBIDDEN_CALLS, f"{module} calls {sorted(called & FORBIDDEN_CALLS)}"


# -- 3 -------------------------------------------------------------------------

JUNK = [None, 0, 1.5, True, "attack", [], [{}], {}, {"id": "H001"},
        _doc([]), _doc("steps"), _doc([None]), _doc([{"op": 7}]), _doc([[]]),
        _doc([{"op": "person"}]), {"id": [], "title": {}, "targets": 3, "expectation": None,
                                   "steps": {}, "evidence": "x"},
        _doc([{"op": "advance_by", "by": {"minutes": "5"}}]),
        _doc([{"op": "advance_to", "at": {"day": 1e9, "hour": 0, "minute": 0}}]),
        _doc([_person("a")] * 50)]


class TestTheCompilerIsTotal:
    @pytest.mark.parametrize("doc", JUNK)
    def test_junk_is_rejected_not_raised(self, doc):
        assert isinstance(compile_attack(doc), Rejected)

    def test_three_hundred_mutations_of_a_real_attack_never_raise(self):
        """Seeded, so a failure here replays."""
        rng = random.Random(20260922)
        base = _handwritten("H001")
        values = [None, 0, -1, 10**9, "", "zz", [], {}, True, 3.14, {"day": 99}]
        for _ in range(300):
            doc = copy.deepcopy(base)
            node, path = doc, []
            for _depth in range(rng.randint(1, 4)):
                if isinstance(node, dict) and node:
                    key = rng.choice(sorted(node))
                elif isinstance(node, list) and node:
                    key = rng.randrange(len(node))
                else:
                    break
                path.append((node, key))
                node = node[key]
            if path:
                parent, key = path[-1]
                if rng.random() < 0.3 and isinstance(parent, dict):
                    del parent[key]
                else:
                    parent[key] = rng.choice(values)
            assert isinstance(compile_attack(doc), (Compiled, Rejected))


BASE = [_person("alice"), {"op": "advance_to", "at": _at(1, 10)},
        {"op": "fail", "person": "alice", "reason": "card_expired", "entity_id": "pay_x"}]


class TestWhatTheCompilerRejects:
    @pytest.mark.parametrize("steps, evidence, expected", [
        (BASE + [{"op": "deliver", "body": {}}], [], "not an operation"),
        (BASE + [{"op": "mark", "label": "m", "colour": "red"}], [], "'colour' is not a field"),
        ([{"op": "fail", "person": "bob", "reason": "card_expired", "entity_id": "pay_x"}], [], "no earlier person"),
        (BASE, [{"kind": "ReceiptCount", "entity_id": "pay_other", "outcome": "EXECUTED", "count": 1}], "no earlier fail"),
        (BASE, [{"kind": "NoExecutionOnEntityAfter", "entity_id": "pay_x", "mark": "never"}], "no earlier mark"),
        (BASE + [{"op": "advance_to", "at": _at(1, 9)}], [], "time runs forwards"),
        (BASE + [{"op": "advance_to", "at": _at(31, 9)}], [], "integer from 1 to 30"),
        ([_person("a"), {"op": "fail", "person": "a", "reason": "issuer_dispatch_failed", "entity_id": "pay_x"}],
         [], "not a card failure reason"),
        ([_person("a"), {"op": "fail", "person": "a", "reason": "card_expired", "entity_id": "pay_x", "upi": True}],
         [], "not a UPI Autopay reason"),
        ([_person("a"), _person("a")], [], "already bound"),
        (BASE + [{"op": "mark", "label": "m"}, {"op": "mark", "label": "m"}], [], "already set"),
        ([_person("a")], [], "no fail step"),
        ([_person("a"), _person("b", contact="+919876543210", same_contact_as="a")] + BASE[1:2], [], "not both"),
        (BASE, [{"kind": "NoRetryExecutedBetweenIST", "open_hour": 21, "close_hour": 17}], "before close_hour"),
        (BASE, [{"kind": "Imaginary"}], "not an evidence type"),
    ])
    def test_it_says_what_is_wrong(self, steps, evidence, expected):
        result = compile_attack(_doc(steps, evidence))
        assert isinstance(result, Rejected) and expected in result.reason, result

    def test_every_proposal_is_scored_against_the_agents_own_claim(self):
        compiled = _compiled(_doc(BASE, expectation="fails"))
        assert compiled.attack.expectation is criterion.Expectation.SURVIVES
        assert compiled.stated_expectation == "fails"

    def test_reconciles_false_builds_the_shipped_default_arena(self):
        assert _compiled(_doc(BASE, reconciles=False)).attack.arena is not None
        assert _compiled(_doc(BASE)).attack.arena is None


# -- 4 -------------------------------------------------------------------------

FAITHFUL = {
    "A01": [_person("alice"), {"op": "advance_to", "at": _at(1, 10)},
            {"op": "fail", "person": "alice", "reason": "gateway_technical_error", "entity_id": "pay_a01"},
            {"op": "advance_by", "by": {"minutes": 6}}, {"op": "fail_last_retry", "entity_id": "pay_a01"},
            {"op": "mark", "label": "money_arrived"}, {"op": "pay_out_of_band", "entity_id": "pay_a01"},
            {"op": "advance_by", "by": {"hours": 6}}],
    "A02": [_person("alice"), {"op": "advance_to", "at": _at(1, 10)},
            {"op": "fail", "person": "alice", "reason": "card_expired", "entity_id": "pay_a02"},
            {"op": "advance_by", "by": {"minutes": 5}}, {"op": "mark", "label": "paid"},
            {"op": "pay_link", "entity_id": "pay_a02"}, {"op": "advance_by", "by": {"hours": 2}}],
    "A07": [_person("one", human_id="rahul", email="rahul@example.invalid"),
            _person("two", human_id="rahul", email="r.kumar@example.invalid", same_contact_as="one"),
            {"op": "advance_to", "at": _at(1, 10)}]
           + [step for i, who in enumerate(["one", "one", "two", "two"])
              for step in ({"op": "fail", "person": who, "reason": "card_expired", "entity_id": f"pay_a07_{i}"},
                           {"op": "advance_by", "by": {"minutes": 30}})],
    "A08": [_person("nyc", human_id="nyc_customer", zone_offset_minutes=-240),
            {"op": "advance_to", "at": _at(1, 3)},
            {"op": "fail", "person": "nyc", "reason": "payment_cancelled", "entity_id": "pay_a08"},
            {"op": "advance_to", "at": _at(1, 21)}],
    "A13": [_person("carol"), {"op": "advance_to", "at": _at(1, 10)},
            {"op": "fail", "person": "carol", "reason": "insufficient_fund", "entity_id": "pay_a13_open"},
            {"op": "mark", "label": "withdrawn"}, {"op": "withdraw_consent", "person": "carol"},
            {"op": "fail", "person": "carol", "reason": "card_expired", "entity_id": "pay_a13_after"},
            {"op": "advance_by", "by": {"hours": 2}}],
    "A18": [_person("alice"), {"op": "advance_to", "at": _at(1, 10)},
            {"op": "fail", "person": "alice", "reason": "gateway_technical_error", "entity_id": "pay_a18"},
            {"op": "promise", "entity_id": "pay_a18", "day": 2}, {"op": "advance_to", "at": _at(4, 12)}],
    "A26": [_person("tok", human_id="tanvi",
                    mandate={"rail": "UPI_AUTOPAY", "category": "GENERAL", "state": "ACTIVE", "valid_days": 365,
                             "mandate_id": "upi_a26", "token_id": "token_a26",
                             "razorpay_customer_id": "cust_a26"}),
            {"op": "jump_to", "at": _at(2, 7)},
            {"op": "fail", "person": "tok", "reason": "issuer_dispatch_failed", "entity_id": "pay_a26", "upi": True},
            {"op": "lose_next_debit_response", "entity_id": "pay_a26"}, {"op": "advance_by", "by": {"days": 3}}],
}


@pytest.mark.parametrize("attack_id", sorted(FAITHFUL))
def test_the_grammar_says_what_the_registered_attack_says(attack_id):
    """Byte-identical ledgers, not similar ones. If the grammar or the compiler
    changed what an operation does, the digest would move."""
    registered = next(a for a in ATTACKS if a.id == attack_id)
    mine = run_attack(_compiled(_doc(FAITHFUL[attack_id], id="H9" + attack_id[-2:])).attack)
    assert mine.digest == run_attack(registered).digest


# -- 5 -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def known():
    return known_signatures(ATTACKS)


@pytest.mark.parametrize("name", ["H001", "H002", "H003"])
def test_the_gate_each_hand_written_attack_compiles_runs_survives_and_is_new(name, known):
    compiled = _compiled(_handwritten(name))
    result = run_attack(compiled.attack)
    assert result.survival.survived, [c for c in result.survival.clauses if not c.held]
    assert is_novel(signature_of_attack(compiled.attack), known)


def _always_allow(cls):
    return mock.patch.object(cls, "check", lambda self, ctx: self.allow("mutation: guard disabled"))


_real_send = comms.CommsSender.send


def _send_ignoring_templates(self, *, proposal, registered_templates, params):
    return _real_send(self, proposal=proposal, registered_templates=frozenset({proposal.template_id}), params=params)


@pytest.mark.parametrize("name, disable", [
    ("H001", [_always_allow(MandateStateGuard)]),
    ("H002", [_always_allow(DLTTemplateGuard), mock.patch.object(comms.CommsSender, "send", _send_ignoring_templates)]),
    ("H003", [_always_allow(AutopayPeakHoursGuard)]),
])
def test_each_hand_written_attack_fails_when_its_target_is_switched_off(name, disable):
    """The attack's evidence is not blind. H003 was first written with
    NoRetryExecutedBetweenIST, which counts card re-presentations only and so
    cannot see a UPI Autopay debit; with the peak-hours guard switched off it
    still survived. It was rewritten until this test could fail."""
    with contextlib.ExitStack() as stack:
        for patch in disable:
            stack.enter_context(patch)
        result = run_attack(_compiled(_handwritten(name)).attack)
    assert not result.survival.survived


def test_the_dlt_rule_has_two_independent_layers():
    """H002 survives with either layer off and fails only with both — so the
    guard and the executor's own template check each stop the link alone."""
    for patch in (_always_allow(DLTTemplateGuard), mock.patch.object(comms.CommsSender, "send", _send_ignoring_templates)):
        with patch:
            assert run_attack(_compiled(_handwritten("H002")).attack).survival.survived


# -- 6 -------------------------------------------------------------------------

def test_a_renamed_attack_has_the_same_signature():
    renamed = json.loads(json.dumps(FAITHFUL["A01"]).replace("alice", "zara").replace("pay_a01", "pay_renamed")
                         .replace("money_arrived", "later"))
    original = next(a for a in ATTACKS if a.id == "A01")
    assert signature_of_attack(_compiled(_doc(renamed)).attack) == signature_of_attack(original)


def test_the_registered_suite_has_fewer_signatures_than_attacks(known):
    """Recorded rather than hidden: attacks that test different things through
    their evidence can exercise the system identically, so the registered
    signature is coarse — which makes "novel" harder to earn, not easier."""
    assert len(known) <= len(ATTACKS)


# -- 7 -------------------------------------------------------------------------

class TestTheProposer:
    def test_the_budget_is_the_registered_one(self):
        assert propose.BUDGET == 100

    def test_no_request_outside_the_budget_can_be_built(self):
        with pytest.raises(ValueError):
            propose.request_for("p", model="m", n=100)

    def test_the_prompt_is_deterministic(self):
        kwargs = dict(grammar_json="{}", worked_example="{}", registered_attacks="A01", guard_source="# guards")
        assert propose.build_prompt(**kwargs) == propose.build_prompt(**kwargs)

    @pytest.mark.parametrize("text, expected", [
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here it is: {"a": [1, 2]} hope that helps', {"a": [1, 2]}),
        ("no json at all", None),
        ('{"a": ', None),
        ("```json\n{not json}\n```", None),
    ])
    def test_json_is_read_by_the_standard_parser_or_not_at_all(self, text, expected):
        assert propose.extract_json(text) == expected


def test_the_runner_scores_by_the_registered_criterion(monkeypatch):
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from tools import generate

    a01 = _doc(FAITHFUL["A01"], id="G003",
               evidence=[{"kind": "NoExecutionOnEntityAfter", "entity_id": "pay_a01", "mark": "money_arrived"}])
    answers = {
        0: "```json\n" + json.dumps(_handwritten("H001")) + "\n```",  # compiles, runs, novel, survives
        1: "I cannot help with that.",                                # not JSON
        2: json.dumps(_doc([{"op": "deliver"}], id="G002")),           # does not compile
        3: json.dumps(a01),                                            # not novel: A01's own signature
    }

    def respond(request):
        if request.repeat in answers:
            return answers[request.repeat]
        raise CassetteMiss(request.key)

    report = generate.run(respond, model="gemini-3.6-flash")
    assert report["counts"] == {"proposed": 4, "compiled": 2, "ran": 2, "novel": 1, "findings": 0}
    assert report["complete"] is False and "proposal 4" in report["stopped"]
    assert report["budget"] == 100 and report["agent_fingerprint"]


def test_every_recorded_adversary_cassette_is_on_the_pinned_model():
    from windtunnel.shadow import PINNED_MODEL
    recorded = [json.loads(p.read_text()) for p in sorted(ADVERSARY_CASSETTES.glob("*.json"))]
    if not recorded:
        pytest.skip("no adversary proposals recorded yet")
    assert {document["model"] for document in recorded} == {PINNED_MODEL}


def test_readme_quotes_the_real_number_of_signatures(known):
    """README prints how many distinct behavioural signatures the registered
    suite has; the number is the suite's, computed, not remembered."""
    import re
    readme = (REPO_ROOT / "README.md").read_text()
    match = re.search(r"have \*\*(\d+)\*\* distinct behavioural signatures", readme)
    assert match, "README no longer quotes the signature count"
    assert int(match.group(1)) == len(known)


def test_a_failed_request_says_why_without_echoing_a_key():
    """A live refusal is reported with its message — the type alone said
    nothing when proposal 0 first failed — and never with a key in it."""
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from tools.generate import reason_for

    fake_key = "AIza" + "x" * 35
    reason = reason_for(RuntimeError(f"404 NOT_FOUND models/gemini-9 for key={fake_key}"))
    assert "404 NOT_FOUND" in reason and fake_key not in reason and "[redacted]" in reason
    assert len(reason_for(RuntimeError("y" * 5000))) < 500
