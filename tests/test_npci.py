"""§2.2: the CITED tier and NPCI's UPI vocabulary, held to their registration.

docs/EVALUATION.md §10, 2026-09-15 registered four properties before any file
used the tier — every payload file declares a tier, every citation names its
document, version and clause, no code string appears in two tiers, and every
mapped code resolves to a real class or is listed as unmapped. They are tested
here along with what the implementation added: the mapping never contradicts
NPCI's own TD/BD column, docs/taxonomy.md §11 is the mapping rendered rather
than a second copy of it, and nothing on the simulator's path reaches any of it
yet.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

import vasool.events.provenance as provenance
from vasool.diagnosis.npci import BY_KEY, MAPPINGS, Unmapped, outcome
from vasool.diagnosis.taxonomy import FailureClass
from vasool.events.provenance import (
    CITED_FIELDS,
    Provenance,
    ProvenanceError,
    payload_files,
    tier_of,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TAXONOMY = REPO_ROOT / "docs" / "taxonomy.md"


def _load_tool():
    spec = importlib.util.spec_from_file_location("_cite_npci", REPO_ROOT / "tools" / "cite_npci.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CITE = _load_tool()
CITED = CITE.load_cited()
BUSINESS = {FailureClass.LIQUIDITY, FailureClass.INSTRUMENT_DEAD,
            FailureClass.CUSTOMER_ACTION, FailureClass.RISK_BLOCK}


# ---------------------------------------------------------------------------
# three tiers, no fourth, no fact without one
# ---------------------------------------------------------------------------
class TestEveryFactHasATier:
    def test_every_payload_file_declares_one(self):
        files = payload_files()
        assert len(files) >= 20
        for path in files:
            tier_of(path)

    def test_all_three_tiers_are_in_use(self):
        assert {tier_of(path) for path in payload_files()} == set(Provenance)

    @pytest.fixture
    def tiers(self, tmp_path, monkeypatch) -> dict[Provenance, pathlib.Path]:
        dirs = {tier: tmp_path / tier.value.lower() for tier in Provenance}
        for directory in dirs.values():
            directory.mkdir()
        monkeypatch.setattr(provenance, "DIRECTORY", dirs)
        return dirs

    def test_a_capture_that_gains_a_marker_is_refused(self, tiers):
        path = tiers[Provenance.OBSERVED] / "capture.json"
        path.write_text(json.dumps({"entity": "event", "_SIMULATED": True}))
        with pytest.raises(ProvenanceError, match="captures are never edited"):
            tier_of(path)

    def test_a_stub_without_its_marker_is_refused(self, tiers):
        path = tiers[Provenance.SIMULATED] / "stub.json"
        path.write_text(json.dumps({"entity": "event"}))
        with pytest.raises(ProvenanceError, match="_SIMULATED"):
            tier_of(path)

    def test_a_citation_that_cannot_be_checked_is_refused(self, tiers):
        document = json.loads(CITE.cited_path("3.1").read_text())
        del document["_PROVENANCE"]["sha256"]
        path = tiers[Provenance.CITED] / "citation.json"
        path.write_text(json.dumps(document))
        with pytest.raises(ProvenanceError, match="sha256"):
            tier_of(path)


# ---------------------------------------------------------------------------
# the citation
# ---------------------------------------------------------------------------
class TestTheCitation:
    def test_every_cited_file_names_document_version_clause_and_bytes(self):
        for section in CITE.SECTIONS:
            block = json.loads(CITE.cited_path(section).read_text())["_PROVENANCE"]
            assert set(CITED_FIELDS) <= set(block)
            assert block["tier"] == Provenance.CITED.value
            assert block["publisher"].startswith("National Payments Corporation of India")
            assert block["version"] == "2.9"
            assert block["clause"].startswith(f"§{section} ")
            assert block["sha256"] == CITE.SHA256 and len(block["sha256"]) == 64

    def test_the_three_sections_hold_the_registered_225_codes(self):
        counts = collections.Counter(section for section, _ in CITED)
        assert counts == {"3.1": 110, "4.1": 7, "4.4": 108}

    def test_every_code_is_transcribed_with_its_description_and_column(self):
        for (section, code), record in CITED.items():
            assert record["description"], (section, code)
            assert record["td_bd"] in {"TD", "BD", "NA", "-", None}, (section, code)

    def test_no_code_string_appears_in_two_tiers(self):
        """A Razorpay reason and an NPCI code are different vocabularies; if
        one string ever turned up in both, a classifier keyed on either would
        read a stub's reason as a cited fact, or the reverse."""
        razorpay: set[str] = set()
        fields = {"error_code", "error_reason", "error_source", "error_step"}

        def collect(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in fields and isinstance(value, str):
                        razorpay.add(value)
                    collect(value)
            elif isinstance(node, list):
                for item in node:
                    collect(item)

        for path in payload_files():
            if tier_of(path) is not Provenance.CITED:
                collect(json.loads(path.read_text()))
        assert razorpay, "no error fields found in the observed or stubbed payloads"
        assert not razorpay & {code for _, code in CITED}


# ---------------------------------------------------------------------------
# the mapping
# ---------------------------------------------------------------------------
class TestTheMapping:
    def test_every_cited_code_is_mapped_exactly_once(self):
        keys = [m.key for m in MAPPINGS]
        assert len(keys) == len(set(keys))
        assert set(keys) == set(CITED)

    def test_every_outcome_is_a_class_or_a_registered_reason_with_a_why(self):
        for m in MAPPINGS:
            assert isinstance(m.outcome, (FailureClass, Unmapped)), m.key
            assert m.why.strip(), m.key

    def test_no_code_marked_TD_blames_the_customer_or_the_instrument(self):
        wrong = [m.key for m in MAPPINGS if CITED[m.key]["td_bd"] == "TD" and m.outcome in BUSINESS]
        assert not wrong, f"NPCI marks these TD, but they map to a business class: {wrong}"

    def test_no_code_marked_BD_is_called_transient(self):
        wrong = [m.key for m in MAPPINGS
                 if CITED[m.key]["td_bd"] == "BD" and m.outcome is FailureClass.TRANSIENT]
        assert not wrong, f"NPCI marks these BD, but they map to TRANSIENT: {wrong}"

    def test_there_is_no_default(self):
        with pytest.raises(KeyError):
            outcome("4.5", "U81")

    def test_every_timeout_and_reversal_UPI_reports_is_reconciled_except_a_full_reversal(self):
        """§4.1 is the codes UPI itself returns on a timeout. Only `21 NO ACTION
        TAKEN (FULL REVERSAL)` says nothing moved; every other one leaves money
        in flight, and a retry there is a second debit."""
        for (section, code), m in BY_KEY.items():
            if section == "4.1":
                expected = FailureClass.TRANSIENT if code == "21" else Unmapped.RECONCILE
                assert m.outcome is expected, code

    def test_the_legal_stops_are_exactly_the_five_legal_codes(self):
        stops = {m.code for m in MAPPINGS if m.outcome is Unmapped.LEGAL_STOP}
        assert stops == {"VO", "VP", "VQ", "VR", "VZ"}
        for code in stops:
            assert re.search(r"COURT|DEATH|INSOLVENCY|LUNACY|ATTACHMENT",
                             CITED[("3.1", code)]["description"]), code

    def test_insufficient_funds_is_liquidity_and_a_revoked_mandate_is_dead(self):
        """The design's own examples of the judgement, held."""
        assert outcome("3.1", "Z9") is FailureClass.LIQUIDITY
        assert outcome("3.1", "VA") is FailureClass.INSTRUMENT_DEAD


# ---------------------------------------------------------------------------
# the prose is the mapping, rendered
# ---------------------------------------------------------------------------
class TestTheProseIsTheMapping:
    def test_section_11s_table_is_the_rendered_mapping(self):
        text = TAXONOMY.read_text()
        start = text.index(CITE.TABLE_START)
        end = text.index(CITE.TABLE_END) + len(CITE.TABLE_END)
        assert text[start:end] == CITE.render_table(), (
            "docs/taxonomy.md §11's table has drifted from vasool/diagnosis/npci.py — "
            "run `python tools/cite_npci.py table`, never edit the table by hand."
        )

    def test_readme_s_three_way_count_is_the_mapping_s(self):
        """§2.2's registered definition of done: README's 'nine of ten are
        simulated' becomes a three-way count, more informative and no more
        flattering — so the count is held to the mapping, and the sentence
        saying none of it was observed is held to exist."""
        readme = (REPO_ROOT / "README.md").read_text()
        tally = collections.Counter(m.outcome for m in MAPPINGS)
        mapped = sum(tally[c] for c in FailureClass)
        assert f"**{len(MAPPINGS)}** UPI codes transcribed from NPCI's public specification" in readme
        assert f"**{mapped}** fit the five failure classes and **{len(MAPPINGS) - mapped}** do not" in readme
        assert "None of the 225 has been seen arriving through Razorpay" in readme
        assert "thirty codes that mean money may already have moved" in readme
        assert tally[Unmapped.RECONCILE] == 30

    def test_the_counts_section_11_quotes_are_the_mapping_s(self):
        text = TAXONOMY.read_text().split("## 11. UPI", 1)[1]
        tally = collections.Counter(m.outcome for m in MAPPINGS)
        mapped = sum(tally[c] for c in FailureClass)
        assert f"**{mapped} of NPCI's {len(MAPPINGS)} codes fit the five classes. " \
               f"{len(MAPPINGS) - mapped} do not.**" in text
        for reason in Unmapped:
            for match in re.finditer(rf"(\d+) (?:codes? )?\(?`{reason.value}`", text):
                assert int(match.group(1)) == tally[reason], reason


# ---------------------------------------------------------------------------
# not on the run path, yet
# ---------------------------------------------------------------------------
class TestNotOnTheRunPathYet:
    def test_nothing_the_simulator_imports_reaches_the_vocabulary(self):
        """What makes §2.2 change no number, checked in a clean interpreter.
        §2.5 wires the vocabulary in; this test changes in that commit, beside
        the amendment and the re-run it needs."""
        probe = (
            "import sys; import windtunnel.runner, windtunnel.evaluate, windtunnel.shadow; "
            "print([m for m in ('vasool.diagnosis.npci', 'vasool.events.provenance') "
            "if m in sys.modules])"
        )
        result = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT,
                                capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "[]"

    def test_the_cited_tier_is_outside_the_fingerprint(self):
        """Because nothing on the run path reads it — the fingerprint's own rule
        is 'can it change a shard row', and a file nothing reads cannot."""
        from windtunnel.fingerprint import agent_sources

        assert not [s for s in agent_sources() if s.startswith("data/cited_payloads/")]
