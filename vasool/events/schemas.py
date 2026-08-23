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

**Which episode a failure belongs to is decided here.** `from_webhook` is the
only place a `payment.failed` body becomes a FailureEvent — the receiver
calls it, and so does windtunnel/payloads.py — which makes it the one seam
production and the simulator both cross, and therefore the only place a
correlation can live without the two drifting apart. See `from_webhook` for
why a failed retry needs one at all.

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

# RetryIndex is imported rather than re-declared as a third structural copy:
# it is already a Protocol in this same package, describing the same shape for
# the same reason (vasool/events/settlement.py). settlement.py imports nothing
# from this module, so there is no cycle, and one Protocol is better than two
# that can quietly disagree.
from vasool.events.settlement import RetryIndex


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
    entity_id: str  # payment.entity.id (pay_xxx) — but see retried_payment_id
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

    retried_payment_id: str | None = None
    """The payment Razorpay actually reported failing, when that payment was
    one of our own retries and `entity_id` above has been resolved back to
    the episode it belongs to.

    None on an original failure, which is every failure that is not a
    continuation — there `entity_id` is the failing payment and there is
    nothing to distinguish. Retained rather than discarded because without it
    the event would claim a failure of a payment that did not fail, which is
    a small lie in the core type and exactly the kind that surfaces later
    against Razorpay's own records. Mirrors Proposal.supersedes: the thing
    this one stands in for is one hop away rather than gone.
    """

    model_config = {"frozen": True}


def from_webhook(
    *,
    event_id: str,
    body: dict[str, Any],
    pepper: str,
    retry_index: RetryIndex | None = None,
) -> FailureEvent:
    """Build a FailureEvent from an already-JSON-decoded payment.failed webhook body.

    **`retry_index` is what makes a failed retry advance its own episode.**
    `RazorpayClient.retry_payment` wraps `createRecurring`, which creates a
    NEW payment with its own id — the same fact that forces a *successful*
    retry's `payment.captured` to be correlated through RetryIndex
    (docs/VERIFIED.md). A *failed* retry has the identical problem: Razorpay
    fires `payment.failed` for the new payment, and
    `PolicyMachine.observe` keys on `entity_id`, so without this a real
    failed retry opens a brand-new episode at attempt 1 rather than
    advancing the one it belongs to — which would leave RetryCapGuard
    counting to one forever, docs/taxonomy.md §6's salary ladder unable to
    reach attempt 2, and `card_expired`'s flagship argument describing an
    attempt budget nothing tracks.

    So a `payment.failed` whose payment id is in the index is not a new
    failure; it is the next attempt of a known episode, and `entity_id`
    resolves to that episode's own payment while `retried_payment_id` keeps
    the one that actually failed. Nothing is guessed: the index holds
    Razorpay's own response id, recorded by the executor against the
    entity_id that asked for it.

    Omitting `retry_index` leaves every event exactly as it was before this
    correlation existed. So does passing one that has never heard of this
    payment — an ordinary first-time failure, or a retry some other process
    dispatched. This narrows what is treated as a continuation; it never
    widens what a `payment.failed` is trusted to mean.

    # VERIFY: two gaps remain and neither is closeable here. RetryIndex is
    process-local (vasool/actions/executor.py), so a restart between a retry
    firing and its failure webhook arriving loses the mapping and that
    webhook does open a fresh episode at attempt 1 — the exact mirror of the
    settlement gap already recorded for `payment.captured`. And if
    `createRecurring`'s response carries no id, nothing is recorded to
    correlate against in the first place. Both fail in the same safe
    direction: an episode is under-counted, never over-counted.
    """
    payment = body["payload"]["payment"]["entity"]
    payment_id = payment["id"]
    episode_entity_id = (
        retry_index.entity_id_for(payment_id) if retry_index is not None else None
    )
    return FailureEvent(
        event_id=event_id,
        entity_id=episode_entity_id or payment_id,
        retried_payment_id=payment_id if episode_entity_id is not None else None,
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
