"""Append-only SQLite store for received webhook events.

Dedupe key is event_id (x-razorpay-event-id). Per VERIFIED.md, every webhook
observed live was delivered twice with an identical event id — append() makes
the second delivery a no-op rather than raising, because that's normal
operation, not an error condition.

Append-only is enforced by omission: this class has no update or delete
method. tests/test_store.py::test_store_exposes_no_update_or_delete checks
that stays true.

**A file-backed store runs in WAL mode with `synchronous=FULL`, and both are
read back, not assumed.** WAL lets the receiver's request threads write while a
reader reads. FULL is the choice that matters: the receiver answers 2xx only
after `append` commits, Razorpay does not redeliver an event it has had a 2xx
for, and SQLite documents that a WAL commit under `synchronous=NORMAL` "might
roll back following a power loss or system crash", while FULL syncs the WAL
after every commit and is durable across one (sqlite.org/pragma.html). An
acknowledged event lost to a power cut is a payment event nobody will ever send
again, so the throughput NORMAL buys is not worth it at webhook volumes.

`PRAGMA journal_mode=WAL` does not fail when it fails: it returns the mode it
left the database in, which on a filesystem that cannot hold a WAL is the old
one. So the mode is read back and a refusal raises. An in-memory database has
no file for a WAL — SQLite ignores the attempt — and every store in this
repository's tests, demo and arena is in memory, so there the mode stays
`memory` and `journal_mode` says so rather than pretending
(docs/EVALUATION.md §10, 2026-09-15).
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


IN_MEMORY = ":memory:"
SYNCHRONOUS_FULL = 2
"""`PRAGMA synchronous` reads back as an integer; FULL is 2."""


class JournalModeRefused(RuntimeError):
    """SQLite left a file-backed database out of WAL mode."""


def make_durable(conn: sqlite3.Connection) -> str:
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower()
    if mode != "wal":
        raise JournalModeRefused(
            f"PRAGMA journal_mode=WAL left the database in {mode!r} mode — this "
            "filesystem cannot hold a WAL (a network mount is the usual cause)"
        )
    conn.execute("PRAGMA synchronous=FULL")
    if conn.execute("PRAGMA synchronous").fetchone()[0] != SYNCHRONOUS_FULL:
        raise JournalModeRefused("PRAGMA synchronous=FULL did not take")
    return mode


class EventStore:
    def __init__(self, db_path: str | Path = IN_MEMORY) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        if str(db_path) == IN_MEMORY:
            self.journal_mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        else:
            self.journal_mode = make_durable(self._conn)
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
