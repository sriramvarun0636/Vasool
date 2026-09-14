"""windtunnel/pepper.py: the world every published figure was computed in.

The pepper keys `customer_id`, and `customer_id` decides both §3c's split and
ContactWindowGuard's jitter, so it is as much an input to every number as the
seed is. Until 2026-09-14 it came from one machine's `.env` and was registered
nowhere; these tests are what make "registered" mean something
(docs/EVALUATION.md §10, 2026-09-14).

The strongest of them needs no shard, no `.env` and no network, so it runs on a
fresh clone and in CI. The manifest ships the head of seed 0's ledger with the
exact bytes each hash covers, and recomputing seed 0 under the registered value
has to reproduce those receipts byte for byte. Under any other pepper, not one
of them would match.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from tests.payloads import TEST_PEPPER
from windtunnel.adversary.arena import ADVERSARY_PEPPER
from windtunnel.pepper import REGISTERED_PEPPER
from windtunnel.runner import run_seed

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO_ROOT / "out" / "development" / "evaluation.json"


def _manifest() -> dict:
    if not MANIFEST.exists():
        pytest.skip("no manifest on disk")
    return json.loads(MANIFEST.read_text())


class TestThePublishedManifestWasComputedUnderIt:
    def test_the_manifest_names_the_registered_pepper_by_digest(self):
        expected = hashlib.sha256(REGISTERED_PEPPER.encode()).hexdigest()
        assert _manifest().get("pepper_sha256") == expected

    def test_it_reproduces_the_receipts_the_manifest_ships(self):
        """Does not trust the manifest's own account of its pepper: a digest
        field is written by the same code that used the value. The receipts
        are the world's own evidence — every canonical payload embeds
        customer ids derived under the pepper, so a wrong one fails on the
        first receipt."""
        determinism = _manifest()["determinism"]
        shipped = determinism["sample_receipts"]
        assert shipped, "the manifest ships no receipts to check against"

        ledger = run_seed(determinism["sample_seed"], pepper=REGISTERED_PEPPER).ledger()
        assert len(ledger) >= len(shipped)
        for receipt, expected in zip(ledger, shipped):
            assert receipt.receipt_id == expected["receipt_id"]
            assert receipt.canonical_payload == expected["canonical_payload"], (
                f"{expected['receipt_id']}: seed {determinism['sample_seed']} under the "
                "registered pepper does not reproduce the manifest's receipt. Either "
                "windtunnel/pepper.py is not the value the manifest was computed under, "
                "or the agent has changed since — and a changed agent also fails "
                "tests/windtunnel/test_fingerprint.py."
            )
            assert receipt.hash == expected["hash"]
            assert receipt.prev_hash == expected["prev_hash"]


class TestItIsItsOwnWorld:
    def test_it_is_none_of_the_other_literal_peppers(self):
        """Three literals, three worlds: the evaluation's, the adversary's,
        and the one the golden fixtures and the theatre are built under.
        Sharing one would couple a golden fixture to the evaluation, or an
        attack's customers to the split."""
        assert len({REGISTERED_PEPPER, ADVERSARY_PEPPER, TEST_PEPPER}) == 3

    def test_it_is_a_real_value(self):
        assert isinstance(REGISTERED_PEPPER, str) and REGISTERED_PEPPER.strip()
