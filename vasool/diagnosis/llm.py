"""The LLM classifier: a prompt in, an inert verdict out.

docs/VASOOL-design-spec.md §4.5 asks for a second classifier that runs beside
the deterministic one so that the two can be measured against each other. This
module is that classifier's whole surface, and it is deliberately two pure
functions: `build_prompt` turns a FailureEvent into a string, `parse_verdict`
turns a string into an `LLMVerdict`. There is no client here, no key, no
network, no clock. The provider lives in `tools/gemini.py`, the measurement in
`windtunnel/shadow.py`, and the wiring in `tools/shadow.py`.

**One deliberate departure from §4.5.** The spec says this module emits a
`Proposal`, and `vasool/diagnosis/proposal.py`'s docstring says the same. It
does not. A `Proposal` is precisely the object `actions/executor.py` consumes,
so a constructor from a model response would leave CLAUDE.md invariant 1 —
*the LLM never calls a tool* — resting on nobody calling the wrong function
in a later session. `LLMVerdict` is a separate type with no conversion to a
`Proposal` anywhere in the codebase, which turns the invariant into a property
of the import graph. `tests/test_shadow_boundary.py` walks that graph in both
directions and fails if either becomes reachable from the other.

**The parser is the boundary, and it is strict on purpose.** Both verdict
fields are parsed through the closed enums of `vasool/diagnosis/taxonomy.py`,
whose module docstring anticipated exactly this: "an invented class or an
invented action fails at the boundary instead of reaching the policy plane."
A model that decides the correct response is a refund cannot say so — there is
no `REFUND` member, and adding one is a taxonomy change that belongs in
`docs/taxonomy.md` first.

**What the prompt deliberately does not contain.** docs/taxonomy.md §8 is
explicit that every row of §4 is a dictionary lookup rather than a judgment
call. Pasting that dictionary into the prompt would measure whether a model
can copy a table, which is a question nobody needs answered. So the
instructions carry §2 — what the five classes *mean* — and never a
reason-to-class mapping. `tests/test_llm_classifier.py` asserts that no
canonical reason string appears in the template.

**And what it does not contain about the customer.** The registered arm is
fields-only: four error strings, and nothing about who was being charged, for
how much, or on whose behalf. That was chosen as scope, but it earns a second
justification from where this runs — the comparison uses a free API tier whose
published terms say the data is used to improve the provider's products. A
field that never enters the prompt is a field that never leaves the machine.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from vasool.events.schemas import FailureEvent

MAX_RATIONALE_CHARS = 400
"""Where a rationale stops being a rationale and starts being an essay.

Truncation rather than rejection: over-long prose is a formatting failure, and
throwing away a verdict whose class was correct would report a model error
that did not happen.
"""

VERDICT_KEYS: frozenset[str] = frozenset({"failure_class", "intervention", "rationale"})
"""Exactly the keys a response may carry. Not a minimum — an unrequested field
is a response of the wrong shape, and a parser that shrugs at extra keys is
how a `tool_call` key eventually arrives unnoticed."""


class VerdictRejected(ValueError):
    """The model said something that is not a verdict.

    Distinct from "the model said the wrong class", and counted separately by
    windtunnel/shadow.py: a rejection and a misclassification are different
    failures with different remedies, and collapsing them would hide which one
    happened.
    """


@dataclass(frozen=True, slots=True)
class LLMVerdict:
    """What the LLM concluded. Inert by construction.

    Deliberately not a Proposal, and deliberately carrying no method that
    could become one — see this module's docstring. It describes a class and
    an action; nothing in the codebase can turn it into an action taken.
    """

    failure_class: FailureClass
    intervention: InterventionType
    rationale: str


INSTRUCTIONS = """\
You are classifying a failed payment for an Indian payments merchant.

You are given four fields exactly as the payment gateway reported them, and
nothing else. Decide which one of five failure classes the failure belongs
to, and which one of five interventions the merchant should take.

FAILURE CLASSES — choose exactly one:

  TRANSIENT        A rail, bank or gateway hiccup. Nothing is wrong with the
                   customer or with their payment instrument. Re-presenting
                   the same instrument later may well succeed, and the
                   customer probably never noticed anything.
  LIQUIDITY        The instrument is fine and the customer intends to pay;
                   the money is not in the account right now. The problem is
                   timing rather than the instrument.
  INSTRUMENT_DEAD  The payment method cannot succeed again in its current
                   state, no matter how many times it is presented. The
                   customer has to supply a different instrument, or change
                   something at their bank before this one can work.
  CUSTOMER_ACTION  A human has to do something: complete an authentication
                   step, correct a mistake they made, or finish something
                   they abandoned. Re-presenting the same request without
                   them cannot help.
  RISK_BLOCK       A fraud or risk engine declined this transaction. No
                   automated response of any kind is appropriate — neither
                   re-presenting the payment nor messaging the customer.

