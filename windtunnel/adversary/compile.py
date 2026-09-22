"""A proposal, checked and turned into an `Attack`. Pure, total, and it never evaluates anything.

The design doc's §2.6, registered in docs/EVALUATION.md §10 on 2026-09-22,
asks three things of this module and each is a property of its shape rather
than a promise in a comment:

**Pure.** `compile_attack` reads a JSON-shaped value and returns a value. It
touches no arena, no clock, no file and no network. The `Attack` it returns
carries a `run` that will act on an arena later, when the harness hands it one;
compiling a proposal and running it are different events, and only the second
can change anything.

**Total.** Every input gets an answer. A proposal that is not a dict, names an
operation that does not exist, passes a field the grammar does not declare,
refers to a person nobody bound, or moves time backwards comes back as
`Rejected` with the first problem and where it is — never as an exception, and
never as a silently narrower attack. A model's output is the least trusted input
in this repository, and a compiler that could be crashed by one is a compiler
whose "compiled" count means nothing.

**No eval, ever.** Nothing here turns text into code. Operations are dispatched
through an explicit table whose keys are exactly `grammar.OPS`; evidence is
built by an explicit table of constructors; no name read from a proposal is
ever looked up as an attribute. `tests/adversary/test_generator.py` walks this
file's syntax tree and fails on `eval`, `exec`, `compile`, `__import__`,
`getattr` or `setattr`.

**What compiling does not decide.** Whether the arena can actually play the
script: `fail_last_retry` on a payment the agent never retried, or a mandate
trigger the lifecycle refuses, is only knowable by running it, and those are
reported as a run that did not complete — not as findings, because the agent
did nothing wrong in a world that could not happen.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from vasool.diagnosis.rules import IST
from vasool.ledger.receipts import Outcome
from vasool.mandate.machine import Trigger
from vasool.mandate.record import MandateCategory, MandateRail, MandateRecord
from vasool.mandate.states import MandateState
from windtunnel import payloads
from windtunnel.adversary import criterion, grammar
from windtunnel.adversary.arena import DEFAULT_AMOUNT_PAISE, EPOCH, Arena
from windtunnel.adversary.criterion import Attack, Expectation

__all__ = ["Compiled", "Rejected", "compile_attack"]


@dataclass(frozen=True, slots=True)
class Step:
    """One operation with its arguments already checked and converted."""

    op: str
    args: tuple[tuple[str, Any], ...]

    def arg(self, name: str, default: Any = None) -> Any:
        for key, value in self.args:
            if key == name:
                return value
        return default


@dataclass(frozen=True, slots=True)
class Compiled:
    attack: Attack
    steps: tuple[Step, ...]
    stated_expectation: str
    """What the proposal said would happen. Recorded; not used for scoring."""


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: str


class _Reject(Exception):
    """Internal only. `compile_attack` converts it; it never escapes this module."""


# -- values -----------------------------------------------------------------------

def _ist(day: int, hour: int, minute: int) -> datetime:
    """The same instant `Arena.ist` would build — the compiler keeps its own
    clock so it can refuse a script that moves time backwards."""
    return datetime(EPOCH.astimezone(IST).year, 9, day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


END_OF_CALENDAR = _ist(grammar.MAX_DAY, 23, 59)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check(kind: object, value: object, where: str, refs: "_Refs") -> Any:
    """Check one value against its kind and return it converted."""
    if isinstance(kind, grammar.Pattern):
        if not isinstance(value, str) or re.fullmatch(kind.regex, value) is None:
            raise _Reject(f"{where}: expected {kind.meaning}")
        return value
    if isinstance(kind, grammar.Integer):
        if not _is_int(value) or not (kind.lo <= value <= kind.hi):
            raise _Reject(f"{where}: expected an integer from {kind.lo} to {kind.hi}")
        return value
    if isinstance(kind, grammar.Boolean):
        if not isinstance(value, bool):
            raise _Reject(f"{where}: expected true or false")
        return value
    if isinstance(kind, grammar.OneOf):
        if not isinstance(value, str) or value not in kind.values:
            raise _Reject(f"{where}: expected {kind.meaning}")
        return value
    if isinstance(kind, grammar.Instant):
        fields = _record(value, where, required={"day", "hour", "minute"}, optional=set())
        day = _check(grammar.Integer(1, grammar.MAX_DAY), fields["day"], f"{where}.day", refs)
        hour = _check(grammar.Integer(0, 23), fields["hour"], f"{where}.hour", refs)
        minute = _check(grammar.Integer(0, 59), fields["minute"], f"{where}.minute", refs)
        return (day, hour, minute)
    if isinstance(kind, grammar.Duration):
        fields = _record(value, where, required=set(), optional={"days", "hours", "minutes"})
        if not fields:
            raise _Reject(f"{where}: a duration names at least one of days, hours, minutes")
        delta = timedelta(
            days=_check(grammar.Integer(0, grammar.MAX_DAY), fields.get("days", 0), f"{where}.days", refs),
            hours=_check(grammar.Integer(0, 24 * grammar.MAX_DAY), fields.get("hours", 0), f"{where}.hours", refs),
            minutes=_check(grammar.Integer(0, 24 * 60 * grammar.MAX_DAY), fields.get("minutes", 0),
                           f"{where}.minutes", refs),
        )
        if kind.positive and delta <= timedelta(0):
            raise _Reject(f"{where}: must be longer than zero")
        if delta > timedelta(days=grammar.MAX_DAY):
            raise _Reject(f"{where}: longer than the arena's {grammar.MAX_DAY}-day calendar")
        return delta
    if isinstance(kind, grammar.Offset):
        if not _is_int(value) or not (-720 <= value <= 840) or value % 15:
            raise _Reject(f"{where}: expected a UTC offset in minutes, a multiple of 15")
        return timezone(timedelta(minutes=value))
    if isinstance(kind, grammar.Mandate):
        required = {name for name, spec in grammar.MANDATE_FIELDS.items() if spec.required}
        fields = _record(value, where, required=required, optional=set(grammar.MANDATE_FIELDS))
        checked = {name: _check(grammar.MANDATE_FIELDS[name].kind, fields[name], f"{where}.{name}", refs)
                   for name in fields}
        return checked  # built into a MandateRecord once the person's id is known
    if isinstance(kind, grammar.Templates):
        if not isinstance(value, list) or len(value) > len(grammar.REGISTERED_TEMPLATES):
            raise _Reject(f"{where}: expected a list of registered template ids")
        if len(set(v for v in value if isinstance(v, str))) != len(value):
            raise _Reject(f"{where}: template ids must be strings, none repeated")
        for index, template in enumerate(value):
            if template not in grammar.REGISTERED_TEMPLATES:
                raise _Reject(f"{where}[{index}]: not a registered template id")
        return frozenset(value)
    if isinstance(kind, grammar.PersonRef):
        if not isinstance(value, str) or value not in refs.people:
            raise _Reject(f"{where}: no earlier person step bound {value!r}")
        return value
    if isinstance(kind, grammar.EntityRef):
        if not isinstance(value, str) or value not in refs.entities:
            raise _Reject(f"{where}: no earlier fail step opened {value!r}")
        return value
    if isinstance(kind, grammar.MarkRef):
        if not isinstance(value, str) or value not in refs.marks:
            raise _Reject(f"{where}: no earlier mark step set {value!r}")
        return value
    raise _Reject(f"{where}: the grammar declares a kind this compiler does not know")


def _record(value: object, where: str, *, required: set[str], optional: set[str]) -> dict[str, Any]:
    """A JSON object with exactly these keys: all of `required`, any of `optional`."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _Reject(f"{where}: expected an object")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise _Reject(f"{where}: {unknown[0]!r} is not a field here")
    missing = sorted(required - set(value))
    if missing:
        raise _Reject(f"{where}: missing {missing[0]!r}")
    return value


