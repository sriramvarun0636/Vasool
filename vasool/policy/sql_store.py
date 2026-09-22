"""The `FactStore` a deployment reads, and the one the simulator replaces.

`vasool/policy/facts.py` has said since it was written that the store is
"SQLite in production, a dict in the simulator". `windtunnel/world.py` is the
dict. This is the other one, and until now it did not exist: every number this
repository publishes comes from the simulator's store, and a real deployment
had nothing to read.

**Why SQLite rather than Postgres** (docs/EVALUATION.md §10, 2026-09-21). CI
runs the whole suite from a fresh clone with nothing configured, which is the
property INC-007 exists to protect, and a Postgres path could not be exercised
there without a service. An unexercised persistence layer that a README calls
production-ready is exactly the unverified claim this repository forbids. The
limit that buys is stated rather than hidden: **one writer, one machine**. What
makes a second writer safe is not this module but `vasool/policy/partition.py`
— ownership keyed on `identity_id`, so two workers never hold one human's
episodes — and on Postgres that becomes a row lock rather than a redesign.

**One read, not fifteen.** `FactStore.snapshot` is deliberately a single
method: the chain must rule on one consistent view of the world, not on fifteen
queries taken at fifteen slightly different moments. Every query below runs
inside one transaction opened with `BEGIN IMMEDIATE`, so a writer cannot land
between the contact history and the spend counter and hand two guards facts
that never held simultaneously.

**Absence is a fact here, and the schema says which kind.** `PolicyFacts`
distinguishes *known-absent* (an empty history means nobody has been messaged)
from *unknown* (`dnd_listed = None` means the registry could not be reached,
and `DNDGuard` fails closed on it). A store that returned `False` for a DND
lookup it never made would turn the second into the first silently, which is
the failure §2a's DND claim rests on not happening. So a row that has never
been written reads as `None`, and only a recorded scrub reads as a boolean.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from vasool.diagnosis.proposal import Proposal
from vasool.events.schemas import FailureEvent
from vasool.mandate.record import MandateRecord
from vasool.mandate.record import MandateCategory, MandateRail
from vasool.mandate.states import MandateState
from vasool.policy.facts import (
    ConsentRecord,
    MerchantPolicy,
    MessageCategory,
    PolicyFacts,
)

IN_MEMORY = ":memory:"

FREQUENCY_CAP_WINDOW = timedelta(days=7)
"""What `FrequencyCapGuard` counts over. Read here so the store hands the guard
the window it documents rather than every contact ever sent."""

IST = ZoneInfo("Asia/Kolkata")
"""The merchant's day, for the spend cap. RBI's rules are stated in IST and the
cap is a per-day figure, so the day boundary is IST's, not the server's."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    identity_id   TEXT PRIMARY KEY,
    consent_json  TEXT,
    dnd_listed    INTEGER,
    dnd_checked_at TEXT,
    zone          TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    identity_id  TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    template_id  TEXT
);
CREATE INDEX IF NOT EXISTS contacts_by_identity ON contacts (identity_id, sent_at);
CREATE TABLE IF NOT EXISTS executions (
    idempotency_key TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL,
    executed_at     TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL DEFAULT 0,
    is_retry        INTEGER NOT NULL DEFAULT 0,
    merchant_id     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS executions_by_entity ON executions (entity_id);
CREATE TABLE IF NOT EXISTS notices (
    entity_id TEXT PRIMARY KEY,
    sent_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mandates (
    customer_id TEXT PRIMARY KEY,
    record_json TEXT
);
CREATE TABLE IF NOT EXISTS promises (
    entity_id TEXT PRIMARY KEY,
    pay_on    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    template_id TEXT PRIMARY KEY,
    category    TEXT NOT NULL
);
"""


class SqlFactStore:
    """A `FactStore` over SQLite. One snapshot, one transaction.

    `merchant` is passed in rather than read from a table: a `MerchantPolicy`
    is configuration — the caps and the declared templates the merchant owns —
    and configuration belongs to the composition root, not to a row somebody
    could edit underneath a running chain.
    """

    def __init__(
        self,
        db_path: str | Path = IN_MEMORY,
        *,
        merchant: MerchantPolicy,
        identity_of=None,
    ) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._merchant = merchant
        # How a customer id becomes the human the cap counts. Defaults to the
        # identity of the record itself, which is what the system did before
        # vasool/identity/ existed and what `NullIdentityResolver` answers —
        # wiring a resolver is a decision the composition root takes.
        self._identity_of = identity_of or (lambda customer_id: customer_id)

    # -- the port ---------------------------------------------------------
    def snapshot(
        self, *, event: FailureEvent, proposal: Proposal, now: datetime
    ) -> PolicyFacts:
        """One consistent read of everything the chain may look at."""
        identity = self._identity_of(event.customer_id)
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            customer = conn.execute(
                "SELECT consent_json, dnd_listed, dnd_checked_at, zone"
                " FROM customers WHERE identity_id = ?",
                (identity,),
            ).fetchone()
            opens = (now - FREQUENCY_CAP_WINDOW).isoformat()
            history = tuple(
                datetime.fromisoformat(row[0])
                for row in conn.execute(
                    "SELECT sent_at FROM contacts WHERE identity_id = ? AND sent_at >= ?"
                    " ORDER BY sent_at",
                    (identity, opens),
                )
            )
            keys = frozenset(
                row[0]
                for row in conn.execute(
                    "SELECT idempotency_key FROM executions WHERE entity_id = ?",
                    (event.entity_id,),
                )
            )
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount_paise), 0) FROM executions"
                " WHERE merchant_id = ? AND is_retry = 1 AND executed_at >= ? AND executed_at < ?",
                (proposal.merchant_id, *_ist_day_bounds(now)),
            ).fetchone()[0]
            notice = conn.execute(
                "SELECT sent_at FROM notices WHERE entity_id = ?", (event.entity_id,)
            ).fetchone()
            mandate = conn.execute(
                "SELECT record_json FROM mandates WHERE customer_id = ?",
                (event.customer_id,),
            ).fetchone()
            promise = conn.execute(
                "SELECT pay_on FROM promises WHERE entity_id = ?", (event.entity_id,)
            ).fetchone()
            categories = frozenset(
                (row[0], MessageCategory(row[1]))
                for row in conn.execute("SELECT template_id, category FROM templates")
            )
        finally:
            conn.commit()

        consent_json, dnd_listed, dnd_checked_at, zone = customer or (None, None, None, None)
        return PolicyFacts(
            merchant=self._merchant,
            executed_keys=keys,
            spent_today_paise=int(spent),
            # attempts_used and episode_contacts are left at their defaults:
            # PolicyMachine._context overwrites both from the episode's own
            # counters, and a store that guessed at them would be overwritten
            # silently or, worse, believed.
            contact_history=history,
            identity_id=identity,
            consent=_consent_from_json(consent_json),
            dnd_listed=None if dnd_listed is None else bool(dnd_listed),
            dnd_checked_at=datetime.fromisoformat(dnd_checked_at) if dnd_checked_at else None,
            promise_to_pay=date.fromisoformat(promise[0]) if promise else None,
            customer_zone=ZoneInfo(zone) if zone else None,
            # Three values, not two: a row carrying a record is a mandate; a row
            # carrying NULL is a recorded one-time customer; no row at all is a
            # customer this store cannot speak for, and MandateStateGuard fails
            # closed on it (docs/EVALUATION.md §10, 2026-09-22).
            mandate=_mandate_from_json(mandate[0]) if mandate and mandate[0] else None,
            mandate_unknown=mandate is None,
            pre_debit_notice_sent_at=datetime.fromisoformat(notice[0]) if notice else None,
            registered_templates=frozenset(t for t, _ in categories),
            template_categories=categories,
        )

    # -- what the executor records ----------------------------------------
    def record_execution(self, proposal: Proposal, *, at: datetime) -> None:
        """Write what an execution changes about the world.

        Called at the moment an action lands, before anything else can be
        gated on it: three guards read history this store owns rather than
        history the episode owns, and `PreDebitNoticeGuard` in particular
        blocks forever if the notice it is waiting for is never recorded —
        which is INC-002, and the reason this method exists at all.
        """
        conn = self._conn
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO executions"
                " (idempotency_key, entity_id, executed_at, amount_paise, is_retry, merchant_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    proposal.idempotency_key,
                    proposal.entity_id,
                    at.isoformat(),
                    proposal.amount_paise if proposal.is_retry else 0,
                    1 if proposal.is_retry else 0,
                    proposal.merchant_id,
                ),
            )
            if proposal.is_contact:
                conn.execute(
                    "INSERT INTO contacts (identity_id, sent_at, entity_id, template_id)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        self._identity_of(proposal.customer_id),
                        at.isoformat(),
                        proposal.entity_id,
                        getattr(proposal, "template_id", None),
                    ),
                )
            if proposal.role.value == "PRE_DEBIT_NOTICE":
                conn.execute(
                    "INSERT OR REPLACE INTO notices (entity_id, sent_at) VALUES (?, ?)",
                    (proposal.entity_id, at.isoformat()),
                )

    # -- what a deployment writes before the chain ever runs ---------------
    def upsert_customer(
        self,
        identity_id: str,
        *,
        consent: ConsentRecord | None = None,
        dnd_listed: bool | None = None,
        dnd_checked_at: datetime | None = None,
        zone: str | None = None,
    ) -> None:
        """Record what is known about a human. Absent stays absent: a customer
        never scrubbed against the DND registry reads `None`, which
        `DNDGuard` fails closed on, rather than `False`."""
        with self._conn as conn:
            conn.execute(
                "INSERT INTO customers (identity_id, consent_json, dnd_listed, dnd_checked_at, zone)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(identity_id) DO UPDATE SET"
                "   consent_json = excluded.consent_json,"
                "   dnd_listed = excluded.dnd_listed,"
                "   dnd_checked_at = excluded.dnd_checked_at,"
                "   zone = excluded.zone",
                (
                    identity_id,
                    _consent_to_json(consent),
                    None if dnd_listed is None else int(dnd_listed),
                    dnd_checked_at.isoformat() if dnd_checked_at else None,
                    zone,
                ),
            )

    def upsert_mandate(self, customer_id: str, record: MandateRecord) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT OR REPLACE INTO mandates (customer_id, record_json) VALUES (?, ?)",
                (customer_id, _mandate_to_json(record)),
            )

    def record_no_mandate(self, customer_id: str) -> None:
        """Record that this customer's payments are one-time — known, and absent.
        Without this or `upsert_mandate`, the store cannot say, and says so."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO mandates (customer_id, record_json) VALUES (?, NULL)",
                (customer_id,),
            )

    def record_promise(self, entity_id: str, pay_on: date) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT OR REPLACE INTO promises (entity_id, pay_on) VALUES (?, ?)",
                (entity_id, pay_on.isoformat()),
            )

    def declare_template(self, template_id: str, category: MessageCategory) -> None:
        """The merchant declaring how a template is registered on DLT. Only the
        merchant knows, which is why an undeclared template is judged rather
        than assumed transactional (§10, 2026-09-15)."""
        with self._conn as conn:
            conn.execute(
                "INSERT OR REPLACE INTO templates (template_id, category) VALUES (?, ?)",
                (template_id, category.value),
            )

    def close(self) -> None:
        self._conn.close()


