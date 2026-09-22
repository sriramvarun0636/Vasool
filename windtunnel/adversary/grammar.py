"""The grammar a proposed attack is written in. Closed, like everything else here.

docs/EVALUATION.md §10, 2026-09-22, registers the adversary generator of the
design doc's §2.6. The observation that makes it tractable is that an attack in
this repository was never free-form code: it is a sequence of operations on the
arena, a set of evidence predicates from `criterion.py`, and an expectation.
That is a closed grammar — the shape a model can be constrained to emit, and a
compiler can check completely.

This module is the vocabulary and nothing else: which operations exist, which
fields each takes, and what values each field may hold. It executes nothing and
imports no model client. `compile.py` reads it to check a proposal;
`propose.py` renders it into the prompt; neither can extend it, and a field
that is not declared here is rejected rather than ignored.

**What is deliberately absent.**

- `Arena.deliver`, which takes a raw webhook body. Every failure enters from a
  payload on disk (`windtunnel/payloads.py`) with only identity stamped on it,
  and "no attack can author an error string" is a rule an adversary does not
  get an exemption from. None of the registered attacks call it either.
- `Arena.set_merchant` and the `**changes` of `mandate_event`: open-ended
  keyword arguments. A value this module cannot type is a value the compiler
  cannot check. `mandate_event` is here with its trigger alone.
- Anything that reads the scene. An attack describes what the world did; what
  the system made of it is `criterion.judge`'s to say, and the grammar has no
  way to express a condition, a loop or a return value.

**Every reference is declared before it is used.** A person is bound by a
`person` step and referred to by that name; an entity id is introduced by the
`fail` that opens it; a mark exists once a `mark` step has set it. Evidence may
only name entities and marks the steps declared — evidence about an entity the
attack never touched is vacuous, and vacuous evidence would let a proposal
"survive" for no reason at all.
"""
from __future__ import annotations

from dataclasses import dataclass

from vasool.diagnosis.proposal import InterventionType, ProposalRole, template_ids
from vasool.ledger.receipts import Outcome
from vasool.mandate.machine import Trigger
from vasool.mandate.record import MandateCategory, MandateRail
from vasool.mandate.states import MandateState
from vasool.policy.registry import GUARD_CHAIN
from windtunnel import payloads

GRAMMAR_VERSION = 1
"""Bumped whenever the vocabulary changes. A cassette recorded against one
version is not replayed against another — the prompt it answered is gone."""

MAX_STEPS = 40
MAX_PEOPLE = 8
MAX_EVIDENCE = 12
MAX_DAY = 30
"""`Arena.ist` builds its instants in the epoch's own month — September, IST —
so a thirty-first day is not a date at all, and an attack that named one would
crash the arena rather than be judged by it. Thirty days covers every ladder
the policy machine can climb from the first."""


# -- field kinds --------------------------------------------------------------
# Each is a small frozen value the compiler dispatches on. None of them carries
# code: a kind says what a value must look like, and compile.py checks it.

@dataclass(frozen=True, slots=True)
class Pattern:
    """A string matching `regex` in full."""
    regex: str
    meaning: str


@dataclass(frozen=True, slots=True)
class Integer:
    lo: int
    hi: int


@dataclass(frozen=True, slots=True)
class Boolean:
    pass


@dataclass(frozen=True, slots=True)
class OneOf:
    """A string drawn from a closed set."""
    values: frozenset[str]
    meaning: str


@dataclass(frozen=True, slots=True)
class Instant:
    """`{"day": d, "hour": h, "minute": m}` — an IST wall-clock time on the
    arena's calendar, exactly what `Arena.ist` builds."""


@dataclass(frozen=True, slots=True)
class Duration:
    """`{"days": d, "hours": h, "minutes": m}`, any subset, all non-negative."""
    positive: bool = False


@dataclass(frozen=True, slots=True)
class Offset:
    """A UTC offset in minutes, a multiple of fifteen — a customer's own zone."""


@dataclass(frozen=True, slots=True)
class Mandate:
    """`{"rail", "category", "state", "valid_days"}` — a mandate record named
    exactly, for an attack about a particular rail, category or state."""


@dataclass(frozen=True, slots=True)
class Templates:
    """A list of DLT template ids, each registered, none repeated."""


@dataclass(frozen=True, slots=True)
class PersonRef:
    """The name a `person` step bound."""


@dataclass(frozen=True, slots=True)
class EntityRef:
    """An entity id a `fail` step has already opened."""


@dataclass(frozen=True, slots=True)
class MarkRef:
    """A label a `mark` step has already set."""


@dataclass(frozen=True, slots=True)
class Field:
    kind: object
    required: bool = True


