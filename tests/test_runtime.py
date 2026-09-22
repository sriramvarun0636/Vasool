"""The production tranche: a real store, a composition root, and a driver.

docs/EVALUATION.md §10, 2026-09-21. Everything this repository publishes was
measured through the simulator's fact store; a deployment had nothing to read,
nothing to wire it, and nothing to call `tick()`. These tests cover the three
modules that close that, and they are written against the properties that would
be dangerous to get wrong rather than against the happy path:

  - the store distinguishes *unknown* from *known-absent*, because `DNDGuard`
    fails closed on the first and permits on the second;
  - the composition root refuses to boot on a pepper that would make the
    ledger unjoinable or the pseudonymisation public;
  - the driver's partition really excludes, and an episode nobody owns is
    impossible rather than merely unlikely.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vasool.diagnosis.proposal import MessageCategory
from vasool.mandate.record import MandateCategory, MandateRail, MandateRecord
from vasool.mandate.states import MandateState
from vasool.policy.facts import ConsentRecord, MerchantPolicy
from vasool.policy.sql_store import IN_MEMORY, SqlFactStore
from vasool.runtime.composition import (
    MIN_PEPPER_LENGTH,
    PEPPER_VAR,
    PUBLIC_TEST_PEPPER,
    StartupRefused,
    build,
    read_pepper,
)
from vasool.runtime.driver import Driver
from tests.payloads import event_for

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
MERCHANT = MerchantPolicy(
    merchant_id="acc_test",
    daily_retry_cap_paise=50_000_000,
    human_approval_threshold_paise=5_000_000,
    kill_switch=False,
)
GOOD_PEPPER = "a" * MIN_PEPPER_LENGTH


def _store(**kwargs) -> SqlFactStore:
    return SqlFactStore(IN_MEMORY, merchant=MERCHANT, **kwargs)


def _snapshot(store: SqlFactStore, *, now: datetime = NOW):
    """One snapshot for a card_expired failure, which is the cheapest event
    that reaches every table this store reads."""
    event = event_for("card_expired")
    proposal = _proposal().model_copy(
        update={"entity_id": event.entity_id, "customer_id": event.customer_id,
                "merchant_id": MERCHANT.merchant_id}
    )
    return store.snapshot(event=event, proposal=proposal, now=now)


def _proposal(event=None, **overrides):
    """The real proposal for a `card_expired` row, built the way the rest of
    the suite builds one — the store reads its `merchant_id`, `entity_id` and
    the three role flags, so a hand-made stand-in would test the stand-in."""
    from tests.policy.strategies import proposal_for

    proposal = proposal_for("card_expired", now=NOW)
    return proposal.model_copy(update=overrides) if overrides else proposal


class TestTheStoreDistinguishesUnknownFromAbsent:
    """The distinction §2a's DND claim rests on.

    `dnd_listed=None` means nobody asked the registry and `DNDGuard` blocks;
    `False` means it answered. A store that returned `False` for a lookup it
    never made would convert a fail-closed guard into a permit, silently, for
    every customer it had no row for.
    """

    def test_a_customer_nobody_has_scrubbed_reads_unknown(self):
        facts = _snapshot(_store())
        assert facts.dnd_listed is None
        assert facts.dnd_checked_at is None

    def test_a_recorded_scrub_reads_as_a_boolean(self):
        store = _store()
        event = event_for("card_expired")
        store.upsert_customer(event.customer_id, dnd_listed=False, dnd_checked_at=NOW)
        facts = _snapshot(store)
        assert facts.dnd_listed is False
        assert facts.dnd_checked_at == NOW

    def test_an_empty_contact_history_is_known_absent(self):
        """Empty means "we have messaged nobody", which is a fact, not a gap —
        the frequency cap is entitled to act on it."""
        assert _snapshot(_store()).contact_history == ()


class TestTheStoreAnswersWhatTheGuardsCount:
    def test_contacts_outside_the_window_are_not_counted(self):
        store = _store()
        event = event_for("card_expired")
        proposal = _proposal().model_copy(update={"entity_id": event.entity_id})
        contact = proposal
        # Two contacts: one inside FrequencyCapGuard's seven days, one outside.
        for at, key in ((NOW - timedelta(days=2), "k1"), (NOW - timedelta(days=30), "k2")):
            store.record_execution(
                _contact_like(contact, key=key, customer_id=event.customer_id), at=at
            )
        history = _snapshot(store).contact_history
        assert len(history) == 1 and history[0] == NOW - timedelta(days=2)

    def test_the_spend_cap_counts_the_merchants_ist_day_only(self):
        """The cap is per day and the day is IST's: a retry at 20:00 UTC is
        already tomorrow in Mumbai, and counting it against today would block
        work the rule allows."""
        store = _store()
        event = event_for("card_expired")
        retry = _retry_like(_proposal().model_copy(update={'entity_id': event.entity_id}), key="r1", amount=10_000)
        store.record_execution(retry, at=NOW)
        store.record_execution(
            _retry_like(_proposal().model_copy(update={'entity_id': event.entity_id}), key="r2", amount=99_000),
            at=NOW - timedelta(days=1),
        )
        assert _snapshot(store).spent_today_paise == 10_000

    def test_an_execution_is_idempotent_on_its_key(self):
        """The same key recorded twice is one execution. `IdempotencyGuard`
        reads this set, and a duplicate row would not change its answer — but
        a duplicate *contact* row would change the cap's."""
        store = _store()
        event = event_for("card_expired")
        retry = _retry_like(_proposal().model_copy(update={'entity_id': event.entity_id}), key="same", amount=1_000)
        store.record_execution(retry, at=NOW)
        store.record_execution(retry, at=NOW)
        facts = _snapshot(store)
        assert facts.executed_keys == {"same"}
        assert facts.spent_today_paise == 1_000

    def test_a_notice_is_recorded_where_the_guard_looks_for_it(self):
        """INC-002: the guard defers until a notice exists, and the only thing
        that could satisfy it was an execution it was blocking. The store has
        to record the notice at the moment it is sent."""
        store = _store()
        event = event_for("card_expired")
        notice = _notice_like(_proposal().model_copy(update={'entity_id': event.entity_id}))
        assert _snapshot(store).pre_debit_notice_sent_at is None
        store.record_execution(notice, at=NOW)
        assert _snapshot(store).pre_debit_notice_sent_at == NOW

    def test_the_records_survive_a_round_trip_through_sqlite(self):
        """Consent and the mandate are dataclasses hand-serialised into JSON.
        A field dropped there is a fact the chain never sees — a lost
        `withdrawn_at` reads as consent still standing."""
        store = _store()
        event = event_for("card_expired")
        consent = ConsentRecord(
            granted_at=NOW - timedelta(days=100),
            purposes=frozenset({"payment_recovery"}),
            withdrawn_at=NOW - timedelta(days=1),
        )
        mandate = MandateRecord(
            mandate_id="mand_1",
            rail=MandateRail.UPI_AUTOPAY,
            category=MandateCategory.GENERAL,
            state=MandateState.PAUSED,
            valid_until=NOW + timedelta(days=200),
            revocable_by_payer=True,
            paused_until=NOW + timedelta(days=3),
            token_id="tok_1",
            razorpay_customer_id="cust_1",
        )
        store.upsert_customer(event.customer_id, consent=consent)
        store.upsert_mandate(event.customer_id, mandate)
        facts = _snapshot(store)
        assert facts.consent == consent
        assert facts.mandate == mandate

    def test_a_template_is_unknown_until_the_merchant_declares_it(self):
        """Only the merchant knows how a template is registered on DLT, which
        is why an undeclared one is judged rather than assumed transactional
        (§10, 2026-09-15)."""
        store = _store()
        assert _snapshot(store).template_categories == frozenset()
        store.declare_template("tpl_1", MessageCategory.TRANSACTIONAL)
        facts = _snapshot(store)
        assert facts.registered_templates == frozenset({"tpl_1"})
        assert facts.template_categories == frozenset({("tpl_1", MessageCategory.TRANSACTIONAL)})


