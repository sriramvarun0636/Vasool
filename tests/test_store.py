"""EventStore: append-only, dedupe on event_id.

VERIFIED.md: every webhook observed live arrived twice with an identical
x-razorpay-event-id. Dedupe on append is not defensive coding here, it's the
normal-operation path.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vasool.events.schemas import FailureEvent, from_webhook
from vasool.events.store import EventStore

SOME_TIME = datetime(2026, 8, 21, 14, 0, 0, tzinfo=timezone.utc)
TEST_PEPPER = "test-pepper-do-not-use-in-prod"


def make_failure_event(event_id: str = "evt_1", entity_id: str = "pay_1") -> FailureEvent:
    body = {
        "account_id": "acc_TEST",
        "created_at": 1787299792,
        "payload": {
            "payment": {
                "entity": {
                    "id": entity_id,
                    "amount": 50000,
                    "currency": "INR",
                    "method": "card",
                    "contact": "+919392284464",
                    "email": "void@razorpay.com",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_source": "gateway",
                    "error_step": "payment_authorization",
                    "error_reason": "payment_failed",
                }
            }
        },
    }
    return from_webhook(event_id=event_id, body=body, pepper=TEST_PEPPER)


@pytest.fixture
def store() -> EventStore:
    return EventStore(":memory:")


def test_append_new_event_succeeds(store: EventStore):
    ok = store.append(
        event_id="evt_1",
        event_name="payment.failed",
        received_at=SOME_TIME,
        raw_body={"event": "payment.failed"},
        failure_event=make_failure_event(),
    )
    assert ok is True
    assert store.has_event("evt_1")


def test_duplicate_event_id_is_a_no_op(store: EventStore):
    """The exact scenario VERIFIED.md documents: the same event_id arrives
    twice. Second append must not create a second row."""
    first = store.append(
        event_id="evt_1",
        event_name="payment.failed",
        received_at=SOME_TIME,
        raw_body={"event": "payment.failed"},
        failure_event=make_failure_event(),
    )
    second = store.append(
        event_id="evt_1",
        event_name="payment.failed",
        received_at=SOME_TIME,
        raw_body={"event": "payment.failed"},
        failure_event=make_failure_event(),
    )
    assert first is True
    assert second is False
    assert store.all_event_ids() == ["evt_1"]


def test_distinct_events_both_stored(store: EventStore):
    store.append(
        event_id="evt_1",
        event_name="payment.failed",
        received_at=SOME_TIME,
        raw_body={"event": "payment.failed"},
        failure_event=make_failure_event(event_id="evt_1"),
    )
    store.append(
        event_id="evt_2",
        event_name="payment.failed",
        received_at=SOME_TIME,
        raw_body={"event": "payment.failed"},
        failure_event=make_failure_event(event_id="evt_2"),
    )
    assert store.all_event_ids() == ["evt_1", "evt_2"]


def test_get_missing_event_returns_none(store: EventStore):
    assert store.get("does-not-exist") is None


def test_get_round_trips_failure_event(store: EventStore):
    failure = make_failure_event()
    store.append(
        event_id="evt_1",
        event_name="payment.failed",
        received_at=SOME_TIME,
        raw_body={"event": "payment.failed"},
        failure_event=failure,
    )
    record = store.get("evt_1")
    assert record is not None
    assert record["failure_event"] == failure


def test_non_failure_event_can_have_no_failure_event(store: EventStore):
    """Not every webhook is a payment.failed — e.g. payment.captured. Those
    still need to be stored (for out-of-band-success detection later) without
    a FailureEvent attached."""
    ok = store.append(
        event_id="evt_captured",
        event_name="payment.captured",
        received_at=SOME_TIME,
        raw_body={"event": "payment.captured"},
        failure_event=None,
    )
    assert ok is True
    record = store.get("evt_captured")
    assert record["failure_event"] is None
    assert record["event_name"] == "payment.captured"


def test_store_exposes_no_update_or_delete():
    """Append-only by construction, not by convention: there is nothing on
    this class that can mutate or remove a row once written."""
    forbidden = {"update", "delete", "remove", "modify"}
    public_methods = {name for name in dir(EventStore) if not name.startswith("_")}
    assert not (public_methods & forbidden)
