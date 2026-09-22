"""`SqlRetryIndex`: the retry index a restart cannot lose.

docs/EVALUATION.md §10, 2026-09-22. `RetryIndex` was process-local, so a
restart between a retry firing and its `payment.captured` arriving meant the
capture was not recognised as ours and reconciliation read the agent's own
successful retry as somebody else's payment. These tests hold the three
properties that close it: the mapping survives the process, it is durable the
way the event store is, and a payment id can only ever belong to one episode.
"""
from __future__ import annotations

import inspect

import pytest

from vasool.actions.executor import RetryIndex
from vasool.actions.retry_store import RetryIndexConflict, SqlRetryIndex


def test_the_mapping_survives_the_process(tmp_path):
    first = SqlRetryIndex(tmp_path / "retries.db")
    first.record("pay_1", "ent_1")
    first.close()
    second = SqlRetryIndex(tmp_path / "retries.db")
    try:
        assert second.entity_id_for("pay_1") == "ent_1"
        assert second.payment_ids() == frozenset({"pay_1"})
    finally:
        second.close()


def test_it_is_durable_the_way_the_event_store_is(tmp_path):
    index = SqlRetryIndex(tmp_path / "retries.db")
    try:
        assert index.journal_mode == "wal"
    finally:
        index.close()


def test_recording_the_same_pair_twice_is_harmless(tmp_path):
    index = SqlRetryIndex(tmp_path / "retries.db")
    try:
        index.record("pay_1", "ent_1")
        index.record("pay_1", "ent_1")
        assert index.payment_ids() == frozenset({"pay_1"})
    finally:
        index.close()


def test_a_payment_id_cannot_change_owner(tmp_path):
    """The rail minted the id for a debit this agent asked for; a second owner
    is a bug, and overwriting would settle one episode's money onto another."""
    index = SqlRetryIndex(tmp_path / "retries.db")
    try:
        index.record("pay_1", "ent_1")
        with pytest.raises(RetryIndexConflict):
            index.record("pay_1", "ent_2")
        assert index.entity_id_for("pay_1") == "ent_1"
    finally:
        index.close()


def test_an_unknown_payment_is_not_ours(tmp_path):
    index = SqlRetryIndex(tmp_path / "retries.db")
    try:
        assert index.entity_id_for("pay_never") is None
    finally:
        index.close()


def test_it_refuses_to_be_an_in_memory_index():
    with pytest.raises(ValueError):
        SqlRetryIndex(":memory:")


def test_it_offers_exactly_what_the_in_memory_index_offers():
    """Anything that reads a RetryIndex — the receiver, settlement,
    reconciliation — must be able to read this one unchanged."""
    public = lambda cls: {name for name, _ in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")}
    assert public(RetryIndex) <= public(SqlRetryIndex)
