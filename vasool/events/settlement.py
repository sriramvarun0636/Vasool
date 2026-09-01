"""Correlating a settlement webhook back to the recovery episode it closes.

Two paths are wired, one per family of intervention, because each has a
different non-guessed join key on offer — and `order.paid` has none, so it
stays unwired.

**Why `payment_link.paid` and not `payment.captured` or `order.paid`, for a
REAUTH_LINK/REATTEMPT_LINK.** VERIFIED.md records that a successful Payment
Links checkout fires all three events in sequence, so all three are equally
valid signals that money landed. But only one of them can be traced back to
*which* episode it closes with anything that is actually on disk.

`payment.captured` and `order.paid` fire for every successful payment on the
account, including a customer's very first, never-failed checkout — neither
payload carries a field that marks it as closing a recovery on its own. The
only lead either offers is `order_id`, and it does not hold: a
REAUTH_LINK/REATTEMPT_LINK created by vasool/actions/executor.py opens a
brand-new Payment Link with its own new order, never the original failed
payment's order_id (confirmed by inspecting create_payment_link — it takes no
order_id and Razorpay allocates one). Attributing settlement by order_id,
amount, or customer would mean guessing a join key the project's working
agreement specifically says not to guess — an honest gap, not an oversight.

`payment_link.paid` is different because the payment link's `notes` field is
merchant-supplied metadata, not something Razorpay decides. executor.py's
`_link` already sets `notes={"vasool_proposal_id": ..., "vasool_entity_id":
...}` on every link it creates, so a `payment_link.paid` webhook for a link
this agent made carries its own entity_id back, with no join key to guess.

# VERIFY: whether Razorpay actually echoes `notes` back unmodified on the
`payment_link.paid` webhook has never been observed live. Session 0A's only
`payment_link.paid` capture (data/observed_payloads/) predates this notes
tag entirely — that link was created by hand, so its own `notes` is null.
The Payment Links API documents `notes` as pass-through metadata and echoes
it on create/fetch responses, so the assumption is reasonable, but it
remains documentation until a link created with `vasool_entity_id` in its
notes is observed coming back with it on a real webhook.

**Why `payment.captured` for a SILENT_RETRY/TIMED_RETRY.** `createRecurring`
has nothing resembling `notes` to tag — it takes `amount`, `currency`,
`payment_id` (RazorpayClient.retry_payment), and no merchant-metadata field
Session 0A ever observed. But it does return its own new payment's id in the
response, and vasool/actions/executor.py::RazorpayExecutor._retry already
records that id against the entity_id that asked for it, in its own
RetryIndex — Razorpay's own answer to "what did you just create", not
anything guessed. `entity_id_from_payment_captured` below reads a captured
payment's id and looks it up there.

# VERIFY: whether `createRecurring`'s synchronous response id is the same id
that later appears on `payload.payment.entity.id` of a `payment.captured`
webhook has never been observed live — RazorpayClient.retry_payment's own
VERIFY note already flags that this call was never exercised at all (Session
0A never activated the merchant account, so the token-based recharge path it
wraps was never reachable to test). A payment entity's id being stable across
its own lifecycle is standard Razorpay behaviour and the same shape
tests/test_executor.py's fake client already assumes, but it remains a
documented assumption, not an observed fact, until a real createRecurring
response is captured and matched against a subsequent capture.

**Why not `order.paid`, ever, for either path.** It carries the same
order_id problem as `payment.captured` without even a payment id to fall
back on — nothing about it is more attributable than payment.captured, so
there is no path where wiring it adds anything the other two don't already
cover.

**Where the dispatch lives, and why it moved here.** `settle_from_webhook`
below — which event name is wired to which correlation — used to sit inline
in vasool/events/receiver.py's route closure. It was lifted out so that
windtunnel/ can drive the *same* dispatch production does. The simulator
feeds settlement back through these paths rather than calling
`PolicyMachine.settled()` itself, and the whole value of doing that is that
the code being exercised is the real one; a copy of the route's ten lines
would be a second implementation, and the first time it drifted the
evaluation would be measuring behaviour the receiver does not have. The
route still owns everything around the call — signature verification,
dedupe, HTTP — so `tests/test_receiver.py` still covers all of that end to
end.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


def entity_id_from_payment_link_paid(body: dict[str, Any]) -> str | None:
    """The FailureEvent.entity_id this settlement closes, or None.

    None covers two honest cases the caller cannot tell apart from the
    payload alone: this payment link predates the vasool_entity_id tag (every
    capture in data/observed_payloads/ does), or it was never created by this
    agent at all — a merchant's own, unrelated Payment Link. Either way there
    is nothing to settle here.
    """
    payment_link = body.get("payload", {}).get("payment_link", {}).get("entity", {})
    notes = payment_link.get("notes")
    if not isinstance(notes, dict):
        return None
    entity_id = notes.get("vasool_entity_id")
    return entity_id if isinstance(entity_id, str) else None


def amount_paise_from_payment_link_paid(body: dict[str, Any]) -> int:
    """What the customer actually paid, off the payment entity nested in the
    same webhook — never assumed equal to the amount that originally failed."""
    return body["payload"]["payment"]["entity"]["amount"]


class RetryIndex(Protocol):
    """What vasool/actions/executor.py::RetryIndex looks like, from this
    module's point of view — a structural shape, not an import, so events/
    never has to depend on actions/ to read a settlement. The same pattern
    vasool/events/receiver.py's SettlementTarget and vasool/ledger/receipts.py's
    CallJournal already use for the same reason."""

    def entity_id_for(self, payment_id: str) -> str | None: ...


def entity_id_from_payment_captured(body: dict[str, Any], *, retry_index: RetryIndex) -> str | None:
    """The entity_id a SILENT_RETRY/TIMED_RETRY-initiated `payment.captured`
    closes, or None.

    None covers every `payment.captured` that isn't a retry we ourselves
    dispatched: a customer's ordinary first-time checkout, a Payment Links
    checkout (whose payment id `_link` never hands to RetryIndex — see
    executor.py), or a retry Razorpay never actually captured. Every one of
    those falls through exactly as `payment.captured` already did before this
    correlation existed — this narrows what gets attributed, it never widens
    what payment.captured is trusted to mean.
    """
    payment = body.get("payload", {}).get("payment", {}).get("entity", {})
    payment_id = payment.get("id")
    if not isinstance(payment_id, str):
        return None
    return retry_index.entity_id_for(payment_id)


def amount_paise_from_payment_captured(body: dict[str, Any]) -> int:
    """What was actually captured, off the same payment entity the id came
    from — never assumed equal to the amount that originally failed."""
    return body["payload"]["payment"]["entity"]["amount"]


class SettlementTarget(Protocol):
    """What vasool/policy/machine.py::PolicyMachine looks like from here — a
    structural shape, not an import, so the events plane never has to depend
    on the policy plane to type its own seam (the same pattern
    vasool/ledger/receipts.py's CallJournal already uses for the same
    reason). Defined here rather than in receiver.py because the dispatch
    that needs it lives here now; receiver.py re-exports it so its own
    callers are unaffected."""

    def settled(self, entity_id: str, *, reason: str, amount_paise: int) -> None: ...


def settle_from_webhook(
    *,
    event_name: str,
    body: dict[str, Any],
    machine: SettlementTarget,
    retry_index: RetryIndex | None = None,
) -> str | None:
    """Close whichever recovery episode this webhook settles, and say which.

    Returns the entity_id settled, or None when the event settles nothing —
    which is the common case, not an error. Three distinct situations all
    land there and the caller cannot tell them apart from the payload alone:
    an event name that is not a settlement at all, a settlement that carries
    no attributable field (`order.paid`, always — see this module's
    docstring), and a settlement whose correlation simply misses, such as a
    `payment.captured` for a payment this agent never dispatched.

    That last one is worth naming because it is not a gap in this function.
    A customer who pays out of band, through any channel other than a link
    this agent sent, carries no `vasool_entity_id` and appears in no
    RetryIndex — they are indistinguishable from any other payment on the
    account (docs/taxonomy.md §9.9). The episode stays open and the agent
    keeps chasing money it already has. Correlating it anyway would mean
    guessing a join key, which is the one thing this module exists not to do.

    `retry_index` is optional independently of `machine`: omitting it just
    means `payment.captured` correlation is skipped entirely.

    Deduplication is deliberately *not* handled here. Razorpay delivers every
    webhook at least twice (docs/VERIFIED.md) and the caller is the one
    holding the store whose insert decides which delivery is the first —
    vasool/events/receiver.py gates this call on exactly that.
    """
    if event_name == "payment_link.paid":
        entity_id = entity_id_from_payment_link_paid(body)
        if entity_id is None:
            log.info(
                "payment_link.paid carries no vasool_entity_id — not one of "
                "ours, or predates the notes tag; nothing to settle"
            )
            return None
        machine.settled(
            entity_id,
            reason="payment_link.paid",
            amount_paise=amount_paise_from_payment_link_paid(body),
        )
        return entity_id

    if event_name == "payment.captured" and retry_index is not None:
        entity_id = entity_id_from_payment_captured(body, retry_index=retry_index)
        if entity_id is None:
            return None
        machine.settled(
            entity_id,
            reason="payment.captured",
            amount_paise=amount_paise_from_payment_captured(body),
        )
        return entity_id

    return None