@dataclass
class _Refs:
    people: dict[str, str]  # bind name -> human_id
    entities: set[str]
    marks: set[str]
    clock: datetime


def _fields(spec: Mapping[str, grammar.Field], body: dict[str, Any], where: str,
            refs: _Refs) -> tuple[tuple[str, Any], ...]:
    required = {name for name, field in spec.items() if field.required}
    fields = _record(body, where, required=required, optional=set(spec))
    return tuple((name, _check(spec[name].kind, fields[name], f"{where}.{name}", refs)) for name in sorted(fields))


# -- steps ------------------------------------------------------------------------

def _step(index: int, raw: object, refs: _Refs) -> Step:
    where = f"steps[{index}]"
    if not isinstance(raw, dict) or not isinstance(raw.get("op"), str):
        raise _Reject(f"{where}: expected an object with an op")
    op = raw["op"]
    if op not in grammar.OPS:
        raise _Reject(f"{where}: {op!r} is not an operation in the grammar")
    body = {key: value for key, value in raw.items() if key != "op"}
    args = _fields(grammar.OPS[op], body, where, refs)
    step = Step(op=op, args=args)

    if op == "person":
        bind = step.arg("bind")
        if bind in refs.people:
            raise _Reject(f"{where}.bind: {bind!r} is already bound")
        if len(refs.people) >= grammar.MAX_PEOPLE:
            raise _Reject(f"{where}: more than {grammar.MAX_PEOPLE} people")
        if step.arg("mandate") is not None and step.arg("is_mandate") is not None:
            raise _Reject(f"{where}: name a mandate or set is_mandate, not both")
        if step.arg("contact") is not None and step.arg("same_contact_as") is not None:
            raise _Reject(f"{where}: give a contact or same_contact_as, not both")
        refs.people[bind] = step.arg("human_id")
    elif op == "mark":
        label = step.arg("label")
        if label in refs.marks:
            raise _Reject(f"{where}.label: {label!r} is already set")
        refs.marks.add(label)
    elif op == "fail":
        reason, upi, source = step.arg("reason"), step.arg("upi", False), step.arg("source")
        if upi:
            if reason not in grammar.UPI_REASONS:
                raise _Reject(f"{where}.reason: {reason!r} is not a UPI Autopay reason")
            if source is not None:
                raise _Reject(f"{where}.source: a UPI failure carries no card error source")
        else:
            if reason not in grammar.CARD_REASONS:
                raise _Reject(f"{where}.reason: {reason!r} is not a card failure reason")
            if source is not None and (reason, source) not in payloads.available_pairs():
                raise _Reject(f"{where}.source: no payload on disk for {reason}/{source}")
        refs.entities.add(step.arg("entity_id"))
    elif op in {"advance_to", "jump_to"}:
        target = _ist(*step.arg("at"))
        if target < refs.clock:
            raise _Reject(f"{where}.at: time runs forwards, and this is earlier than the step before")
        refs.clock = target
    elif op == "advance_by":
        refs.clock = refs.clock + step.arg("by")
        if refs.clock > END_OF_CALENDAR:
            raise _Reject(f"{where}.by: runs past the arena's last day")
    return step