class TestTheCompositionRootRefusesBadPeppers:
    def test_an_absent_pepper_refuses(self):
        with pytest.raises(StartupRefused, match="not set"):
            read_pepper({})

    def test_the_public_test_pepper_refuses(self):
        """It is printed in this repository. A deployment under it derives the
        same customer_id as every reader's laptop."""
        with pytest.raises(StartupRefused, match="public test pepper"):
            read_pepper({PEPPER_VAR: PUBLIC_TEST_PEPPER})

    def test_a_short_pepper_refuses(self):
        with pytest.raises(StartupRefused, match="floor"):
            read_pepper({PEPPER_VAR: "short"})

    def test_a_real_pepper_is_returned_stripped(self):
        assert read_pepper({PEPPER_VAR: f"  {GOOD_PEPPER}  "}) == GOOD_PEPPER

    def test_a_refusal_builds_nothing(self, tmp_path):
        """The check runs before anything is constructed, so a refused start
        leaves no database file behind to be found later and trusted."""
        db = tmp_path / "facts.db"
        with pytest.raises(StartupRefused):
            build(
                db_path=db,
                events_path=tmp_path / "events.db",
                merchant=MERCHANT,
                environ={},
            )
        assert not db.exists()

    def test_a_built_runtime_has_no_executor_unless_one_is_passed(self, tmp_path):
        """Dispatching money is the one thing that must never be wired by
        accident."""
        runtime = build(
            db_path=tmp_path / "f.db",
            events_path=tmp_path / "e.db",
            merchant=MERCHANT,
            environ={PEPPER_VAR: GOOD_PEPPER},
        )
        try:
            assert runtime.machine.executor is None
            assert runtime.pepper == GOOD_PEPPER
        finally:
            runtime.close()


