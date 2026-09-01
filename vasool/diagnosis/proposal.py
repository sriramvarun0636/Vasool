"""The Proposal: what the diagnosis plane emits, and what the policy plane rules
on. Inert by construction — it describes an action, it cannot perform one.

This is the object architectural invariant 1 is about. When the LLM classifier lands
in Session 7 it produces one of these and nothing else: a closed enum it cannot
invent a member of, a channel, a time, and a rationale. Thirteen guards stand
between it and any executor.

**One Diagnosis can describe two actions.** docs/taxonomy.md §4 gives LIQUIDITY
"TIMED_RETRY ×3 + soft nudge" — a re-presentation of an instrument, which
touches nobody, and a message to a human, which is subject to the contact
window, the DND registry, the DLT template rules and the frequency cap. Carrying
both on one Proposal would make the nudge inherit the retry's verdict and never
meet any of those guards. So the fan-out below emits one Proposal per action and
lets each be gated on its own terms: the retry can execute while the nudge
defers to 08:00, and the nudge can be blocked while the retry proceeds.

The nudge is *not* a sixth InterventionType. §4 models it as a modifier on
TIMED_RETRY rather than an action in its own right, and taxonomy.py's first rule
is that adding an enum member is a taxonomy change that belongs in the document
before it belongs in code. So the sibling proposals share an intervention and
differ by `role` — which is also what keeps a nudge from spending one of the
four attempts Razorpay allows before it halts a subscription.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, model_validator

from vasool.diagnosis.rules import Diagnosis
from vasool.diagnosis.taxonomy import (
    CONTACT_INTERVENTIONS,
    RETRY_INTERVENTIONS,
    FailureClass,
    InterventionType,
)
from vasool.events.schemas import FailureEvent


class Channel(StrEnum):
    """How a message reaches the customer. Closed."""

    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"


class MessageCategory(StrEnum):
    """TRAI's distinction, which decides whether the DND registry applies.

    # VERIFY: whether a payment-recovery message is transactional, service or
    # promotional under TCCCPR is genuinely unsettled, and it is the single fact
    # that decides whether DNDGuard fires at all. We categorise recovery
    # messages as TRANSACTIONAL — they concern a payment the customer already
    # initiated — but a telecom operator or the merchant's DLT registration
    # could categorise them otherwise, and if they do, DNDGuard becomes
    # load-bearing overnight. Not a detail to guess about in production.
    """

    TRANSACTIONAL = "TRANSACTIONAL"
    SERVICE = "SERVICE"
    PROMOTIONAL = "PROMOTIONAL"


class ProposalRole(StrEnum):
    """Which of a Diagnosis's actions this proposal is. Closed."""

    PRIMARY = "PRIMARY"
    """The intervention §4 names for the row."""

    NUDGE = "NUDGE"
    """§4's "+ soft nudge" — the message accompanying a timed retry. Shares the
    primary's intervention; is a contact; does not spend attempt budget."""

    PRE_DEBIT_NOTICE = "PRE_DEBIT_NOTICE"
    """The 24h notice a mandate debit owes the customer. Created by the state
    machine from a guard's Obligation, and gated in its own right — a notice is
    a customer contact, so one generated at 03:00 does not get to skip the
    contact window."""


DEFAULT_CHANNEL = Channel.SMS
"""# VERIFY: channel selection is a comms-plane decision we have not built.
SMS is the floor — it reaches a customer who has no WhatsApp and no email on
file. Choosing per customer belongs with actions/comms.py."""


_TEMPLATES: dict[str, str] = {
    "NUDGE": "VASOOL_LIQUIDITY_NUDGE",
    "PRE_DEBIT_NOTICE": "VASOOL_PRE_DEBIT_NOTICE",
    "REATTEMPT_LINK": "VASOOL_REATTEMPT",
    "REAUTH_LINK": "VASOOL_REAUTH",
    "REAUTH_LINK_EXPLAIN": "VASOOL_REAUTH_EXPLAIN",
}
"""# VERIFY: placeholders. A real DLT template id is issued to the merchant
through their own TRAI/DLT registration and is not ours to invent — these exist
so DLTTemplateGuard has something to check and so the wiring is exercised. The
_EXPLAIN variant is separate because §5's card_disabled_for_online_payments
argument is that naming the specific cause is what makes the message work, and
that is a different registered template, not a different string interpolated
into the same one."""


