"""FailureEvent: normalised form of a Razorpay payment.failed webhook.

Every field is derived from real captures in data/observed_payloads/ (live)
and data/stubbed_payloads/ (hand-built, _SIMULATED: true) — see
docs/VERIFIED.md. Fields the design spec proposed that do NOT exist in any
real payload were deliberately left out rather than invented:

  - attempt_number: never present on a payment.failed payload. Whoever
    consumes the event store next (diagnosis/policy) has to compute it by
    counting prior FailureEvents for the same entity/order, not read it off
    the wire.
  - is_recurring, mandate_id: no subscription.* or mandate payload has ever
    been observed — subscriptions are unavailable pre-activation (see
    VERIFIED.md). Add these once a real subscription payload exists to
    derive them from.

# VERIFY: error_code/error_source/error_step/error_reason are typed as plain
# str, not a restricted Literal, because only one value of each has been
# observed live (BAD_REQUEST_ERROR / gateway / payment_authorization /
# payment_failed) plus the values in data/stubbed_payloads/. Restricting to
# Razorpay's documented enum would mean trusting doc knowledge over observed
# data, which CLAUDE.md forbids. Same reasoning for `method` (only "card"
# observed).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


def derive_customer_id(contact: str | None, email: str | None, *, pepper: str) -> str:
    """Pseudonymises contact+email into a customer_id via HMAC-SHA256 keyed on
    a secret pepper (VASOOL_ID_PEPPER — see .env.example), not a plain hash.

    No Customer object exists in any observed payload — this is a
    pre-KYC/unactivated account, so Razorpay never sends a customer_id, only
    `contact` and `email` on the payment itself. A plain sha256(contact) is
    not protection: Indian mobile numbers are ~10^9 candidates, small enough
    to hash every one and reverse the digest in seconds. Keying the hash on a
    pepper closes that — the customer_id can't be recomputed from a candidate
    phone number without also knowing the pepper.

    This is pseudonymisation, not anonymisation: anyone holding both
    VASOOL_ID_PEPPER and the phone number still recovers the same id, and the
    mapping isn't destroyed anywhere else in the system. Don't oversell it as
    a DPDP data-minimisation guarantee.

    KNOWN LIMITATION: keying on contact+email means the same human contacting
    with two different emails gets two different customer_ids, silently
    bypassing FrequencyCapGuard's per-customer contact cap. See adversary
    attack A13 (duplicate customer records, same human). Not fixed here —
    fixing it needs a real identity resolution step this session doesn't
    build.
    """
    basis = f"{contact or ''}|{email or ''}"
    return hmac.new(pepper.encode(), basis.encode(), hashlib.sha256).hexdigest()


class FailureEvent(BaseModel):
    """Normalised from payment.failed. Immutable once constructed."""

    event_id: str  # x-razorpay-event-id header, NOT body — see VERIFIED.md
    entity_id: str  # payment.entity.id (pay_xxx)
    customer_id: str  # derive_customer_id(contact, email) — see above
    merchant_id: str  # body.account_id (acc_xxx), renamed for domain clarity
    amount_paise: int  # payment.entity.amount — already integer paise
    currency: str
    method: str  # payment.entity.method
    occurred_at: datetime  # body.created_at: when Razorpay emitted the event

    error_code: str
    error_source: str
    error_step: str
    error_reason: str

    model_config = {"frozen": True}


def from_webhook(*, event_id: str, body: dict[str, Any], pepper: str) -> FailureEvent:
    """Build a FailureEvent from an already-JSON-decoded payment.failed webhook body."""
    payment = body["payload"]["payment"]["entity"]
    return FailureEvent(
        event_id=event_id,
        entity_id=payment["id"],
        customer_id=derive_customer_id(payment.get("contact"), payment.get("email"), pepper=pepper),
        merchant_id=body["account_id"],
        amount_paise=payment["amount"],
        currency=payment["currency"],
        method=payment["method"],
        occurred_at=datetime.fromtimestamp(body["created_at"], tz=timezone.utc),
        error_code=payment["error_code"],
        error_source=payment["error_source"],
        error_step=payment["error_step"],
        error_reason=payment["error_reason"],
    )