# -- evidence ---------------------------------------------------------------------

_EVIDENCE_TYPES: dict[str, Callable[..., criterion.Evidence]] = {
    "ExecutedCount": criterion.ExecutedCount,
    "RailDebitsAtMost": criterion.RailDebitsAtMost,
    "ExecutedAtMost": criterion.ExecutedAtMost,
    "ReceiptWithOutcome": criterion.ReceiptWithOutcome,
    "ReceiptCount": criterion.ReceiptCount,
    "ReceiptNoLaterThan": criterion.ReceiptNoLaterThan,
    "NoExecutionOnEntityAfter": criterion.NoExecutionOnEntityAfter,
    "NoDebitBeforeNoticeMatures": criterion.NoDebitBeforeNoticeMatures,
    "NoContactOutsideCustomerWindow": criterion.NoContactOutsideCustomerWindow,
    "NoContactToDndListed": criterion.NoContactToDndListed,
    "ContactsPerHumanWithin": criterion.ContactsPerHumanWithin,
    "NoRetryExecutedBetweenIST": criterion.NoRetryExecutedBetweenIST,
}
"""Exactly `grammar.EVIDENCE`'s keys, each mapped to its class by hand.
tests/adversary/test_generator.py fails if the two ever disagree."""


def _evidence(index: int, raw: object, refs: _Refs) -> criterion.Evidence:
    where = f"evidence[{index}]"
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        raise _Reject(f"{where}: expected an object with a kind")
    kind = raw["kind"]
    if kind not in grammar.EVIDENCE:
        raise _Reject(f"{where}: {kind!r} is not an evidence type in the grammar")
    body = {key: value for key, value in raw.items() if key != "kind"}
    args = dict(_fields(grammar.EVIDENCE[kind], body, where, refs))
    if "outcome" in args:
        args["outcome"] = Outcome[args["outcome"]]
    if kind == "NoRetryExecutedBetweenIST" and not args["open_hour"] < args["close_hour"]:
        raise _Reject(f"{where}: open_hour must be before close_hour")
    return _EVIDENCE_TYPES[kind](**args)


