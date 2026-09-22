"""`RetryIndex`, on disk.

docs/EVALUATION.md §10, 2026-09-22 closes the gap `RetryIndex`'s own docstring
named: the index is process-local, so a restart between a retry firing and its
`payment.captured` arriving loses the mapping, and that capture is not
recognised as ours. The consequence was the conservative one — nothing settled
that should not have — but a deployment that restarts, as every deployment
does, left episodes in AWAITING that had in fact recovered, and reconciliation
read the agent's own successful retry as somebody else's payment.

**Why here and not in the FactStore.** `vasool/policy/sql_store.py` is a
policy-plane store, and `vasool/ledger/receipts.py` establishes that
Razorpay-shaped data — a request id, a response body, a payment id the rail
minted — never enters the policy plane. This mapping is exactly that kind of
data: a record of calls the action plane made. So it is persisted in the action
plane, in a database of its own, with the same interface the in-memory index
has, and the policy plane never sees it. (§10's row said "persisted in the
store"; the location differs from that wording and the result row says so. The
gap it closes is the same one.)

**Durable the way the event store is durable.** WAL with `synchronous=FULL`,
read back rather than assumed (`vasool/events/store.py::make_durable`), because
an index that loses its last write on power loss has the restart gap back.

**A payment id belongs to one entity.** Recording the same pair twice is a
no-op; recording a payment id against a *different* entity refuses. The rail
mints the id for the debit this agent asked for, so a second owner can only be
a bug, and silently overwriting it would settle one episode's money onto
another.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from vasool.events.store import make_durable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS retry_index (
    payment_id TEXT PRIMARY KEY,
    entity_id  TEXT NOT NULL
)
"""


class RetryIndexConflict(ValueError):
    """A payment id was recorded against a second entity."""


class SqlRetryIndex:
    """The `RetryIndex` interface — `record`, `entity_id_for`, `payment_ids` —
    over a file that outlives the process."""

    def __init__(self, db_path: str | Path) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("SqlRetryIndex is the durable index; use RetryIndex for an in-memory one")
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.journal_mode = make_durable(self._conn)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def record(self, payment_id: str, entity_id: str) -> None:
        with self._conn:
            existing = self._conn.execute(
                "SELECT entity_id FROM retry_index WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != entity_id:
                    raise RetryIndexConflict(
                        f"{payment_id} is already recorded against {existing[0]}, not {entity_id}"
                    )
                return
            self._conn.execute(
                "INSERT INTO retry_index (payment_id, entity_id) VALUES (?, ?)", (payment_id, entity_id)
            )

    def entity_id_for(self, payment_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT entity_id FROM retry_index WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        return row[0] if row else None

    def payment_ids(self) -> frozenset[str]:
        return frozenset(row[0] for row in self._conn.execute("SELECT payment_id FROM retry_index"))

    def close(self) -> None:
        self._conn.close()