NAME = Pattern(r"[a-z][a-z0-9_]{0,23}", "a lowercase identifier")
ENTITY_ID = Pattern(r"pay_[a-z0-9_]{1,40}", "a payment id beginning pay_")
EVENT_ID = Pattern(r"evt_[a-z0-9_]{1,40}", "a webhook event id beginning evt_")
MARK = Pattern(r"[a-z][a-z0-9_]{0,39}", "a lowercase label")
EMAIL = Pattern(r"[a-z0-9_]{1,24}(\.[a-z0-9_]{1,24})?@example\.invalid", "an address at example.invalid")
CONTACT = Pattern(r"\+91[6-9][0-9]{9}", "an Indian mobile number")

CARD_REASONS = frozenset(reason for reason, _source in payloads.available_pairs())
UPI_REASONS = payloads.upi_reasons()
REASON = OneOf(CARD_REASONS | UPI_REASONS, "a failure reason with a payload on disk")
SOURCE = OneOf(frozenset(source for _reason, source in payloads.available_pairs()),
               "an error source with a payload on disk")

AMOUNT = Integer(100, 100_000_000)
"""One rupee to ten lakh, in paise. Wide enough for both AFA thresholds (₹15,000
and ₹1,00,000) on either side, which is where amount matters to the chain."""

COUNT = Integer(0, 50)


def _names(enum_type) -> frozenset[str]:
    return frozenset(member.name for member in enum_type)


def _values(enum_type) -> frozenset[str]:
    return frozenset(member.value for member in enum_type)


# -- operations ---------------------------------------------------------------
# One entry per arena method a proposal may call, with the arguments it may pass.
# compile.py's dispatch table has exactly these keys and no others.

OPS: dict[str, dict[str, Field]] = {
    "person": {
        "bind": Field(NAME),
        "human_id": Field(NAME),
        "contact": Field(CONTACT, required=False),
        "same_contact_as": Field(PersonRef(), required=False),
        "email": Field(EMAIL, required=False),
        "zone_offset_minutes": Field(Offset(), required=False),
        "dnd_listed": Field(Boolean(), required=False),
        "is_mandate": Field(Boolean(), required=False),
        "mandate": Field(Mandate(), required=False),
        "consent": Field(OneOf(frozenset({"default", "none"}), "consent on file or not"), required=False),
    },
    "advance_to": {"at": Field(Instant())},
    "advance_by": {"by": Field(Duration(positive=True))},
    "jump_to": {"at": Field(Instant())},
    "mark": {"label": Field(MARK)},
    "fail": {
        "person": Field(PersonRef()),
        "reason": Field(REASON),
        "entity_id": Field(ENTITY_ID),
        "source": Field(SOURCE, required=False),
        "amount_paise": Field(AMOUNT, required=False),
        "event_id": Field(EVENT_ID, required=False),
        "upi": Field(Boolean(), required=False),
        "occurred_at": Field(Instant(), required=False),
    },
    "fail_last_retry": {"entity_id": Field(EntityRef())},
    "pay_link": {"entity_id": Field(EntityRef()), "event_id": Field(EVENT_ID, required=False)},
    "capture_last_retry": {"entity_id": Field(EntityRef())},
    "pay_out_of_band": {"entity_id": Field(EntityRef())},
    "lose_next_debit_response": {"entity_id": Field(EntityRef())},
    "withdraw_consent": {"person": Field(PersonRef())},
    "mandate_event": {"person": Field(PersonRef()), "trigger": Field(OneOf(_names(Trigger), "a mandate trigger"))},
    "promise": {"entity_id": Field(EntityRef()), "day": Field(Integer(1, MAX_DAY))},
    "poison_dedupe_oracle": {},
    "set_registered_templates": {"templates": Field(Templates())},
}

MANDATE_FIELDS: dict[str, Field] = {
    "rail": Field(OneOf(_names(MandateRail), "a mandate rail")),
    "category": Field(OneOf(_names(MandateCategory), "a mandate category")),
    "state": Field(OneOf(_names(MandateState), "a mandate state")),
    "valid_days": Field(Integer(-365, 3650)),
    "mandate_id": Field(Pattern(r"[a-z][a-z0-9_]{0,40}", "a lowercase mandate id"), required=False),
    "token_id": Field(Pattern(r"token_[a-z0-9_]{1,40}", "a token id beginning token_"), required=False),
    "razorpay_customer_id": Field(Pattern(r"cust_[a-z0-9_]{1,40}", "a customer id beginning cust_"), required=False),
    "revocable_by_payer": Field(Boolean(), required=False),
}
"""A UPI Autopay debit is made against a token, so a mandate on that rail that
should be debitable names one — A26 is written exactly that way."""

REGISTERED_TEMPLATES = frozenset(template_ids())


# -- evidence -----------------------------------------------------------------
# One entry per `criterion.py` evidence type, keyed by its class name, with the
# fields its dataclass declares. Every one is a further requirement on the
# ledger; none can relax the three universal clauses.

