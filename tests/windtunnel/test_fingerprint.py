"""windtunnel/fingerprint.py, and the refusal it makes possible.

INC-003 is the reason this file exists. A `make eval` after a real fix
finished in 5.5 seconds and re-emitted pre-fix rows as a post-fix evaluation,
because the resume keys on the seed and a shard said nothing about what
produced it. Nothing raised. Only the elapsed time gave it away.

So the tests here are not really about hashing. Two of them check that the
declared source set is honest -- that it covers what the run path loads, and
that its two exclusions are facts rather than opinions. The rest check that a
shard from another agent stops a run instead of being resumed over.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from windtunnel.evaluate import StaleShard, StaleShards, _done
from windtunnel.fingerprint import (
    EXCLUDED,
    MANIFEST_PATH,
    ManifestDrift,
    agent_fingerprint,
    agent_sources,
    read_manifest,
)


def _tree(root, files: dict[str, str]) -> None:
    """Write a synthetic repository so a fingerprint can be perturbed without
    editing the real tree."""
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


BASELINE = {
    "vasool/policy/guards/contact_window.py": "WINDOW = (8, 19)\n",
    "vasool/policy/machine.py": "MAX_DEFERRALS = 5\n",
    "windtunnel/outcome.py": "P = 0.35\n",
    "README.md": "not source\n",
}


class TestTheDeclaredSet:
    """The set has to be honest before the hash over it means anything."""

    def test_the_manifest_matches_what_the_globs_resolve_to(self):
        """The drift guard, and the reason `AGENT_SOURCES` can stay a glob.

        A bare glob that silently starts matching a new file changes what every
        fingerprint means with nobody deciding it should. This failing is not a
        nuisance -- it is the check doing its job, and the fix is to look at
        the file and then regenerate, in that order.
        """
        assert agent_sources() == read_manifest()

    def test_the_manifest_is_not_part_of_what_it_describes(self):
        """Otherwise regenerating it would change the fingerprint it records,
        which never converges."""
        assert MANIFEST_PATH.suffix != ".py"
        assert MANIFEST_PATH.relative_to(MANIFEST_PATH.parent.parent).as_posix() not in agent_sources()

    def test_the_demo_really_is_off_the_run_path(self):
        """`vasool/demo.py` is excluded on the claim that no simulated run
        imports it. That is checkable, so it is checked rather than asserted in
        a comment -- if the emitter refactor of §2.3 ever pulls demo.py onto
        the run path, this fails and the exclusion has to be revisited.
        """
        assert "vasool/demo.py" in EXCLUDED
        probe = subprocess.run(
            [sys.executable, "-c",
             "import windtunnel.runner, sys; print('vasool.demo' in sys.modules)"],
            capture_output=True, text=True, check=True,
        )
        assert probe.stdout.strip() == "False", probe.stdout

    def test_neither_exclusion_appears_in_the_set(self):
        sources = agent_sources()
        for excluded in EXCLUDED:
            assert excluded not in sources

    def test_the_set_covers_the_guards_and_the_outcome_model(self):
        """A fingerprint that missed either would be worse than none: it would
        look like a check while passing the two things most likely to move a
        number."""
        sources = agent_sources()
        assert "vasool/policy/guards/contact_window.py" in sources
        assert "windtunnel/outcome.py" in sources
        assert "windtunnel/parameters.py" in sources


class TestTheHash:
    def test_the_same_tree_hashes_the_same_way_twice(self, tmp_path):
        _tree(tmp_path, BASELINE)
        first = agent_fingerprint(tmp_path, check_manifest=False)
        assert first == agent_fingerprint(tmp_path, check_manifest=False)
        assert len(first) == 64

    def test_one_changed_character_in_a_guard_changes_it(self, tmp_path):
        _tree(tmp_path, BASELINE)
        before = agent_fingerprint(tmp_path, check_manifest=False)
        (tmp_path / "vasool/policy/guards/contact_window.py").write_text("WINDOW = (8, 20)\n")
        assert agent_fingerprint(tmp_path, check_manifest=False) != before

    def test_a_non_source_file_does_not_change_it(self, tmp_path):
        _tree(tmp_path, BASELINE)
        before = agent_fingerprint(tmp_path, check_manifest=False)
        (tmp_path / "README.md").write_text("edited prose\n")
        assert agent_fingerprint(tmp_path, check_manifest=False) == before

    def test_a_new_agent_module_changes_it(self, tmp_path):
        _tree(tmp_path, BASELINE)
        before = agent_fingerprint(tmp_path, check_manifest=False)
        _tree(tmp_path, {"vasool/policy/guards/new_guard.py": "pass\n"})
        assert agent_fingerprint(tmp_path, check_manifest=False) != before

    def test_a_drifted_manifest_raises_rather_than_re_resolving(self, tmp_path):
        _tree(tmp_path, BASELINE)
        with pytest.raises(ManifestDrift):
            agent_fingerprint(tmp_path, check_manifest=True)


def _shard_with(path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class TestTheRefusal:
    """What `_done` does when a shard was not produced by the running agent."""

    def test_rows_from_this_agent_resume_normally(self, tmp_path):
        path = tmp_path / "vasool.jsonl"
        _shard_with(path, [{"seed": 0, "agent": "aa"}, {"seed": 1, "agent": "aa"}])
        assert sorted(_done(path, expected="aa")) == [0, 1]

    def test_a_missing_shard_is_not_an_error(self, tmp_path):
        assert _done(tmp_path / "absent.jsonl", expected="aa") == {}

    def test_a_foreign_row_refuses_by_default(self, tmp_path):
        path = tmp_path / "vasool.jsonl"
        _shard_with(path, [{"seed": 0, "agent": "aa"}, {"seed": 1, "agent": "bb"}])
        with pytest.raises(StaleShard) as caught:
            _done(path, expected="aa")
        message = str(caught.value)
        assert "aa" in message and "bb" in message, "both fingerprints must be named"
        assert "INC-003" in message, "the reader should be told what this caught"

    def test_a_row_predating_the_field_refuses_too(self, tmp_path):
        """A row with no `agent` key is not a row from this agent. Treating
        absence as agreement would exempt exactly the shards most likely to be
        stale."""
        path = tmp_path / "vasool.jsonl"
        _shard_with(path, [{"seed": 0}])
        with pytest.raises(StaleShard):
            _done(path, expected="aa")

    def test_rebuild_discards_the_foreign_rows_and_truncates(self, tmp_path):
        path = tmp_path / "vasool.jsonl"
        _shard_with(path, [{"seed": 0, "agent": "aa"}, {"seed": 1, "agent": "bb"}])
        assert _done(path, expected="aa", stale=StaleShards.REBUILD) == {}
        assert path.read_text() == "", "the shard must not keep rows a rebuild discarded"

    def test_adopt_stamps_the_rows_and_rewrites_the_file(self, tmp_path):
        path = tmp_path / "vasool.jsonl"
        _shard_with(path, [{"seed": 1}, {"seed": 0, "agent": "aa"}])
        rows = _done(path, expected="aa", stale=StaleShards.ADOPT)

        assert sorted(rows) == [0, 1]
        assert all(r["agent"] == "aa" for r in rows.values())

        on_disk = [json.loads(line) for line in path.read_text().splitlines()]
        assert [r["seed"] for r in on_disk] == [0, 1], "rewritten in seed order"
        assert all(r["agent"] == "aa" for r in on_disk)

    def test_an_adopted_shard_then_resumes_without_complaint(self, tmp_path):
        path = tmp_path / "vasool.jsonl"
        _shard_with(path, [{"seed": 0}])
        _done(path, expected="aa", stale=StaleShards.ADOPT)
        assert sorted(_done(path, expected="aa")) == [0]

    def test_a_half_written_line_is_dropped_not_fatal(self, tmp_path):
        """Unchanged behaviour, retested here because the parse path moved."""
        path = tmp_path / "vasool.jsonl"
        path.write_text(json.dumps({"seed": 0, "agent": "aa"}) + "\n" + '{"seed": 1, "ag')
        assert sorted(_done(path, expected="aa")) == [0]