# -- running a compiled script ----------------------------------------------------

def _at(arena: Arena, value: tuple[int, int, int]) -> datetime:
    day, hour, minute = value
    return arena.ist(day=day, hour=hour, minute=minute)


def _person(arena: Arena, people: dict[str, Any], step: Step) -> None:
    mandate = step.arg("mandate")
    record = None
    if mandate is not None:
        optional = {key: mandate[key] for key in ("token_id", "razorpay_customer_id", "revocable_by_payer")
                    if key in mandate}
        record = MandateRecord(
            mandate_id=mandate.get("mandate_id", f"arena_mandate_{step.arg('human_id')}"),
            rail=MandateRail[mandate["rail"]],
            category=MandateCategory[mandate["category"]],
            state=MandateState[mandate["state"]],
            valid_until=EPOCH + timedelta(days=mandate["valid_days"]),
            **optional,
        )
    shared = step.arg("same_contact_as")
    people[step.arg("bind")] = arena.person(
        step.arg("human_id"),
        contact=people[shared].contact if shared is not None else step.arg("contact"),
        email=step.arg("email"),
        zone=step.arg("zone_offset_minutes"),
        dnd_listed=step.arg("dnd_listed", False),
        is_mandate=step.arg("is_mandate", False),
        mandate=record,
        consent=None if step.arg("consent") == "none" else "default",
    )


def _fail(arena: Arena, people: dict[str, Any], step: Step) -> None:
    occurred = step.arg("occurred_at")
    arena.fail(
        people[step.arg("person")],
        step.arg("reason"),
        source=step.arg("source"),
        amount_paise=step.arg("amount_paise", DEFAULT_AMOUNT_PAISE),
        entity_id=step.arg("entity_id"),
        occurred_at=None if occurred is None else _at(arena, occurred),
        event_id=step.arg("event_id"),
        upi=step.arg("upi", False),
    )


def _mandate_event(arena: Arena, people: dict[str, Any], step: Step) -> None:
    bind = step.arg("person")
    people[bind] = arena.mandate_event(people[bind], Trigger[step.arg("trigger")])


