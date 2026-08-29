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
HOLDOUT = REPO_ROOT / "out" / "holdout" / "evaluation.json"
SHADOW = REPO_ROOT / "out" / "shadow" / "classifier_comparison_partial.json"

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
        """Every percentage the README prints must be one a manifest supports.

        Asserting "the right number appears somewhere" is too weak — it passes
        while a stale copy of the same figure sits three paragraphs down. This
        inverts it: collect every percentage in the document and require each
        to be a value some cohort's manifest actually produces, at either
        precision the README uses. A drifted figure fails even if a correct one
        survives elsewhere.

        Both cohorts count. §3c's holdout is evaluated once and the README
        quotes it beside the development figures, so a percentage is legitimate
        if either manifest produces it — and stale if neither does.
        """
        manifests = [report]
        if HOLDOUT.exists():
            manifests.append(json.loads(HOLDOUT.read_text()))

        allowed = set()
        for manifest in manifests:
            for arm in self.HEADLINE_ARMS:
                rate = manifest["per_arm"][arm]["recovery_rate_mean"] * 100
                allowed.update({f"{rate:.2f}%", f"{rate:.1f}%"})

        # §4.5's shadow comparison prints its rates as percentages too. They
        # are measurements from a different artifact, not recovery rates, and
        # they are held to the same rule: quotable only if something produced
        # them.
        if SHADOW.exists():
            overall = json.loads(SHADOW.read_text()).get("overall", {})
            for value in overall.values():
                if isinstance(value, float) and 0.0 < value <= 1.0:
                    allowed.update({f"{value * 100:.2f}%", f"{value * 100:.1f}%"})

        # Both spellings. "49.07 percent" in an alt attribute is the same
        # claim as "49.07%" and drifts the same way, so the pattern
        # normalises the written-out form rather than walking past it.
        printed = {
            m if m.endswith("%") else m.split()[0] + "%"
            for m in re.findall(r"\d{1,3}\.\d{1,2}(?:%|\s+percent\b)", readme)
        }
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


    def test_no_rupee_figure_in_the_readme_is_stale(self, report, readme):
        """Money figures must come from the manifests, including the total.

        This exists because the total was briefly wrong: ₹46.50 Cr and
        ₹69.60 Cr were added as *displayed* figures to give ₹116.10 Cr, when
        summing the underlying paise gives ₹116.09 Cr. Adding rounded numbers
        manufactures a figure no artifact produced — the exact class of claim
        this repository exists to make impossible.

        The pattern matches ``Rs`` as well as ``\u20b9`` because that is how the
        stale ₹116.10 survived its first fix: the badge *alt* text spelled it
        ``Rs 116.10 Cr``, so a ₹-only regex walked straight past it. Alt text
        is exactly where a figure hides — it is read by screen readers and by
        anything parsing the raw markdown, and never by the person eyeballing
        the rendered page.
        """
        allowed = set()
        dev = report["per_arm"]["vasool"]["recovered_paise_total"]
        allowed.add(f"{dev / 100 / 1e7:.2f}")
        total = dev
        if HOLDOUT.exists():
            hold = json.loads(HOLDOUT.read_text())["per_arm"]["vasool"]["recovered_paise_total"]
            allowed.add(f"{hold / 100 / 1e7:.2f}")
            total = dev + hold
        allowed.add(f"{total / 100 / 1e7:.2f}")
        allowed.add(f"{total / 100 / 1e7:.0f}")          # the tagline rounds to whole crore

        printed = set(re.findall(r"(?:\u20b9|\bRs\.?)\s?(\d+(?:\.\d+)?)\s*Cr", readme))
        stale = sorted(printed - allowed)
        assert not stale, (
            f"README prints \u20b9{stale} Cr, which no manifest produces. "
            f"Manifest figures are {sorted(allowed)}."
        )


