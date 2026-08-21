"""Append-only SQLite store for received webhook events.

Dedupe key is event_id (x-razorpay-event-id). Per VERIFIED.md, every webhook
observed live was delivered twice with an identical event id — append() makes
the second delivery a no-op rather than raising, because that's normal
operation, not an error condition.

Append-only is enforced by omission: this class has no update or delete
method. tests/test_store.py::test_store_exposes_no_update_or_delete checks
that stays true.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from vasool.events.schemas import FailureEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_body TEXT NOT NULL,
    failure_event TEXT
)
"""


class EventStore:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def has_event(self, event_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None

    def append(
        self,
        *,
        event_id: str,
        event_name: str,
        received_at: datetime,
        raw_body: dict,
        failure_event: FailureEvent | None,
    ) -> bool:
        """Insert a new event row. Returns False without writing anything if
        event_id already exists."""
        try:
            self._conn.execute(
                "INSERT INTO events (event_id, event_name, received_at, raw_body, failure_event)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    event_id,
                    event_name,
                    received_at.isoformat(),
                    json.dumps(raw_body),
                    failure_event.model_dump_json() if failure_event is not None else None,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get(self, event_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT event_id, event_name, received_at, raw_body, failure_event"
            " FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        event_id_, event_name, received_at, raw_body, failure_event_json = row
        return {
            "event_id": event_id_,
            "event_name": event_name,
            "received_at": datetime.fromisoformat(received_at),
            "raw_body": json.loads(raw_body),
            "failure_event": (
                FailureEvent.model_validate_json(failure_event_json)
                if failure_event_json is not None
                else None
            ),
        }

    def all_event_ids(self) -> list[str]:
        rows = self._conn.execute("SELECT event_id FROM events ORDER BY rowid").fetchall()
        return [r[0] for r in rows]
