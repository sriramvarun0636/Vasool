"""MandateRecord: what the policy plane knows about the mandate behind a debit.

`PolicyFacts.is_mandate` reads this. It used to be a boolean the simulator set
honestly and production had nothing to set from; the record is what production
would read from its mandate store, and what the simulator now builds instead.

**Frozen, and replaced rather than mutated** — the machine returns a new
record for every transition, the same discipline as Episode, so every version
of a mandate is a value a ledger could hold.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from vasool.mandate.states import TERMINAL, MandateState


class MandateRail(StrEnum):
    """Which network carries the mandate's debits. Closed.

    RBI's Framework governs both (§2: "cards / PPI / UPI"); NPCI's operating
    circulars govern only UPI. So the rules that differ by rail are exactly the
    NPCI ones — peak hours, the attempt limit, pause.
    """

    UPI_AUTOPAY = "UPI_AUTOPAY"
    CARD = "CARD"
    """A card e-mandate.

    # VERIFY: e-NACH is a third rail with its own rules, none of them gathered,
    # and it is not modelled. The simulator's netbanking episodes on mandate
    # customers are held to the card rules, as they were before this record
    # existed (docs/EVALUATION.md §10, 2026-09-15).
    """


class MandateCategory(StrEnum):
    """What the debits pay for, as far as the AFA limit cares. Closed.

    §8(b) of the Framework lifts the limit for three purposes and no others.
    NPCI implements the same lift by merchant category code (OC-151A's
    Annexure A lists eight), so for UPI the category is the merchant's
    declaration and its MCC is its acquirer's to verify — the same division of
    knowledge as a DLT template category (PolicyFacts.template_categories).
    """

    GENERAL = "GENERAL"
    INSURANCE_PREMIUM = "INSURANCE_PREMIUM"
    MUTUAL_FUND = "MUTUAL_FUND"
    CREDIT_CARD_BILL = "CREDIT_CARD_BILL"


@dataclass(frozen=True, slots=True)
class MandateRecord:
    mandate_id: str
    """The rail's id — a UPI mandate's UMN."""

    rail: MandateRail
    category: MandateCategory
    state: MandateState
    """The state the last applied transition left it in. A guard reads
    `state_at` instead, which also applies the transitions that happen on
    their own."""

    valid_until: datetime
    """Required, because the Framework requires it: "Every e-mandate registered
    by the issuer shall specify the validity period" (§4(b))."""

    revocable_by_payer: bool = True
    """False for a loan-repayment or EMI mandate created with NPCI's
    "revokeable" flag set to "N" (OC-125A ¶2-3): the customer's app offers
    neither revoke nor pause, and revocation goes through the merchant."""

    paused_until: datetime | None = None
    """When a pause ends, where the customer set an end. Meaningful only while
    PAUSED."""

    token_id: str | None = None
    """Razorpay's token for the mandate — "The `token_id` generated when the
    customer successfully completes the authorisation payment" — which both
    the pre-debit notice's order and the debit carry. None where no Razorpay
    token exists, which is every mandate the simulator builds."""

    razorpay_customer_id: str | None = None
    """Razorpay's customer the token belongs to, which a recurring payment
    requires. Holds an id, never the email or contact behind it."""

    def state_at(self, at: datetime) -> MandateState:
        """What a debit presented at `at` would meet.

        Two transitions happen whether or not anyone applies them: a validity
        period ends, and a pause with an end date lapses. A guard that read
        `state` alone would debit an expired mandate for as long as nobody
        had got round to marking it expired.
        """
        if self.state in TERMINAL:
            return self.state
        if self.state in (MandateState.ACTIVE, MandateState.PAUSED) and at >= self.valid_until:
            return MandateState.EXPIRED
        if (
            self.state is MandateState.PAUSED
            and self.paused_until is not None
            and at >= self.paused_until
        ):
            return MandateState.ACTIVE
        return self.state
