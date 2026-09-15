"""EventStore: append-only, dedupe on event_id.

VERIFIED.md: every webhook observed live arrived twice with an identical
x-razorpay-event-id. Dedupe on append is not defensive coding here, it's the
normal-operation path.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from vasool.events.schemas import FailureEvent, from_webhook
from vasool.events.store import SYNCHRONOUS_FULL, EventStore, JournalModeRefused

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


class TestDurability:
    """WAL and synchronous=FULL on a file-backed store — read back, never assumed.

    docs/EVALUATION.md §10, 2026-09-15. `PRAGMA journal_mode=WAL` does not
    raise when it fails; it returns whatever mode it left the database in. A
    test that only checks the statement ran would pass on exactly the
    filesystems where WAL silently does not happen, so every assertion here
    reads the database's actual state.
    """

    def test_a_file_backed_store_is_in_wal_mode(self, tmp_path):
        store = EventStore(tmp_path / "events.db")
        assert store.journal_mode == "wal"
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_every_commit_is_synced(self, tmp_path):
        """FULL, not NORMAL: SQLite documents that a WAL commit under NORMAL
        might roll back after a power loss, and an acknowledged webhook that
        rolls back is one Razorpay will never send again."""
        store = EventStore(tmp_path / "events.db")
        assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == SYNCHRONOUS_FULL

    def test_wal_is_what_a_fresh_connection_finds(self, tmp_path):
        """WAL is a property of the file, so it is checked from outside the
        store too: a plain connection that sets nothing must find it."""
        path = tmp_path / "events.db"
        EventStore(path).append(event_id="evt_1", event_name="payment.failed",
                                received_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
                                raw_body={}, failure_event=None)
        assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_an_in_memory_store_says_memory_rather_than_pretending(self):
        """SQLite ignores WAL for an in-memory database, and every store in the
        tests, the demo and the arena is one — so the store reports it."""
        assert EventStore(":memory:").journal_mode == "memory"

    def test_a_refused_wal_raises_instead_of_passing_silently(self, tmp_path, monkeypatch):
        real_connect = sqlite3.connect

        class Rows:
            def __init__(self, *row):
                self._row = row

            def fetchone(self):
                return self._row

        class Refusing:
            """A filesystem that cannot hold a WAL, as SQLite reports one."""

            def __init__(self, *args, **kwargs):
                self._conn = real_connect(*args, **kwargs)

            def execute(self, sql, *args):
                if sql.replace(" ", "").upper() == "PRAGMAJOURNAL_MODE=WAL":
                    return Rows("delete")
                return self._conn.execute(sql, *args)

            def commit(self):
                self._conn.commit()

        monkeypatch.setattr("vasool.events.store.sqlite3.connect", Refusing)
        with pytest.raises(JournalModeRefused, match="'delete'"):
            EventStore(tmp_path / "events.db")