class TestTheDriverPartitions:
    def _runtime(self, tmp_path):
        return build(
            db_path=tmp_path / "f.db",
            events_path=tmp_path / "e.db",
            merchant=MERCHANT,
            environ={PEPPER_VAR: GOOD_PEPPER},
        )

    def test_every_identity_has_exactly_one_owner(self, tmp_path):
        runtime = self._runtime(tmp_path)
        try:
            drivers = [Driver(runtime, worker=i, workers=4) for i in range(4)]
            for identity in (f"{i:064x}" for i in range(200)):
                assert sum(d.owns_episode(identity) for d in drivers) == 1
        finally:
            runtime.close()

    def test_an_unresolved_episode_is_owned_rather_than_dropped(self, tmp_path):
        """Nobody owning it is the silent-stall failure INC-002 was."""
        runtime = self._runtime(tmp_path)
        try:
            drivers = [Driver(runtime, worker=i, workers=3) for i in range(3)]
            assert sum(d.owns_episode(None) for d in drivers) == 1
        finally:
            runtime.close()

    def test_a_partition_it_cannot_be_part_of_is_refused(self, tmp_path):
        runtime = self._runtime(tmp_path)
        try:
            for worker, workers in ((0, 0), (3, 3), (-1, 2)):
                with pytest.raises(ValueError):
                    Driver(runtime, worker=worker, workers=workers)
        finally:
            runtime.close()

    def test_the_loop_stops_when_it_is_told_to_and_counts_its_passes(self, tmp_path):
        """`run_forever` takes its sleep and its stop condition as arguments
        for the same reason every clock here is injected: a test must not have
        to wait a minute to see a second pass."""
        runtime = self._runtime(tmp_path)
        try:
            driver = Driver(runtime, interval_seconds=0)
            slept: list[float] = []
            passes = driver.run_forever(
                sleep=slept.append, until=lambda: driver.ticks >= 3
            )
            assert passes == 3 and driver.ticks == 3
            assert len(slept) <= passes
        finally:
            runtime.close()


# -- helpers ---------------------------------------------------------------
#
# The store records what a Proposal says it is, so these build the three shapes
# it branches on without going through the whole diagnosis plane.


def _contact_like(proposal, *, key: str, customer_id: str):
    return _Fake(
        idempotency_key=key,
        entity_id=proposal.entity_id,
        customer_id=customer_id,
        merchant_id=proposal.merchant_id,
        amount_paise=0,
        is_retry=False,
        is_contact=True,
        role=_Role("MESSAGE"),
        template_id=None,
    )


def _retry_like(proposal, *, key: str, amount: int):
    return _Fake(
        idempotency_key=key,
        entity_id=proposal.entity_id,
        customer_id=proposal.customer_id,
        merchant_id=MERCHANT.merchant_id,
        amount_paise=amount,
        is_retry=True,
        is_contact=False,
        role=_Role("RETRY"),
        template_id=None,
    )


def _notice_like(proposal):
    return _Fake(
        idempotency_key="notice",
        entity_id=proposal.entity_id,
        customer_id=proposal.customer_id,
        merchant_id=MERCHANT.merchant_id,
        amount_paise=0,
        is_retry=False,
        is_contact=False,
        role=_Role("PRE_DEBIT_NOTICE"),
        template_id=None,
    )


