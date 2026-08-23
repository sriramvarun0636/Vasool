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


class FakeMachine:
    """Stands in for PolicyMachine at receiver.py's SettlementTarget seam —
    no policy plane involved, this session only wires the receiver."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def settled(self, entity_id: str, *, reason: str, amount_paise: int) -> None:
        self.calls.append((entity_id, reason, amount_paise))


class FakeRetryIndex:
    """Stands in for vasool/actions/executor.py::RetryIndex at its own
    public interface — no executor, no real retry ever dispatched."""

    def __init__(self, **known: str) -> None:
        self._known = known

    def entity_id_for(self, payment_id: str) -> str | None:
        return self._known.get(payment_id)


class TestSettlementWiring:
    """docs/VERIFIED.md: only payment_link.paid can be attributed to an
    episode with what's on disk, via the vasool_entity_id this agent's own
    executor.py stamps into the link's notes."""

    def test_a_payment_link_paid_with_our_own_notes_settles_the_episode(
        self, store: EventStore, clock: VirtualClock
    ):
        machine = FakeMachine()
        app = create_app(
            store=store, webhook_secret=TEST_SECRET, pepper=TEST_PEPPER, clock=clock, machine=machine
        )
        client = TestClient(app)

        fixture = _load("payment_link_paid__none__12b6f2.json")
        body = fixture["body"]
        body["payload"]["payment_link"]["entity"]["notes"] = {"vasool_entity_id": "pay_original_fail"}
        raw = json.dumps(body).encode()

        resp = client.post(
            "/webhook",
            content=raw,
            headers={
                "content-type": "application/json",
                "x-razorpay-signature": _sign(raw),
                "x-razorpay-event-id": "evt-settle",
            },
        )

        assert resp.status_code == 200
        assert machine.calls == [("pay_original_fail", "payment_link.paid", 50000)]

    def test_a_payment_link_paid_with_no_vasool_notes_settles_nothing(
        self, store: EventStore, clock: VirtualClock
    ):
        """The real Session 0A capture: created by hand, notes is null. Must
        not guess a correlation — see settlement.py's module docstring."""
        machine = FakeMachine()
        app = create_app(
            store=store, webhook_secret=TEST_SECRET, pepper=TEST_PEPPER, clock=clock, machine=machine
        )
        client = TestClient(app)

        _post_fixture(client, "payment_link_paid__none__12b6f2.json", event_id="evt-no-correlation")

        assert machine.calls == []

    def test_payment_captured_with_no_retry_index_wired_settles_nothing(
        self, store: EventStore, clock: VirtualClock
    ):
        """payment.captured fires for every successful payment, recovery or
        not, and carries no episode identifier on its own -- without a
        RetryIndex there's nothing to correlate it against, so it must be a
        no-op exactly as it always was."""
        machine = FakeMachine()
        app = create_app(
            store=store, webhook_secret=TEST_SECRET, pepper=TEST_PEPPER, clock=clock, machine=machine
        )
        client = TestClient(app)

        _post_fixture(client, "payment_captured__none__0ced11.json", event_id="evt-captured-2")

        assert machine.calls == []

    def test_payment_captured_not_matching_our_retry_index_settles_nothing(
        self, store: EventStore, clock: VirtualClock
    ):
        """item 2: a RetryIndex is wired, but this particular payment id
        isn't in it -- a customer's ordinary checkout, or a Payment Links
        payment, must still correlate to nothing."""
        machine = FakeMachine()
        retry_index = FakeRetryIndex(pay_some_other_retry="pay_unrelated")
        app = create_app(
            store=store,
            webhook_secret=TEST_SECRET,
            pepper=TEST_PEPPER,
            clock=clock,
            machine=machine,
            retry_index=retry_index,
        )
        client = TestClient(app)

        _post_fixture(client, "payment_captured__none__0ced11.json", event_id="evt-captured-3")

        assert machine.calls == []

    def test_payment_captured_matching_our_retry_index_settles_the_episode(
        self, store: EventStore, clock: VirtualClock
    ):
        """item 2: the id our own retry_payment call got back, now arriving
        on a payment.captured webhook, closes the episode it was a retry
        for -- via RetryIndex, never a guessed join key."""
        machine = FakeMachine()
        fixture = _load("payment_captured__none__0ced11.json")
        body = fixture["body"]
        captured_payment_id = body["payload"]["payment"]["entity"]["id"]
        retry_index = FakeRetryIndex(**{captured_payment_id: "pay_original_fail"})
        app = create_app(
            store=store,
            webhook_secret=TEST_SECRET,
            pepper=TEST_PEPPER,
            clock=clock,
            machine=machine,
            retry_index=retry_index,
        )
        client = TestClient(app)

        _post_fixture(client, "payment_captured__none__0ced11.json", event_id="evt-captured-4")

        assert machine.calls == [("pay_original_fail", "payment.captured", 50000)]

    def test_a_duplicate_captured_delivery_settles_only_once(
        self, store: EventStore, clock: VirtualClock
    ):
        machine = FakeMachine()
        fixture = _load("payment_captured__none__0ced11.json")
        body = fixture["body"]
        captured_payment_id = body["payload"]["payment"]["entity"]["id"]
        retry_index = FakeRetryIndex(**{captured_payment_id: "pay_original_fail"})
        app = create_app(
            store=store,
            webhook_secret=TEST_SECRET,
            pepper=TEST_PEPPER,
            clock=clock,
            machine=machine,
            retry_index=retry_index,
        )
        client = TestClient(app)

        _post_fixture(client, "payment_captured__none__0ced11.json", event_id="evt-captured-dup")
        _post_fixture(client, "payment_captured__none__0ced11.json", event_id="evt-captured-dup")

        assert len(machine.calls) == 1

    def test_a_duplicate_settlement_delivery_settles_only_once(
        self, store: EventStore, clock: VirtualClock
    ):
        """VERIFIED.md: every webhook is delivered at least twice. Gating on
        the store's own dedupe result means the machine only ever hears
        about the first delivery — PolicyMachine.settled() is independently
        idempotent too, but there is no reason to rely on that here."""
        machine = FakeMachine()
        app = create_app(
            store=store, webhook_secret=TEST_SECRET, pepper=TEST_PEPPER, clock=clock, machine=machine
        )
        client = TestClient(app)

        fixture = _load("payment_link_paid__none__12b6f2.json")
        body = fixture["body"]
        body["payload"]["payment_link"]["entity"]["notes"] = {"vasool_entity_id": "pay_original_fail"}
        raw = json.dumps(body).encode()
        headers = {
            "content-type": "application/json",
            "x-razorpay-signature": _sign(raw),
            "x-razorpay-event-id": "evt-settle-dup",
        }

        client.post("/webhook", content=raw, headers=headers)
        client.post("/webhook", content=raw, headers=headers)

        assert len(machine.calls) == 1

    def test_no_machine_wired_is_still_a_valid_configuration(
        self, store: EventStore, clock: VirtualClock
    ):
        """create_app's existing callers (and every other test in this file)
        never pass machine= at all — that must keep working exactly as
        before."""
        app = create_app(store=store, webhook_secret=TEST_SECRET, pepper=TEST_PEPPER, clock=clock)
        client = TestClient(app)

        resp = _post_fixture(client, "payment_link_paid__none__12b6f2.json", event_id="evt-no-machine")

        assert resp.status_code == 200


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


