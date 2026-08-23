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
`order.paid` never is. `machine` is optional so every existing caller/test
that only cares about signature and dedupe is unaffected; production wiring
passes a real PolicyMachine. `retry_index` is optional independently of
`machine` — omitting it just means `payment.captured` correlation is
skipped, the same as before this session.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Protocol

from fastapi import FastAPI, Header, HTTPException, Request

from vasool.clock import Clock, RealClock
from vasool.events.schemas import from_webhook
from vasool.events.settlement import (
    RetryIndex,
    amount_paise_from_payment_captured,
    amount_paise_from_payment_link_paid,
    entity_id_from_payment_captured,
    entity_id_from_payment_link_paid,
)
from vasool.events.store import EventStore

log = logging.getLogger(__name__)


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Razorpay signs the exact raw request body with HMAC-SHA256 over the
    webhook secret. Must run against the raw bytes, not a re-serialised
    dict — re-encoding can change whitespace/key order and silently break
    verification."""
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class SettlementTarget(Protocol):
    """What vasool/policy/machine.py::PolicyMachine looks like from here — a
    structural shape, not an import, so the events plane never has to depend
    on the policy plane to type its own seam (the same pattern
    vasool/ledger/receipts.py uses for CallJournal)."""

    def settled(self, entity_id: str, *, reason: str, amount_paise: int) -> None: ...


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
            from_webhook(event_id=x_razorpay_event_id, body=body, pepper=pepper)
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

        if inserted and machine is not None:
            if event_name == "payment_link.paid":
                entity_id = entity_id_from_payment_link_paid(body)
                if entity_id is not None:
                    machine.settled(
                        entity_id,
                        reason="payment_link.paid",
                        amount_paise=amount_paise_from_payment_link_paid(body),
                    )
                else:
                    log.info(
                        "payment_link.paid %s carries no vasool_entity_id — not "
                        "one of ours, or predates the notes tag; nothing to settle",
                        x_razorpay_event_id,
                    )
            elif event_name == "payment.captured" and retry_index is not None:
                entity_id = entity_id_from_payment_captured(body, retry_index=retry_index)
                if entity_id is not None:
                    machine.settled(
                        entity_id,
                        reason="payment.captured",
                        amount_paise=amount_paise_from_payment_captured(body),
                    )

        return {"ok": True, "duplicate": not inserted}

    return app