_DISPATCH: dict[str, Callable[[Arena, dict[str, Any], Step], None]] = {
    "person": _person,
    "advance_to": lambda arena, people, step: arena.advance_to(_at(arena, step.arg("at"))),
    "advance_by": lambda arena, people, step: arena.advance_by(step.arg("by")),
    "jump_to": lambda arena, people, step: arena.jump_to(_at(arena, step.arg("at"))),
    "mark": lambda arena, people, step: arena.mark(step.arg("label")),
    "fail": _fail,
    "fail_last_retry": lambda arena, people, step: arena.fail_last_retry(step.arg("entity_id")),
    "pay_link": lambda arena, people, step: arena.pay_link(step.arg("entity_id"), event_id=step.arg("event_id")),
    "capture_last_retry": lambda arena, people, step: arena.capture_last_retry(step.arg("entity_id")),
    "pay_out_of_band": lambda arena, people, step: arena.pay_out_of_band(step.arg("entity_id")),
    "lose_next_debit_response": lambda arena, people, step: arena.lose_next_debit_response(step.arg("entity_id")),
    "withdraw_consent": lambda arena, people, step: arena.withdraw_consent(people[step.arg("person")]),
    "mandate_event": _mandate_event,
    "promise": lambda arena, people, step: arena.promise(step.arg("entity_id"), arena.ist_date(step.arg("day"))),
    "poison_dedupe_oracle": lambda arena, people, step: arena.poison_dedupe_oracle(),
    "set_registered_templates": lambda arena, people, step: arena.set_registered_templates(step.arg("templates")),
}
"""Exactly `grammar.OPS`'s keys. Each entry calls one named arena method with
arguments the compiler has already checked — there is no path from a string in
a proposal to an attribute lookup."""


def _script(steps: tuple[Step, ...]) -> Callable[[Arena], None]:
    def run(arena: Arena) -> None:
        people: dict[str, Any] = {}
        for step in steps:
            _DISPATCH[step.op](arena, people, step)
    return run


# -- the entry point --------------------------------------------------------------

def compile_attack(doc: object) -> Compiled | Rejected:
    """Check a proposal against the grammar and build the `Attack` it describes."""
    try:
        return _compile(doc)
    except _Reject as problem:
        return Rejected(str(problem))


def _compile(doc: object) -> Compiled:
    fields = _record(doc, "proposal", required=grammar.TOP_LEVEL - {"reconciles"}, optional={"reconciles"})
    refs = _Refs(people={}, entities=set(), marks=set(), clock=EPOCH)
    attack_id = _check(grammar.ID, fields["id"], "id", refs)
    for key in ("title", "targets"):
        text = fields[key]
        if not isinstance(text, str) or not (1 <= len(text) <= 240) or not text.isprintable():
            raise _Reject(f"{key}: expected printable text, 1 to 240 characters")
    stated = _check(grammar.OneOf(grammar.EXPECTATIONS, "survives or fails"), fields["expectation"], "expectation", refs)
    reconciles = fields.get("reconciles", True)
    if not isinstance(reconciles, bool):
        raise _Reject("reconciles: expected true or false")

    raw_steps = fields["steps"]
    if not isinstance(raw_steps, list) or not (1 <= len(raw_steps) <= grammar.MAX_STEPS):
        raise _Reject(f"steps: expected a list of 1 to {grammar.MAX_STEPS} operations")
    steps = tuple(_step(index, raw, refs) for index, raw in enumerate(raw_steps))
    if not refs.entities:
        raise _Reject("steps: no fail step, so there is no payment for the agent to act on")

    raw_evidence = fields["evidence"]
    if not isinstance(raw_evidence, list) or len(raw_evidence) > grammar.MAX_EVIDENCE:
        raise _Reject(f"evidence: expected a list of at most {grammar.MAX_EVIDENCE} predicates")
    evidence = tuple(_evidence(index, raw, refs) for index, raw in enumerate(raw_evidence))

    attack = Attack(
        id=attack_id,
        title=fields["title"],
        targets=fields["targets"],
        source=f"windtunnel/adversary/grammar.py v{grammar.GRAMMAR_VERSION}; stated expectation {stated}",
        # §10, 2026-09-22: every proposal is scored against the agent's own
        # standing claim, so a finding means the agent broke.
        expectation=Expectation.SURVIVES,
        evidence=evidence,
        run=_script(steps),
        arena=None if reconciles else (lambda: Arena(reconciles=False)),
    )
    return Compiled(attack=attack, steps=steps, stated_expectation=stated)
