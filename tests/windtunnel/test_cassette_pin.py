"""The recorded model is pinned, and the pin is checked against the disk.

**Why this file exists.** A cassette's address includes the model that produced
it (windtunnel/cassette.py::Request.key), so changing the model is not a
configuration change — it invalidates every recording at once. Every cassette
in this repository was recorded on one model, against a free tier whose
observed allowance is twenty requests per day. Re-recording the twelve-cell
corpus after a casual model bump therefore costs **a fresh day**, and it is
exactly the kind of edit that looks harmless in a diff: one string, in a
constant that used to be called a default.

So the pin is not a comment asking for care. It is a constant with a test
behind it, and the test reads the cassettes actually on disk rather than
trusting the constant to describe them. A future session that edits
`PINNED_MODEL` gets a red test naming the cost, before it spends the day.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from windtunnel.shadow import PINNED_MODEL, PINNED_PROVIDER

CASSETTE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "cassettes"


def cassettes() -> list[dict]:
    if not CASSETTE_DIR.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(CASSETTE_DIR.glob("*.json"))]


class TestThePinIsReal:
    def test_the_pin_names_a_model(self):
        assert PINNED_MODEL and isinstance(PINNED_MODEL, str)
        assert PINNED_PROVIDER

    def test_the_pin_is_not_a_family_or_an_alias(self):
        """`gemini-flash-latest` would silently re-point the corpus at a new
        model without any edit at all, and every cassette would miss on a day
        nobody chose. The pin has to name a specific version."""
        assert "latest" not in PINNED_MODEL
        assert PINNED_MODEL.count("-") >= 2, PINNED_MODEL


class TestTheDiskAgreesWithThePin:
    """The half that cannot be satisfied by editing a constant."""

    def test_every_cassette_was_recorded_on_the_pinned_model(self):
        recorded = cassettes()
        if not recorded:
            pytest.skip("no cassettes recorded yet")
        wrong = sorted({c["model"] for c in recorded} - {PINNED_MODEL})
        assert not wrong, (
            f"cassettes on disk were recorded on {wrong}, but PINNED_MODEL is "
            f"{PINNED_MODEL!r}. Changing the pin orphans every recording — the "
            "model is part of the cassette key — and re-recording the corpus "
            "costs a fresh day against a 20-request free-tier quota. Either "
            "restore the pin or re-record deliberately."
        )

    def test_every_cassette_came_from_the_pinned_provider(self):
        recorded = cassettes()
        if not recorded:
            pytest.skip("no cassettes recorded yet")
        assert {c["provider"] for c in recorded} == {PINNED_PROVIDER}

    def test_the_corpus_was_not_recorded_across_two_models(self):
        """The worst version of the failure, because it does not announce
        itself: half the table measured on one model and half on another still
        renders as one table."""
        recorded = cassettes()
        if not recorded:
            pytest.skip("no cassettes recorded yet")
        assert len({c["model"] for c in recorded}) == 1

    def test_the_provider_client_defaults_to_the_pin(self):
        """`tools/shadow.py` is what wires the two together; this asserts they
        cannot drift apart silently."""
        import tools.shadow as shadow

        assert shadow.default_model() == PINNED_MODEL