class TestAFailedRetryIsRecognisedAtTheBoundary:
    """The receiver mints the FailureEvent, so it is where a failed retry
    either is or is not recognised as a continuation of its episode.

    `RetryIndex` is read in both directions here: a `payment.captured` it
    knows is our retry succeeding (above), a `payment.failed` it knows is
    that same retry failing. The correlation itself lives in
    vasool/events/schemas.py::from_webhook — the seam windtunnel/ crosses
    too — and these check the receiver actually hands it the index.
    """

    def _post_failure(self, client: TestClient, payment_id: str, event_id: str):
        fixture = _load("payment_failed__payment_failed__firstcapture.json")
        body = fixture["body"]
        body["payload"]["payment"]["entity"]["id"] = payment_id
        raw = json.dumps(body).encode()
        return client.post(
            "/webhook",
            content=raw,
            headers={
                "content-type": "application/json",
                "x-razorpay-signature": _sign(raw),
                "x-razorpay-event-id": event_id,
            },
        )

    def test_a_failure_for_a_payment_our_retry_created_names_the_original_episode(
        self, store: EventStore, clock: VirtualClock
    ):
        app = create_app(
            store=store,
            webhook_secret=TEST_SECRET,
            pepper=TEST_PEPPER,
            clock=clock,
            retry_index=FakeRetryIndex(pay_our_retry="pay_original_fail"),
        )
        self._post_failure(TestClient(app), "pay_our_retry", "evt-retry-failed")

        event = store.get("evt-retry-failed")["failure_event"]
        assert event.entity_id == "pay_original_fail"
        assert event.retried_payment_id == "pay_our_retry"

    def test_an_ordinary_failure_is_left_exactly_as_it_was(
        self, store: EventStore, clock: VirtualClock
    ):
        app = create_app(
            store=store,
            webhook_secret=TEST_SECRET,
            pepper=TEST_PEPPER,
            clock=clock,
            retry_index=FakeRetryIndex(pay_some_other_retry="pay_unrelated"),
        )
        self._post_failure(TestClient(app), "pay_a_customers_own_failure", "evt-ordinary")

        event = store.get("evt-ordinary")["failure_event"]
        assert event.entity_id == "pay_a_customers_own_failure"
        assert event.retried_payment_id is None

    def test_with_no_index_wired_the_receiver_behaves_as_before(
        self, client: TestClient, store: EventStore
    ):
        self._post_failure(client, "pay_our_retry", "evt-no-index")

        event = store.get("evt-no-index")["failure_event"]
        assert event.entity_id == "pay_our_retry"
        assert event.retried_payment_id is None