INTERVENTIONS — choose exactly one:

  SILENT_RETRY     Re-present the same instrument after a short delay. The
                   customer is not contacted.
  TIMED_RETRY      Re-present the same instrument at a time chosen so that
                   funds are more likely to be present.
  REATTEMPT_LINK   Send the customer a link to complete the same payment
                   themselves.
  REAUTH_LINK      Send the customer a link to supply or re-authorise a
                   different payment instrument.
  HUMAN_QUEUE      Hand the case to a human operator. Nothing automated
                   happens and the customer is not contacted.

Answer with a single JSON object and nothing else:

  {"failure_class": "...", "intervention": "...", "rationale": "..."}

Use only the exact strings listed above. Do not invent a class or an
intervention, do not add any other field, and keep the rationale to one
sentence.
"""
"""The instruction template. Carries the five meanings and never the mapping —
see this module's docstring on why, and tests/test_llm_classifier.py for the
assertion that keeps it that way."""


RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "failure_class": {
            "type": "string",
            "enum": [member.value for member in FailureClass],
        },
        "intervention": {
            "type": "string",
            "enum": [member.value for member in InterventionType],
        },
        "rationale": {"type": "string"},
    },
    "required": sorted(VERDICT_KEYS),
}
"""A plain JSON Schema, held here rather than in the provider client so that a
second provider gets the identical contract.

It is belt and braces, not a replacement for `parse_verdict`: a provider that
enforces the schema server-side still cannot be trusted to have done so, and
the enum members are generated from the taxonomy so the two cannot drift.
"""


def build_prompt(event: FailureEvent) -> str:
    """The prompt for one failed payment. A pure function of four fields.

    Everything else on the event — who, how much, which merchant, which
    payment — is deliberately absent. One consequence is worth knowing before
    reading the comparison: because identity is excluded, every episode
    sharing a `(reason, source, code, step)` tuple produces a byte-identical
    prompt, and the registered universe contains exactly twelve such tuples
    across ~8,900 episodes. The corpus in windtunnel/shadow.py is therefore
    the whole input space rather than a sample of it.
    """
    return (
        f"{INSTRUCTIONS}\n"
        "THE FAILURE\n"
        f"  error_reason: {event.error_reason}\n"
        f"  error_source: {event.error_source}\n"
        f"  error_code:   {event.error_code}\n"
        f"  error_step:   {event.error_step}\n"
    )


def _strip_fence(text: str) -> str:
    """Unwrap ```json fences.

    Models emit them even when asked not to. That is a formatting quirk rather
    than a malformed verdict, and rejecting it would report a model failure
    that did not happen.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:]
    if body.lower().startswith("json"):
        body = body[4:]
    return body.rsplit("```", 1)[0].strip() if "```" in body else body.strip()


def _member(enum_type, raw: object, field: str):
    """Resolve one closed-enum field, or reject the whole verdict.

    Case and surrounding whitespace are folded first, for the same reason
    `taxonomy.normalise` folds them on an `error_reason`: `risk_block` is a
    model formatting a member of a closed vocabulary, not a model inventing a
    class. What is never done is fuzzy matching — `RETRY` does not become
    `SILENT_RETRY`, because resolving a near miss to its closest neighbour
    would be this parser inventing a verdict on the model's behalf.
    """
    if not isinstance(raw, str):
        raise VerdictRejected(f"{field} must be a string, got {type(raw).__name__}")
    try:
        return enum_type(raw.strip().upper())
    except ValueError:
        raise VerdictRejected(
            f"{field}={raw!r} is not one of "
            f"{sorted(member.value for member in enum_type)}"
        ) from None


def parse_verdict(text: str) -> LLMVerdict:
    """Turn raw model output into an LLMVerdict, or raise.

    This is the boundary CLAUDE.md invariant 1 is enforced at. Everything that
    is not exactly a verdict over the closed vocabularies is rejected, and the
    caller counts the rejection rather than retrying — a model that cannot
    produce the shape is a finding, not an inconvenience.
    """
    payload = _strip_fence(text)
    if not payload:
        raise VerdictRejected("empty response")

    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise VerdictRejected(f"not JSON: {exc}") from None

    if not isinstance(document, dict):
        raise VerdictRejected(f"expected an object, got {type(document).__name__}")

    keys = set(document)
    if keys != set(VERDICT_KEYS):
        missing = sorted(set(VERDICT_KEYS) - keys)
        extra = sorted(keys - set(VERDICT_KEYS))
        raise VerdictRejected(f"keys missing={missing} unexpected={extra}")

    rationale = document["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise VerdictRejected("rationale is empty — a verdict has to say why")

    return LLMVerdict(
        failure_class=_member(FailureClass, document["failure_class"], "failure_class"),
        intervention=_member(InterventionType, document["intervention"], "intervention"),
        rationale=rationale.strip()[:MAX_RATIONALE_CHARS],
    )
