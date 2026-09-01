"""The cassette store: a recorded response is the only response.

**Why this exists.** architectural invariant 5 says the same seed produces a
byte-identical ledger. An LLM cannot promise that — there is no temperature
setting, seed, or model flag that makes a provider deterministic, and claiming
otherwise would be the kind of unverified assertion docs/VERIFIED.md exists to
prevent. So determinism is bought twice over: the classifier never runs on any
path that writes a ledger (tests/test_shadow_boundary.py), and the comparison
itself replays recorded responses rather than re-asking.

**A miss is a hard failure.** The failure this rule prevents is not a
deliberate live call; it is an absent-minded one, three weeks from now, when a
prompt is edited by one character and the harness silently re-records against
a model that has since changed. Same reasoning as windtunnel/split.py's
unseal phrase: the threat is inattention, so the mechanism has to be loud.

**Provider-agnostic on purpose.** The store knows a provider name, a model
name, a prompt and a repeat index, and nothing else. Session 7 records one
Gemini arm; a second arm on another provider is a new client module and zero
changes here.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from windtunnel.cassette import CassetteMiss, CassetteStore, Request

PROVIDER = "fake"
MODEL = "fake-model-1"


def request(prompt: str = "classify this", repeat: int = 0) -> Request:
    return Request(provider=PROVIDER, model=MODEL, prompt=prompt, repeat=repeat)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> CassetteStore:
    return CassetteStore(tmp_path)


class TestReplayIsTheDefault:
    def test_a_recorded_response_replays(self, store):
        store.put(request(), "the response", label="a-cell")
        assert store.get(request()).response_text == "the response"

    def test_a_miss_raises_rather_than_returning_none(self, store):
        with pytest.raises(CassetteMiss):
            store.get(request())

    def test_a_miss_names_what_was_missing(self, store):
        """The message has to be actionable: someone hitting this needs to know
        which cell and repeat to re-record, not that a hash was absent."""
        store.put(request(), "recorded", label="a-cell")
        with pytest.raises(CassetteMiss) as excinfo:
            store.get(request(prompt="a prompt that was never recorded"))
        message = str(excinfo.value)
        assert PROVIDER in message and MODEL in message
        assert "--record" in message

    def test_a_one_character_prompt_change_misses(self, store):
        """The point of keying on the prompt rather than on a cell name: an
        edited prompt is a different question, and replaying yesterday's answer
        to it would silently report a number that was never measured."""
        store.put(request("classify this"), "recorded", label="a-cell")
        with pytest.raises(CassetteMiss):
            store.get(request("classify this."))

    def test_a_different_model_misses(self, store):
        store.put(request(), "recorded", label="a-cell")
        other = Request(provider=PROVIDER, model="fake-model-2", prompt="classify this", repeat=0)
        with pytest.raises(CassetteMiss):
            store.get(other)

    def test_a_different_provider_misses(self, store):
        store.put(request(), "recorded", label="a-cell")
        other = Request(provider="other", model=MODEL, prompt="classify this", repeat=0)
        with pytest.raises(CassetteMiss):
            store.get(other)


class TestRepeatsAreDistinctRecordings:
    """Repeats are how non-determinism gets measured instead of hidden. If the
    key ignored the repeat index, k repeats would collapse to one cassette and
    the self-consistency column would read 1.00 for a reason that had nothing
    to do with the model."""

    def test_repeats_of_one_prompt_are_separate_cassettes(self, store):
        store.put(request(repeat=0), "TRANSIENT", label="a-cell")
        store.put(request(repeat=1), "LIQUIDITY", label="a-cell")
        assert store.get(request(repeat=0)).response_text == "TRANSIENT"
        assert store.get(request(repeat=1)).response_text == "LIQUIDITY"
        assert store.count() == 2

    def test_an_unrecorded_repeat_misses_even_when_its_siblings_exist(self, store):
        store.put(request(repeat=0), "TRANSIENT", label="a-cell")
        with pytest.raises(CassetteMiss):
            store.get(request(repeat=1))

    def test_raising_the_repeat_count_reuses_what_was_already_recorded(self, store):
        """Why the repeat index is in the key rather than the recording being a
        list: if AI Studio shows a higher daily quota than the run was sized
        for, raising k records only the new repeats."""
        for repeat in range(3):
            store.put(request(repeat=repeat), f"r{repeat}", label="a-cell")
        already = [r for r in range(5) if store.has(request(repeat=r))]
        assert already == [0, 1, 2]


class TestKeys:
    def test_the_key_is_stable_across_store_instances(self, tmp_path):
        first = CassetteStore(tmp_path)
        first.put(request(), "recorded", label="a-cell")
        assert CassetteStore(tmp_path).get(request()).response_text == "recorded"

    def test_the_key_is_separator_delimited(self):
        """windtunnel/rng.py's reasoning, applied to a different address space:
        without a delimiter, ("ab", "c") and ("a", "bc") are the same key, and
        two things meant to be independent silently collide."""
        left = Request(provider="ab", model="c", prompt="p", repeat=0)
        right = Request(provider="a", model="bc", prompt="p", repeat=0)
        assert left.key != right.key

    def test_a_prompt_containing_the_delimiter_cannot_forge_another_key(self):
        forged = Request(provider=PROVIDER, model=MODEL, prompt="p|1|x", repeat=0)
        honest = Request(provider=PROVIDER, model=MODEL, prompt="p", repeat=0)
        assert forged.key != honest.key

    def test_the_key_is_not_the_python_hash(self):
        """hash() is salted per process, so an index built from it would replay
        differently tomorrow — the same reason windtunnel/rng.py uses sha256."""
        assert request().key != str(hash(request().prompt))
        assert len(request().key) == 64


class TestTheFileOnDisk:
    """A cassette is evidence, so it has to be readable by a person and
    reviewable in a diff."""

    def test_the_file_holds_the_whole_request_and_response(self, store, tmp_path):
        path = store.put(request(), "the response", label="a-cell")
        document = json.loads(path.read_text())
        assert document["provider"] == PROVIDER
        assert document["model"] == MODEL
        assert document["prompt"] == "classify this"
        assert document["repeat"] == 0
        assert document["response_text"] == "the response"
        assert document["key"] == request().key

    def test_the_filename_carries_the_label(self, store):
        path = store.put(request(), "the response", label="card_expired__bank")
        assert path.name.startswith("card_expired__bank")
        assert "r00" in path.name

    def test_a_hostile_label_cannot_escape_the_directory(self, store, tmp_path):
        """The label is for a human reading a directory listing; it is not a
        path. It arrives from a cell name today, and a cell name is derived
        from an error_reason off disk — so this is defence against a future
        caller, not against Razorpay."""
        path = store.put(request(), "x", label="../../etc/passwd")
        assert path.parent == tmp_path

    def test_the_label_is_not_part_of_the_key(self, store):
        """Relabelling a cell must not invalidate an expensive recording."""
        store.put(request(), "recorded", label="old-name")
        assert store.get(request()).response_text == "recorded"
        store.put(request(), "recorded", label="new-name")
        assert store.count() == 1

    def test_recording_time_is_injected_not_read_from_the_wall_clock(self, store):
        """architectural invariant 2. The store cannot call datetime.now() — it
        takes the instant from the caller, who holds the clock."""
        from datetime import datetime, timezone

        when = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        store.put(request(), "x", label="a-cell", recorded_at=when)
        assert store.get(request()).recorded_at == when

    def test_recording_time_is_optional(self, store):
        store.put(request(), "x", label="a-cell")
        assert store.get(request()).recorded_at is None


class TestTheStoreNeverCallsAnything:
    """The store is the whole of replay mode. It has no provider handle, no
    fallback, and no way to obtain a response it was not given."""

    def test_the_store_exposes_no_way_to_fetch_a_response(self, store):
        for attribute in ("client", "provider_client", "fetch", "call", "record_live"):
            assert not hasattr(store, attribute)

    def test_an_empty_directory_is_a_miss_not_an_empty_success(self, tmp_path):
        empty = CassetteStore(tmp_path / "nothing-here")
        assert empty.count() == 0
        with pytest.raises(CassetteMiss):
            empty.get(request())
