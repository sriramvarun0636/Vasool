"""FastAPI webhook receiver: verify Razorpay's HMAC signature, dedupe on
x-razorpay-event-id, append to the event store.

Both registered webhooks fire for every payment, and Razorpay itself
redelivers each one once more (VERIFIED.md), from two different IPs within
the same millisecond in every capture. That rules out check-then-act dedupe
(has_event() then append()): two near-simultaneous deliveries can both pass
the check before either has appended. Dedupe here is store.append()'s return
value — the insert attempt and the dedupe decision are the same atomic
operation, not a read followed by a write.

**Settlement.** Two events close a recovery episode: `payment_link.paid` for
a REAUTH_LINK/REATTEMPT_LINK, and `payment.captured` for a
SILENT_RETRY/TIMED_RETRY that this agent's own executor dispatched.
vasool/events/settlement.py explains why each is attributable and
`order.paid` never is, and now owns the dispatch itself
(`settle_from_webhook`) so that windtunnel/ can drive the same code path
rather than a second copy of it. What stays here is what is genuinely the
receiver's: the signature, the dedupe, and the decision that only the first
delivery of an event may settle anything. `machine` is optional so every
existing caller/test that only cares about signature and dedupe is
unaffected; production wiring passes a real PolicyMachine. `retry_index` is
optional independently of `machine` — omitting it just means both
correlations are skipped.

**`retry_index` is read in two directions, not one.** A `payment.captured`
whose payment id it knows is one of our own retries succeeding, and settles
the episode. A `payment.failed` whose payment id it knows is that same retry
*failing*, and is the next attempt of that episode rather than a new one —
`createRecurring` creates a new payment either way, so both webhooks arrive
carrying an id the policy plane has never seen. The failed direction is
applied where the FailureEvent is minted
(vasool/events/schemas.py::from_webhook), because that is the seam
windtunnel/ crosses too and a second copy here would drift from it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Header, HTTPException, Request

from vasool.clock import Clock, RealClock
from vasool.events.schemas import from_webhook
from vasool.events.settlement import RetryIndex, SettlementTarget, settle_from_webhook
from vasool.events.store import EventStore

log = logging.getLogger(__name__)

__all__ = ["SettlementTarget", "create_app", "verify_signature"]
"""SettlementTarget moved to vasool/events/settlement.py alongside the
dispatch that uses it, and is re-exported here so `receiver.SettlementTarget`
keeps resolving for anything that already refers to it."""


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Razorpay signs the exact raw request body with HMAC-SHA256 over the
    webhook secret. Must run against the raw bytes, not a re-serialised
    dict — re-encoding can change whitespace/key order and silently break
    verification."""
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def create_app(
    *,
    store: EventStore,
    webhook_secret: str,
    pepper: str,
    clock: Clock | None = None,
    machine: SettlementTarget | None = None,
    retry_index: RetryIndex | None = None,
) -> FastAPI:
    clock = clock or RealClock()
    app = FastAPI()

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_razorpay_signature: str = Header(alias="x-razorpay-signature"),
        x_razorpay_event_id: str = Header(alias="x-razorpay-event-id"),
    ) -> dict:
        raw_body = await request.body()
        if not verify_signature(raw_body, x_razorpay_signature, webhook_secret):
            raise HTTPException(status_code=400, detail="invalid signature")

        body = json.loads(raw_body)
        event_name = body.get("event", "unknown")

        failure_event = (
            from_webhook(
                event_id=x_razorpay_event_id,
                body=body,
                pepper=pepper,
                # A payment.failed whose payment id is in the index is a
                # failed retry of a known episode, not a new failure — see
                # vasool/events/schemas.py::from_webhook. The same index the
                # settlement path below reads, used in the other direction.
                retry_index=retry_index,
            )
            if event_name == "payment.failed"
            else None
        )

        inserted = store.append(
            event_id=x_razorpay_event_id,
            event_name=event_name,
            received_at=clock.now(),
            raw_body=body,
            failure_event=failure_event,
        )

        # Gated on `inserted`: only the first delivery of an event may settle
        # anything. Razorpay delivers every webhook at least twice
        # (docs/VERIFIED.md), and while PolicyMachine.settled() is
        # independently idempotent there is no reason to lean on that here.
        if inserted and machine is not None:
            settle_from_webhook(
                event_name=event_name,
                body=body,
                machine=machine,
                retry_index=retry_index,
            )

        return {"ok": True, "duplicate": not inserted}

    return app