class _Role:
    def __init__(self, value: str) -> None:
        self.value = value


class _Fake:
    """The fields `record_execution` reads, and nothing else."""

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)


class TestTheMandateIsThreeValued:
    """docs/EVALUATION.md §10, 2026-09-22. `mandate = None` used to be the
    answer both for "a one-time payment" and for "the store could not find one",
    and the second read as the first — every mandate guard lost jurisdiction on
    exactly the payments nobody could vouch for."""

    def test_a_customer_with_no_mandate_row_reads_unknown(self):
        facts = _snapshot(_store())
        assert facts.mandate is None and facts.mandate_unknown is True

    def test_a_recorded_one_time_customer_is_known_absent(self):
        store = _store()
        store.record_no_mandate(event_for("card_expired").customer_id)
        facts = _snapshot(store)
        assert facts.mandate is None and facts.mandate_unknown is False

    def test_a_recorded_mandate_is_known(self):
        store = _store()
        record = MandateRecord(mandate_id="mand_2", rail=MandateRail.CARD, category=MandateCategory.GENERAL,
                               state=MandateState.ACTIVE, valid_until=NOW + timedelta(days=30))
        store.upsert_mandate(event_for("card_expired").customer_id, record)
        facts = _snapshot(store)
        assert facts.mandate == record and facts.mandate_unknown is False

    def test_an_unknown_mandate_blocks_a_retry_and_nothing_else(self):
        from vasool.policy.facts import GuardContext
        from vasool.policy.guards.mandate_state import MandateStateGuard
        from vasool.policy.verdict import Decision
        from tests.policy.strategies import proposal_for

        facts = _snapshot(_store())
        guard = MandateStateGuard()
        retry = proposal_for("gateway_technical_error", now=NOW)
        link = proposal_for("card_expired", now=NOW)
        blocked = guard.evaluate(GuardContext(now=NOW, effective_at=NOW, event=event_for("gateway_technical_error"),
                                              proposal=retry, facts=facts))
        assert blocked.decision is Decision.BLOCK and "cannot tell" in blocked.reason
        untouched = guard.evaluate(GuardContext(now=NOW, effective_at=NOW, event=event_for("card_expired"),
                                                proposal=link, facts=facts))
        assert untouched.decision is Decision.NOT_APPLICABLE


class TestTheRetryIndexIsShared:
    """docs/EVALUATION.md §10, 2026-09-22. The executor writes the index, the
    receiver reads it, reconciliation subtracts it — one object, or the restart
    gap sits between whichever two disagree."""

    def _build(self, tmp_path, **kwargs):
        return build(db_path=tmp_path / "f.db", events_path=tmp_path / "e.db", merchant=MERCHANT,
                     environ={PEPPER_VAR: GOOD_PEPPER}, **kwargs)

    def test_an_executor_without_a_durable_index_refuses(self, tmp_path):
        from types import SimpleNamespace
        from vasool.actions.executor import RetryIndex
        with pytest.raises(StartupRefused, match="without a durable retry index"):
            self._build(tmp_path, executor=SimpleNamespace(retry_index=RetryIndex()))

    def test_an_executor_holding_a_different_index_refuses(self, tmp_path):
        from types import SimpleNamespace
        from vasool.actions.retry_store import SqlRetryIndex
        mine, theirs = SqlRetryIndex(tmp_path / "r.db"), SqlRetryIndex(tmp_path / "other.db")
        try:
            with pytest.raises(StartupRefused, match="different retry indexes"):
                self._build(tmp_path, executor=SimpleNamespace(retry_index=theirs), retries=mine)
        finally:
            mine.close()
            theirs.close()

    def test_the_shared_index_is_what_reconciliation_subtracts(self, tmp_path):
        from types import SimpleNamespace
        from vasool.actions.retry_store import SqlRetryIndex
        retries = SqlRetryIndex(tmp_path / "r.db")
        runtime = self._build(tmp_path, executor=SimpleNamespace(retry_index=retries), retries=retries)
        try:
            retries.record("pay_ours", "ent_1")
            assert runtime.retries is retries
            assert runtime.machine._ours() == frozenset({"pay_ours"})
        finally:
            runtime.close()