def _template_for(role: ProposalRole, intervention: InterventionType, explain: bool) -> str | None:
    if role is not ProposalRole.PRIMARY:
        return _TEMPLATES[role.value]
    if intervention not in CONTACT_INTERVENTIONS:
        return None
    key = f"{intervention.value}_EXPLAIN" if explain else intervention.value
    return _TEMPLATES.get(key, _TEMPLATES[intervention.value])


def _derive_id(entity_id: str, intervention: InterventionType, attempt: int, role: str) -> str:
    """A deterministic proposal id.

    Not uuid4 and not hash(): architectural invariant 5 requires the same seed to
    produce a byte-identical ledger, and Python's hash() is salted per process,
    so it would pass an equality check inside one run and break replay across
    two.

    Keyed on the *entity*, never the event. Razorpay delivers every webhook at
    least twice (docs/VERIFIED.md), and adversary attack A02 replays one with a
    fresh event id — under the spec's (event_id, intervention) key that reads as
    a brand-new action. Keyed on the payment, it is recognisably the same one.
    """
    basis = f"{entity_id}|{intervention.value}|{attempt}|{role}"
    return "prop_" + hashlib.sha256(basis.encode()).hexdigest()[:16]


class Proposal(BaseModel):
    """An inert description of one action. Immutable."""

    proposal_id: str
    role: ProposalRole

    event_id: str
    entity_id: str
    customer_id: str
    merchant_id: str
    amount_paise: int

    failure_class: FailureClass
    intervention: InterventionType
    attempt: int
    execute_at: datetime

    channel: Channel | None = None
    message_category: MessageCategory | None = None
    template_id: str | None = None
    """None is a legitimate state, not an omission — it is what DLTTemplateGuard
    blocks on. An unregistered template must be constructible or the guard
    cannot be tested."""

    explain: bool = False

    rationale: str
    confidence: float = 1.0
    proposed_by: Literal["rules", "llm"] = "rules"

    supersedes: str | None = None
    """The proposal this one replaces after a deferral. Proposals are never
    mutated — re-entry creates a successor — so this is what preserves lineage
    through the ledger."""

    sibling_id: str | None = None
    """The other half of a fan-out."""

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _check_contact_shape(self) -> Self:
        if self.channel is not None and self.message_category is None:
            raise ValueError(
                "a proposal with a channel needs a message_category — DNDGuard "
                "keys on it, and a missing one would silently skip the scrub"
            )
        if self.execute_at.tzinfo is None:
            raise ValueError("execute_at must be timezone-aware")
        return self

    @property
    def is_contact(self) -> bool:
        """Whether this action reaches a human.

        Deliberately over-inclusive: a contact intervention counts even with no
        channel set. The alternative fails open — a REAUTH_LINK built without a
        channel would look like a silent retry to every comms guard and sail
        past the contact window.
        """
        return (
            self.channel is not None
            or self.intervention in CONTACT_INTERVENTIONS
            or self.role in (ProposalRole.NUDGE, ProposalRole.PRE_DEBIT_NOTICE)
        )

    @property
    def is_retry(self) -> bool:
        """Whether this re-presents the instrument, and so spends one of the
        four attempts before Razorpay halts a subscription.

        Keyed on the role as well as the intervention, because a NUDGE shares
        its sibling's TIMED_RETRY without being a re-presentation of anything.
        """
        return self.intervention in RETRY_INTERVENTIONS and self.role is ProposalRole.PRIMARY

    @property
    def idempotency_key(self) -> str:
        """One execution per (payment, intervention, attempt, role).

        The spec's key is (event_id, intervention), which is wrong twice over:
        it collides across the three silent retries gateway_technical_error gets
        for a single event, so retry 2 would be refused as a duplicate of retry
        1; and it misses A02 entirely. The role is in the key because a nudge
        and its retry share everything else.
        """
        return f"{self.entity_id}:{self.intervention.value}:{self.attempt}:{self.role.value}"


