"""FastAPI webhook receiver: verify Razorpay's HMAC signature, dedupe on
x-razorpay-event-id, append to the event store.

Both registered webhooks fire for every payment, and Razorpay itself
redelivers each one once more (VERIFIED.md), from two different IPs within
the same millisecond in every capture. That rules out check-then-act dedupe
(has_event() then append()): two near-simultaneous deliveries can both pass
the check before either has appended. Dedupe here is store.append()'s return
value — the insert attempt and the dedupe decision are the same atomic
operation, not a read followed by a write.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import FastAPI, Header, HTTPException, Request

from vasool.clock import Clock, RealClock
from vasool.events.schemas import from_webhook
from vasool.events.store import EventStore


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Razorpay signs the exact raw request body with HMAC-SHA256 over the
    webhook secret. Must run against the raw bytes, not a re-serialised
    dict — re-encoding can change whitespace/key order and silently break
    verification."""
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def create_app(
    *, store: EventStore, webhook_secret: str, pepper: str, clock: Clock | None = None
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
        return {"ok": True, "duplicate": not inserted}

    return app
