"""The episode theatre's data, and the drift it must not be able to have.

`docs/theatre/` shows a stranger a real recovery episode: thirteen guards
evaluating, one refusing, and a receipt they can verify in their own browser.
It is the only surface where somebody who has not cloned this repository sees
the agent work, which makes a page describing an episode the agent did not run
the worst available bug -- it would be persuasive and wrong.

Two properties keep it honest and both are checked here rather than intended.
The JSON comes from `vasool.demo`'s own traversal through a second emitter, so
`data/golden/*.txt` pins the traversal the page is built from. And the statutes
the page displays are compared, string by string, against the guards' own
`statute` attributes -- if a guard stops citing a clause, the page cannot go on
quoting it through a stale export.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pytest

from vasool.ledger.receipts import GENESIS_HASH
from vasool.policy.registry import GUARD_CHAIN

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EPISODE_DIR = REPO_ROOT / "docs" / "theatre" / "episodes"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORTER = _load("export_episodes")

EPISODES = sorted(p for p in EPISODE_DIR.glob("*.json") if p.name != "index.json")


@pytest.fixture(scope="module", params=[p.name for p in EPISODES])
def episode(request) -> dict:
    return json.loads((EPISODE_DIR / request.param).read_text())


def _verdicts(episode: dict):
    for step in episode["steps"]:
        yield from step["verdicts"]


def _receipts(episode: dict):
    for step in episode["steps"]:
        yield from step["receipts"]


class TestTheExportExists:
    def test_every_golden_fixture_has_an_episode(self):
        """The page can only show episodes whose text output is byte-pinned.

        `tools/export_episodes.py` imports the fixture table rather than
        copying it, so this asserts that import is doing what it claims: no
        episode on the page without a golden fixture behind it, and none
        missing.
        """
        expected = {
            name.removeprefix("demo_").removesuffix(".txt")
            for name in EXPORTER._fixtures()
        }
        assert {p.stem for p in EPISODES} == expected

    def test_the_index_lists_every_episode(self):
        index = json.loads((EPISODE_DIR / "index.json").read_text())
        assert {row["file"] for row in index} == {p.name for p in EPISODES}

    def test_re_exporting_reproduces_the_committed_bytes(self):
        """Architectural invariant 5, at the theatre's boundary.

        A page whose data changed every time it was regenerated could not be
        checked by anyone, and the whole offer here is that it can be.
        """
        import os

        os.environ["VASOOL_ID_PEPPER"] = EXPORTER.TEST_PEPPER
        for name, argv in EXPORTER._fixtures().items():
            stem = name.removeprefix("demo_").removesuffix(".txt")
            fresh = EXPORTER.export(stem, argv)
            committed = json.loads((EPISODE_DIR / f"{stem}.json").read_text())
            assert fresh == committed, (
                f"{stem}.json is stale -- run `python3 tools/export_episodes.py`"
            )


class TestTheReceiptsVerify:
    def test_every_receipt_hash_covers_its_own_payload(self, episode):
        """What the browser is invited to do, done here first.

        The page recomputes SHA-256 over `canonical_payload` and compares. If
        that ever failed in a visitor's browser the claim on the front page
        would be false, so it is checked on every export.
        """
        found = list(_receipts(episode))
        assert found, "an episode with no receipt demonstrates nothing"
        for r in found:
            recomputed = hashlib.sha256(r["canonical_payload"].encode()).hexdigest()
            assert recomputed == r["hash"], r["receipt_id"]

    def test_the_chain_links_from_genesis(self, episode):
        prev = GENESIS_HASH
        for r in _receipts(episode):
            assert r["prev_hash"] == prev, r["receipt_id"]
            prev = r["hash"]


class TestTheGuardsAreQuotedCorrectly:
    """The page displays regulation. It must be the regulation in the code."""

    STATUTES = {
        type(g).__name__: getattr(g, "statute", None) for g in GUARD_CHAIN
    }

    def test_every_verdict_names_a_registered_guard(self, episode):
        for v in _verdicts(episode):
            assert v["guard"] in self.STATUTES, v["guard"]

    def test_every_statute_matches_the_guard_that_owns_it(self, episode):
        """The drift guard, and the reason this file exists.

        An exported episode is a snapshot. Without this, editing a guard's
        statute would leave the page quoting the old clause indefinitely, and
        nothing would fail -- the page would simply be wrong about the law in
        a repository whose entire argument is that it is not.
        """
        for v in _verdicts(episode):
            if v["statute"] is not None:
                assert v["statute"] == self.STATUTES[v["guard"]], v["guard"]

    def test_a_guard_chain_evaluates_the_whole_chain(self, episode):
        """Thirteen guards, every time, in registered order. A page that
        showed nine would be describing a chain that short-circuits, which is
        the design A4 exists to measure the absence of."""
        chains = [s for s in episode["steps"] if s["verdicts"]]
        assert chains, "no guard chain in this episode"
        registered = [type(g).__name__ for g in GUARD_CHAIN]
        for step in chains:
            assert [v["guard"] for v in step["verdicts"]] == registered

    def test_the_decisions_are_all_registered_members(self, episode):
        from vasool.policy.verdict import Decision

        allowed = {d.value for d in Decision}
        for v in _verdicts(episode):
            assert v["decision"] in allowed


class TestTheDocumentIsRenderable:
    def test_every_episode_has_a_conclusion(self, episode):
        assert episode["conclusion"]["text"].startswith("SUMMARY:")

    def test_the_banner_names_the_scenario_and_its_provenance(self, episode):
        labels = [f["label"] for f in episode["banner"]["fields"]]
        assert "scenario" in labels and "provenance" in labels

    def test_steps_are_numbered_from_one_without_gaps(self, episode):
        assert [s["n"] for s in episode["steps"]] == list(range(1, len(episode["steps"]) + 1))


class TestThePageAndItsDataAgree:
    """The page ships as a file in the repository. Nothing stops it going stale
    against the export except a test that says so."""

    PAGE = REPO_ROOT / "docs" / "theatre" / "index.html"
    BUNDLE = REPO_ROOT / "docs" / "theatre" / "episodes.js"

    def test_the_page_exists_and_loads_the_bundle(self):
        html = self.PAGE.read_text()
        assert 'src="episodes.js"' in html
        assert "crypto.subtle.digest" in html, "the page must verify, not assert"

    @staticmethod
    def _bundled() -> list[dict]:
        text = (REPO_ROOT / "docs" / "theatre" / "episodes.js").read_text()
        assert text.startswith("/* Generated by tools/export_episodes.py")
        start = text.index("[")
        return json.loads(text[start : text.rindex("]") + 1])

    def test_the_bundle_carries_every_episode(self):
        """Compared as a set: the bundle keeps `update_golden.FIXTURES`'s
        authored order, which is the tab order on the page and deliberately not
        alphabetical."""
        assert {e["name"] for e in self._bundled()} == {p.stem for p in EPISODES}

    def test_the_bundle_matches_the_episode_files(self):
        """One export, two artifacts. If they can drift, the page can show
        something no test ever checked."""
        for bundled in self._bundled():
            on_disk = json.loads((EPISODE_DIR / f"{bundled['name']}.json").read_text())
            assert bundled == on_disk, bundled["name"]

    def test_the_page_does_not_hardcode_a_hash(self):
        """A digest pasted into the markup would verify against itself forever.
        Every hash the page shows has to come from the export."""
        import re

        html = self.PAGE.read_text()
        genesis = "0" * 64
        for found in re.findall(r"\b[0-9a-f]{64}\b", html):
            assert found == genesis, f"hardcoded digest in the page: {found[:16]}…"