def _ist_day_bounds(now: datetime) -> tuple[str, str]:
    """The IST day `now` falls in, as ISO strings for comparison.

    Both bounds are returned in the same form the rows are stored in, so the
    comparison is a string range over an indexed column rather than a function
    applied to every row.
    """
    local = now.astimezone(IST)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        start.astimezone(timezone.utc).isoformat(),
        (start + timedelta(days=1)).astimezone(timezone.utc).isoformat(),
    )


# -- serialisation ---------------------------------------------------------
#
# `ConsentRecord` and `MandateRecord` are dataclasses, not pydantic models, so
# there is no `model_dump_json` to lean on. These four functions are the whole
# of the mapping, written out rather than generated, because a field silently
# dropped here is a fact the chain never sees: a missing `withdrawn_at` reads
# as consent still standing, and a missing `paused_until` reads as a mandate
# that may be debited.


def _consent_to_json(consent: ConsentRecord | None) -> str | None:
    if consent is None:
        return None
    return json.dumps(
        {
            "granted_at": consent.granted_at.isoformat(),
            "purposes": sorted(consent.purposes),
            "withdrawn_at": consent.withdrawn_at.isoformat() if consent.withdrawn_at else None,
        }
    )


def _consent_from_json(blob: str | None) -> ConsentRecord | None:
    if not blob:
        return None
    raw = json.loads(blob)
    return ConsentRecord(
        granted_at=datetime.fromisoformat(raw["granted_at"]),
        purposes=frozenset(raw["purposes"]),
        withdrawn_at=(
            datetime.fromisoformat(raw["withdrawn_at"]) if raw["withdrawn_at"] else None
        ),
    )


def _mandate_to_json(record: MandateRecord) -> str:
    return json.dumps(
        {
            "mandate_id": record.mandate_id,
            "rail": record.rail.value,
            "category": record.category.value,
            "state": record.state.value,
            "valid_until": record.valid_until.isoformat() if record.valid_until else None,
            "revocable_by_payer": record.revocable_by_payer,
            "paused_until": record.paused_until.isoformat() if record.paused_until else None,
            "token_id": record.token_id,
            "razorpay_customer_id": record.razorpay_customer_id,
        }
    )


def _mandate_from_json(blob: str) -> MandateRecord:
    raw = json.loads(blob)
    return MandateRecord(
        mandate_id=raw["mandate_id"],
        rail=MandateRail(raw["rail"]),
        category=MandateCategory(raw["category"]),
        state=MandateState(raw["state"]),
        valid_until=datetime.fromisoformat(raw["valid_until"]) if raw["valid_until"] else None,
        revocable_by_payer=raw["revocable_by_payer"],
        paused_until=(
            datetime.fromisoformat(raw["paused_until"]) if raw["paused_until"] else None
        ),
        token_id=raw["token_id"],
        razorpay_customer_id=raw["razorpay_customer_id"],
    )