_OUTCOME = OneOf(_names(Outcome), "a receipt outcome")
_GUARD = OneOf(frozenset(guard.name for guard in GUARD_CHAIN), "a guard in the chain")
_INTERVENTION = OneOf(_values(InterventionType), "an intervention")
_ROLE = OneOf(_values(ProposalRole), "a proposal role")

EVIDENCE: dict[str, dict[str, Field]] = {
    "ExecutedCount": {
        "entity_id": Field(EntityRef()),
        "count": Field(COUNT),
        "intervention": Field(_INTERVENTION, required=False),
        "is_retry": Field(Boolean(), required=False),
        "is_contact": Field(Boolean(), required=False),
        "role": Field(_ROLE, required=False),
    },
    "RailDebitsAtMost": {"entity_id": Field(EntityRef()), "count": Field(COUNT)},
    "ExecutedAtMost": {
        "entity_id": Field(EntityRef()),
        "count": Field(COUNT),
        "is_retry": Field(Boolean(), required=False),
        "is_contact": Field(Boolean(), required=False),
    },
    "ReceiptWithOutcome": {
        "entity_id": Field(EntityRef()),
        "outcome": Field(_OUTCOME),
        "guard": Field(_GUARD, required=False),
    },
    "ReceiptCount": {"entity_id": Field(EntityRef()), "outcome": Field(_OUTCOME), "count": Field(COUNT)},
    "ReceiptNoLaterThan": {
        "entity_id": Field(EntityRef()),
        "outcome": Field(_OUTCOME),
        "mark": Field(MarkRef()),
        "within": Field(Duration()),
    },
    "NoExecutionOnEntityAfter": {
        "entity_id": Field(EntityRef()),
        "mark": Field(MarkRef()),
        "is_retry": Field(Boolean(), required=False),
    },
    "NoDebitBeforeNoticeMatures": {"entity_id": Field(EntityRef()), "lead": Field(Duration())},
    "NoContactOutsideCustomerWindow": {},
    "NoContactToDndListed": {},
    "ContactsPerHumanWithin": {"cap": Field(Integer(1, 20)), "window": Field(Duration(positive=True))},
    "NoRetryExecutedBetweenIST": {"open_hour": Field(Integer(0, 23)), "close_hour": Field(Integer(0, 24))},
}

EXPECTATIONS = frozenset({"survives", "fails"})
"""What the proposer *says* will happen. Recorded, and not used for scoring:
§10's 2026-09-22 row scores every proposal against the agent's own standing
claim that it survives, so that a finding means the agent broke."""

TOP_LEVEL = frozenset({"id", "title", "targets", "expectation", "reconciles", "steps", "evidence"})
ID = Pattern(r"[GH][0-9]{3}", "G and three digits for a generated attack, H for a hand-written one")


def describe() -> dict:
    """The grammar as data, for the prompt. Nothing in here is executable."""

    def render(kind: object) -> object:
        if isinstance(kind, Pattern):
            return {"string matching": kind.regex, "meaning": kind.meaning}
        if isinstance(kind, Integer):
            return {"integer from": kind.lo, "to": kind.hi}
        if isinstance(kind, Boolean):
            return "true or false"
        if isinstance(kind, OneOf):
            return {"one of": sorted(kind.values)}
        if isinstance(kind, Instant):
            return {"day": f"1..{MAX_DAY}", "hour": "0..23", "minute": "0..59"}
        if isinstance(kind, Duration):
            return {"any of": ["days", "hours", "minutes"], "positive": kind.positive}
        if isinstance(kind, Offset):
            return "UTC offset in minutes, a multiple of 15, from -720 to 840"
        if isinstance(kind, Mandate):
            return {name: {"required": field.required, "value": render(field.kind)}
                    for name, field in MANDATE_FIELDS.items()}
        if isinstance(kind, Templates):
            return {"list, each one of": sorted(REGISTERED_TEMPLATES)}
        if isinstance(kind, PersonRef):
            return "the bind name of an earlier person step"
        if isinstance(kind, EntityRef):
            return "an entity_id an earlier fail step opened"
        if isinstance(kind, MarkRef):
            return "a label an earlier mark step set"
        raise TypeError(f"no rendering for {kind!r}")

    def table(entries: dict[str, dict[str, Field]]) -> dict:
        return {
            name: {field: {"required": spec.required, "value": render(spec.kind)} for field, spec in fields.items()}
            for name, fields in entries.items()
        }

    return {
        "grammar_version": GRAMMAR_VERSION,
        "limits": {"steps": MAX_STEPS, "people": MAX_PEOPLE, "evidence": MAX_EVIDENCE},
        "top_level": sorted(TOP_LEVEL),
        "id": render(ID),
        "expectation": sorted(EXPECTATIONS),
        "reconciles": "true or false: whether the deployment has wired a settlement lookup (default true)",
        "operations": table(OPS),
        "evidence": table(EVIDENCE),
        "reasons": {"card": sorted(CARD_REASONS), "upi": sorted(UPI_REASONS)},
    }