def _build(
    *,
    role: ProposalRole,
    diagnosis: Diagnosis,
    event: FailureEvent,
    intervention: InterventionType,
    execute_at: datetime,
    is_contact: bool,
) -> Proposal:
    template = _template_for(role, intervention, diagnosis.explain)
    return Proposal(
        proposal_id=_derive_id(event.entity_id, intervention, diagnosis.attempt, role.value),
        role=role,
        event_id=event.event_id,
        entity_id=event.entity_id,
        customer_id=event.customer_id,
        merchant_id=event.merchant_id,
        amount_paise=event.amount_paise,
        failure_class=diagnosis.failure_class,
        intervention=intervention,
        attempt=diagnosis.attempt,
        execute_at=execute_at,
        channel=DEFAULT_CHANNEL if is_contact else None,
        message_category=MessageCategory.TRANSACTIONAL if is_contact else None,
        template_id=template,
        explain=diagnosis.explain,
        rationale=diagnosis.rationale,
    )


def proposals_from(
    diagnosis: Diagnosis, event: FailureEvent, *, now: datetime
) -> tuple[Proposal, ...]:
    """Turn a Diagnosis into the one or two actions it describes.

    Returns empty when the diagnosis names no action at all — the retry budget
    is spent and §4's row defines no escalation. That is the policy plane's
    EXHAUSTED terminal, and it must not become a proposal for guards to rule on;
    there is nothing to rule on.

    `now` is passed rather than taken from a clock because a nudge fires
    immediately while its retry waits for payday, and the Diagnosis carries only
    the retry's time. A nudge that arrives with the retry is useless — the
    entire point is to give the customer 48 hours to move money before we
    re-present.
    """
    if diagnosis.intervention is None or diagnosis.execute_at is None:
        return ()

    primary = _build(
        role=ProposalRole.PRIMARY,
        diagnosis=diagnosis,
        event=event,
        intervention=diagnosis.intervention,
        execute_at=diagnosis.execute_at,
        is_contact=diagnosis.intervention in CONTACT_INTERVENTIONS,
    )
    if not diagnosis.soft_nudge:
        return (primary,)

    nudge = _build(
        role=ProposalRole.NUDGE,
        diagnosis=diagnosis,
        event=event,
        intervention=diagnosis.intervention,
        execute_at=now,
        is_contact=True,
    )
    return (
        primary.model_copy(update={"sibling_id": nudge.proposal_id}),
        nudge.model_copy(update={"sibling_id": primary.proposal_id}),
    )


def notice_proposal_from(debit: Proposal, *, execute_at: datetime) -> Proposal:
    """The pre-debit notice a mandate debit owes the customer.

    Built here rather than in the state machine so that every Proposal in the
    system is constructed by this module. The machine decides *that* a notice is
    owed — from an Obligation a guard returned — and this decides what one looks
    like. It is a customer contact and is gated like any other.
    """
    return Proposal(
        proposal_id=_derive_id(
            debit.entity_id,
            debit.intervention,
            debit.attempt,
            ProposalRole.PRE_DEBIT_NOTICE.value,
        ),
        role=ProposalRole.PRE_DEBIT_NOTICE,
        event_id=debit.event_id,
        entity_id=debit.entity_id,
        customer_id=debit.customer_id,
        merchant_id=debit.merchant_id,
        amount_paise=debit.amount_paise,
        failure_class=debit.failure_class,
        intervention=debit.intervention,
        attempt=debit.attempt,
        execute_at=execute_at,
        channel=DEFAULT_CHANNEL,
        message_category=MessageCategory.TRANSACTIONAL,
        template_id=_TEMPLATES["PRE_DEBIT_NOTICE"],
        rationale=(
            "RBI e-mandate: the customer must be notified before a recurring "
            "debit. The notice is itself a contact and is gated as one."
        ),
        sibling_id=debit.proposal_id,
    )


def template_ids() -> frozenset[str]:
    """Every DLT template this system can emit.

    Exposed so a merchant's registered-template set can be wired from one place
    rather than transcribed. DLTTemplateGuard checks the merchant's registration,
    not this — a template we can emit and the merchant has not registered is
    precisely what the guard exists to catch.
    """
    return frozenset(_TEMPLATES.values())
