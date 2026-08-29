"""tools/report.py — the file that renders the published artifact.

Before this file it had no coverage at all, which is how a hardcoded
`|| 18541` sat in it rendering as a measurement, and how a `trace()` call
placed above its own `const` shipped a page whose every figure silently fell
back to a dash. Both were found by eye. Neither should have needed to be.

The tests here are deliberately about *provenance* rather than appearance:
the report card's whole claim is that every number on it came from the
manifest, so what is worth asserting is that no number can get onto the page
any other way.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REPORT = REPO_ROOT / "tools" / "report.py"
MANIFEST = REPO_ROOT / "out" / "development" / "evaluation.json"

SOURCE = REPORT.read_text()


class TestNoHardcodedMeasurements:
    def test_no_numeric_or_alternative_fallback(self):
        """`EVAL?.x?.y || 18541` is the bug class this guards.

        A falsy-coalescing default turns a missing measurement into a
        plausible number with no warning, and turns a legitimate zero into
        one too. The page renders a dash instead and raises its own
        fallback banner; nothing may reintroduce the shortcut.
        """
        offenders = [
            m.group(0).strip()
            for m in re.finditer(r"[^\n;]{0,80}\|\|\s*\d[\d_,.]*", SOURCE)
            if re.search(r"\bEVAL\b|\barms\b|\bper_arm\b", m.group(0))
        ]
        assert not offenders, (
            f"numeric || fallback on a measurement reintroduced: {offenders}. "
            "A missing measurement must render as a dash, not as a plausible "
            "number. (A `|| 1` guarding a divide-by-zero on chart geometry is "
            "fine and is deliberately not matched — the test keys on the "
            "expression reading from EVAL.)"
        )

    def test_the_violation_count_is_not_a_static_string(self):
        """It used to be `0 safety violations` typed into the markup, which no
        code ever updated — true by luck rather than by measurement."""
        assert "0 safety violations</span>" not in SOURCE


class TestTraceDiscipline:
    """`trace()` is the only sanctioned route for a figure onto the page."""

    def test_the_helper_is_defined_before_every_call(self):
        """A `trace(...)` above `const traced = []` is a temporal-dead-zone
        error that throws inside an async handler — which surfaces as an
        unhandled rejection, not a console error, so the page renders blank
        rather than loudly broken. Ordering is the whole guard."""
        declaration = SOURCE.index("const traced = [];")
        calls = [m.start() for m in re.finditer(r"(?<![.\w])trace\(", SOURCE)]
        assert calls, "no trace() calls found — has the helper been renamed?"
        earliest = min(calls)
        assert earliest > declaration, (
            "a trace() call appears before `const traced = []`; every figure "
            "on the page will fall back to a dash at runtime"
        )

    def test_every_traced_path_is_a_manifest_key(self):
        if not MANIFEST.exists():
            pytest.skip("no manifest on disk — run `make sweeps` first")
        report = json.loads(MANIFEST.read_text())

        paths = set(re.findall(r'trace\([^,]+,\s*"([a-z_][\w.]*)"', SOURCE))
        assert paths, "no literal trace() paths found"
        for path in sorted(paths):
            node = report
            for part in path.split("."):
                assert isinstance(node, dict) and part in node, (
                    f"trace() cites {path!r}, but {part!r} is not in the manifest"
                )
                node = node[part]


class TestReceiptSample:
    """The verifier's whole point is that a reader need not trust this page."""

    @pytest.fixture(scope="class")
    def receipts(self):
        if not MANIFEST.exists():
            pytest.skip("no manifest on disk — run `make sweeps` first")
        sample = json.loads(MANIFEST.read_text())["determinism"]["sample_receipts"]
        if not sample:
            pytest.skip("manifest carries no receipt sample")
        return sample

    def test_every_hash_is_sha256_of_its_own_canonical_payload(self, receipts):
        import hashlib

        for r in receipts:
            digest = hashlib.sha256(r["canonical_payload"].encode()).hexdigest()
            assert digest == r["hash"], f"{r['receipt_id']} does not hash to its payload"

    def test_the_sample_is_a_chain(self, receipts):
        for earlier, later in zip(receipts, receipts[1:]):
            assert later["prev_hash"] == earlier["hash"]


class TestReadmeDoesNotDrift:
    """README.md quotes figures from the manifest. It must not go stale.

    This is the check that matters most for the document a reader meets first:
    a README claiming 49.1% after the manifest moved is worse than one that
    claims nothing. Every key the README's verification table cites is
    resolved here, and the headline percentages are recomputed from the
    manifest and matched against the prose.
    """

    README = REPO_ROOT / "README.md"

    @pytest.fixture(scope="class")
    def report(self):
        if not MANIFEST.exists():
            pytest.skip("no manifest on disk — run `make sweeps` first")
        return json.loads(MANIFEST.read_text())

    @pytest.fixture(scope="class")
    def readme(self):
        return self.README.read_text()

    def test_every_cited_key_resolves(self, report, readme):
        cited = set(re.findall(r"`((?:per_arm|paired_vs_vasool|pass_k|falsification|determinism)[\w.]*)`", readme))
        assert cited, "the README no longer cites any manifest keys"
        for path in sorted(cited):
            node = report
            for part in path.split("."):
                assert isinstance(node, dict) and part in node, (
                    f"README cites `{path}`, but {part!r} is not in the manifest"
                )
                node = node[part]

    HEADLINE_ARMS = ("vasool", "retry_plus_contact", "vasool_ungated")

    def test_no_recovery_percentage_in_the_readme_is_stale(self, report, readme):
        """Every percentage the README prints must be one the manifest supports.

        Asserting "the right number appears somewhere" is too weak — it passes
        while a stale copy of the same figure sits three paragraphs down. This
        inverts it: collect every percentage in the document and require each
        to be a value the manifest actually produces, at either precision the
        README uses. A drifted figure fails even if a correct one survives
        elsewhere.
        """
        allowed = set()
        for arm in self.HEADLINE_ARMS:
            rate = report["per_arm"][arm]["recovery_rate_mean"] * 100
            allowed.update({f"{rate:.2f}%", f"{rate:.1f}%"})

        printed = set(re.findall(r"\d{1,3}\.\d{1,2}%", readme))
        stale = sorted(printed - allowed)
        assert not stale, (
            f"README prints {stale}, which no arm's recovery rate supports. "
            f"Manifest values are {sorted(allowed)}."
        )

    def test_each_arm_appears_at_least_once(self, report, readme):
        for arm in self.HEADLINE_ARMS:
            rate = report["per_arm"][arm]["recovery_rate_mean"] * 100
            assert f"{rate:.2f}%" in readme or f"{rate:.1f}%" in readme, (
                f"README no longer quotes {arm}'s recovery rate ({rate:.2f}%)"
            )

    def test_the_headline_gap_matches(self, report, readme):
        point = report["paired_vs_vasool"]["retry_plus_contact"]["recovery_rate"]["point"]
        assert f"{point * 100:.2f}" == "-16.35"
        assert "16.35" in readme

    def test_the_adversary_count_matches(self, readme):
        artifact = REPO_ROOT / "out" / "adversary" / "redteam.json"
        if not artifact.exists():
            pytest.skip("no red-team artifact — run `make redteam` first")
        result = json.loads(artifact.read_text())
        pattern = rf"{result['survived']}\s+of\s+{result['attacks']}\s+survive"
        assert re.search(pattern, readme), (
            f"README's attack count disagrees with the artifact "
            f"({result['survived']} of {result['attacks']} survive)"
        )