class TestTestCountDoesNotDrift:
    """The suite's own size is quoted in README.md and CLAUDE.md.

    It went stale three times in one week — every time a test was added, the
    two documents claiming a count became wrong, and nothing noticed. A number
    that only a human remembers to update is a number that will be wrong, so
    it is checked here against what pytest actually collected.
    """

    DOCS = {
        "README.md": re.compile(r"#\s*([\d,]+)\s*tests"),
        "CLAUDE.md": re.compile(r"\b([\d,]+)\s*tests\."),
    }

    def test_the_documents_quote_the_real_count(self, request):
        collected = request.session.testscollected
        # A filtered run (-k, a single file) collects a subset; comparing that
        # to a whole-suite figure would fail for the wrong reason.
        if request.config.option.keyword or request.config.option.file_or_dir != [str(REPO_ROOT)]:
            if collected < 500:
                pytest.skip(f"partial run collected {collected} — not the whole suite")

        for name, pattern in self.DOCS.items():
            text = (REPO_ROOT / name).read_text()
            match = pattern.search(text)
            assert match, f"{name} no longer quotes a test count"
            claimed = int(match.group(1).replace(",", ""))
            assert claimed == collected, (
                f"{name} says {claimed} tests; pytest collected {collected}"
            )


class TestExhibitsAreOrdered:
    """The report card is a numbered argument; the numbering has to hold.

    Exhibits were once labelled A, B, B2, B3, B4, C, D, E, F — additions
    announcing themselves as bolt-ons — and after renumbering the titles the
    HTML ids still said `exhibit-c` on the block displaying "EXHIBIT F". An
    anchor that lands on the wrong section is a small thing that reads as a
    careless one.
    """

    BLOCK = re.compile(r'<div class="exhibit[^"]*" id="(exhibit-[a-z]+)">')
    TITLE = re.compile(r'exhibit-title">EXHIBIT ([A-Z]) &mdash; ')

    @pytest.fixture(scope="class")
    def exhibits(self):
        blocks = re.split(r'<div class="exhibit[^"]*" id="', SOURCE)[1:]
        out = []
        for b in blocks:
            eid = b.split('"')[0]
            m = re.search(r'exhibit-title">EXHIBIT ([A-Z])', b)
            if m:
                out.append((eid, m.group(1)))
        return out

    def test_every_id_matches_its_letter(self, exhibits):
        assert exhibits, "no exhibits found — has the markup changed?"
        for eid, letter in exhibits:
            assert eid == f"exhibit-{letter.lower()}", (
                f'block id="{eid}" displays "EXHIBIT {letter}"'
            )

    def test_the_letters_are_consecutive_in_document_order(self, exhibits):
        letters = [l for _, l in exhibits]
        expected = [chr(ord("A") + i) for i in range(len(letters))]
        assert letters == expected, f"exhibits run {letters}, expected {expected}"

    def test_titles_use_one_dash_style(self):
        stray = re.findall(r"exhibit-title\">EXHIBIT [A-Z] —", SOURCE)
        assert not stray, f"{len(stray)} exhibit title(s) use a literal em dash, not &mdash;"


class TestDocsCiteRealExhibits:
    """Prose that names an exhibit must name one that exists.

    The exhibits were renumbered mid-build and three references went stale —
    two of them in the video script, which is recorded from. A judge following
    "Exhibit B3" to a page that has no B3 is a small error that reads as
    nobody having checked.
    """

    DOCS = ("README.md", "SUBMISSION.md", "docs/VIDEO.md", "ARCHITECTURE.md", "COMPLIANCE.md")

    def test_every_named_exhibit_exists(self):
        live = {m.group(1) for m in re.finditer(r'exhibit-title">EXHIBIT ([A-Z]) ', SOURCE)}
        assert live, "no exhibits found in the report source"
        bad = []
        for name in self.DOCS:
            text = (REPO_ROOT / name).read_text()
            for m in re.finditer(r"\bExhibit ([A-Z]\d?)\b", text):
                if m.group(1) not in live:
                    bad.append(f"{name} cites Exhibit {m.group(1)}")
        assert not bad, f"stale exhibit references: {bad}. Live exhibits: {sorted(live)}"
