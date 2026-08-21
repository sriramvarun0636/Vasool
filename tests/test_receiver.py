"""Webhook receiver: HMAC verify, dedupe on x-razorpay-event-id, append to store.

Most tests here resign the real captured payload structure from
data/observed_payloads/ with a test secret we control (TEST_SECRET), so they
don't depend on any local .env state. test_real_captured_signature_verifies
is the exception: RAZORPAY_WEBHOOK_SECRET IS available, in .env, so that test
verifies a signature Razorpay actually computed and sent us — see its
docstring for why the raw bytes have to be reconstructed rather than replayed
directly, and docs/VERIFIED.md for the capture-format caveat that follows
from it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from vasool.clock import VirtualClock
from vasool.events.receiver import create_app
from vasool.events.store import EventStore

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED = REPO_ROOT / "data" / "observed_payloads"

TEST_SECRET = "test-webhook-secret-not-real"
TEST_PEPPER = "test-pepper-do-not-use-in-prod"

load_dotenv(REPO_ROOT / ".env")
REAL_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET")


def _load(name: str) -> dict:
    return json.loads((OBSERVED / name).read_text())


def _sign(raw_body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


@pytest.fixture
def store() -> EventStore:
    return EventStore(":memory:")


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(start=datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc))


@pytest.fixture
def client(store: EventStore, clock: VirtualClock) -> TestClient:
    app = create_app(store=store, webhook_secret=TEST_SECRET, pepper=TEST_PEPPER, clock=clock)
    return TestClient(app)


def _post_fixture(client: TestClient, fixture_name: str, event_id: str | None = None):
    fixture = _load(fixture_name)
    body = fixture["body"]
    raw = json.dumps(body).encode()
    eid = event_id or fixture["headers"]["x-razorpay-event-id"]
    signature = _sign(raw)
    return client.post(
        "/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": eid,
        },
    )


def test_valid_signature_is_accepted_and_stored(client: TestClient, store: EventStore):
    resp = _post_fixture(client, "payment_failed__payment_failed__firstcapture.json", event_id="evt-a")
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is False
    assert store.has_event("evt-a")
    record = store.get("evt-a")
    assert record["failure_event"] is not None
    assert record["failure_event"].error_reason == "payment_failed"


def test_invalid_signature_is_rejected(client: TestClient, store: EventStore):
    fixture = _load("payment_failed__payment_failed__firstcapture.json")
    raw = json.dumps(fixture["body"]).encode()
    resp = client.post(
        "/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": "deadbeef" * 8,
            "x-razorpay-event-id": "evt-bad-sig",
        },
    )
    assert resp.status_code == 400
    assert not store.has_event("evt-bad-sig")


def test_duplicate_delivery_is_deduped(client: TestClient, store: EventStore):
    """VERIFIED.md: every webhook is delivered twice with an identical event
    id. Both requests must succeed, only one row must land."""
    first = _post_fixture(client, "payment_failed__payment_failed__firstcapture.json", event_id="evt-dup")
    second = _post_fixture(client, "payment_failed__payment_failed__firstcapture.json", event_id="evt-dup")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert store.all_event_ids().count("evt-dup") == 1


def test_non_failure_event_is_stored_without_crashing(client: TestClient, store: EventStore):
    """payment.captured has no error_code/error_reason at all — must not
    explode trying to build a FailureEvent out of it."""
    resp = _post_fixture(client, "payment_captured__none__0ced11.json", event_id="evt-captured")
    assert resp.status_code == 200
    record = store.get("evt-captured")
    assert record is not None
    assert record["failure_event"] is None
    assert record["event_name"] == "payment.captured"


def test_received_at_comes_from_injected_clock_not_wall_clock(
    client: TestClient, store: EventStore, clock: VirtualClock
):
    _post_fixture(client, "payment_failed__payment_failed__firstcapture.json", event_id="evt-clock")
    record = store.get("evt-clock")
    assert record["received_at"] == clock.now()


def test_missing_signature_header_is_rejected(client: TestClient):
    fixture = _load("payment_failed__payment_failed__firstcapture.json")
    raw = json.dumps(fixture["body"]).encode()
    resp = client.post(
        "/webhook",
        content=raw,
        headers={"content-type": "application/json", "x-razorpay-event-id": "evt-no-sig"},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.skipif(
    not REAL_WEBHOOK_SECRET, reason="RAZORPAY_WEBHOOK_SECRET not set in the environment/.env"
)
def test_real_captured_signature_verifies():
    """Proves the HMAC scheme against a signature Razorpay itself computed
    and sent us, not one this test suite generated.

    tools/catch.py stored request.json() (the parsed dict), not the raw
    request bytes, so the exact bytes Razorpay signed were never saved. This
    only works because json.dumps(body, separators=(",", ":")) happens to
    reproduce Razorpay's wire format byte-for-byte for every payload captured
    in Session 0A: compact (no whitespace), and json.loads/dumps round-trips
    key order and number formatting losslessly for these payloads. That's
    verified empirically here, not guaranteed by any contract — confirmed by
    hand against every recorded x-razorpay-signature in
    data/observed_payloads/ before writing this test. A payload shaped
    differently (e.g. non-ASCII fields) could break the reconstruction
    without this test catching it. See docs/VERIFIED.md for the note that a
    future capture pass should store raw_body_b64 directly instead of
    relying on this round-trip.
    """
    fixture = _load("payment_failed__payment_failed__firstcapture.json")
    raw = json.dumps(fixture["body"], separators=(",", ":")).encode()

    store = EventStore(":memory:")
    app = create_app(store=store, webhook_secret=REAL_WEBHOOK_SECRET, pepper=TEST_PEPPER)
    client = TestClient(app)

    resp = client.post(
        "/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": fixture["headers"]["x-razorpay-signature"],
            "x-razorpay-event-id": fixture["headers"]["x-razorpay-event-id"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is False
