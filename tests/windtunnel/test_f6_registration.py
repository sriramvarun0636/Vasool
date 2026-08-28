"""windtunnel/evaluate.py's F6 constants against docs/EVALUATION.md §10.

The same argument tests/windtunnel/test_parameters.py makes for §3d and §4,
applied to the one registered quantity that had no such guard.

**Why this file exists.** tests/windtunnel/test_evaluate.py imports
`F6_DENOMINATOR` and `F6_THRESHOLD` from the module under test and asserts the
shape of the block built from them. That checks that F6 is evaluated over
whatever the code says its denominator is — it cannot notice the code saying
something the document does not. §4 is machine-enforced and could not drift;
F6 was not, and could. Editing either side alone now fails here.

**What §10 registers, in two steps.** The 2026-08-24 row sets the denominator
at seven, naming them, and excludes A4 because its per-seed difference was
identically [0, 0]. The 2026-08-25 row drops that exclusion — post-fix A4's
difference is +7.29e-05 and excludes zero, so the ground is empirically false,
and the row records that finding a *new* reason to keep the exclusion would
violate the protocol. The registered denominator is therefore the seven named
in the earlier row plus A4, and §9's "more than half" is applied to that.
"""
from __future__ import annotations

import pathlib
import re

from windtunnel.evaluate import F6_DENOMINATOR, F6_DETAIL, F6_THRESHOLD

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
EVALUATION = REPO_ROOT / "docs" / "EVALUATION.md"


def _row(date: str, marker: str) -> str:
    """The §10 amendment row on `date` whose text contains `marker`."""
    for line in EVALUATION.read_text().splitlines():
        if line.startswith(f"| {date} |") and marker in line:
            return line
    raise AssertionError(f"§10 has no {date} row containing {marker!r}")


def _registered_denominator() -> frozenset[str]:
    """§10's denominator: the 2026-08-24 seven, plus A4 per 2026-08-25."""
    base = _row("2026-08-24", "F6 gets a registered evaluation rule")
    named = re.search(
        r"\*\*Denominator:\*\* the seven per-arm paired comparisons.*?— (.*?), each against",
        base,
    )
    assert named, "§10's 2026-08-24 row no longer names F6's seven comparisons"
    seven = frozenset(re.findall(r"`?\b(naive_retry|retry_plus_contact|vasool_ungated|A\d)\b`?", named.group(1)))
    assert len(seven) == 7, f"expected seven named arms, parsed {sorted(seven)}"
    assert "A4" not in seven, "the 2026-08-24 row is supposed to exclude A4"

    restored = _row("2026-08-25", "A4's exclusion ground is now stale")
    assert "Dropped the exclusion of A4" in restored, (
        "§10's 2026-08-25 row no longer drops A4's exclusion"
    )
    return seven | {"A4"}


class TestF6Registration:
    def test_the_denominator_matches_the_document(self):
        assert frozenset(F6_DENOMINATOR) == _registered_denominator()

    def test_the_denominator_has_no_duplicates(self):
        """It is a tuple, and `f6_verdict` iterates it once per arm. A repeated
        entry would let one flipped comparison count twice toward the
        threshold."""
        assert len(F6_DENOMINATOR) == len(set(F6_DENOMINATOR))

    def test_the_threshold_is_section_9s_more_than_half(self):
        """§9: "If more than half the conclusions in the report card flip".
        On eight, that is five — and it has to be derived from the denominator
        rather than pinned, or restoring an arm would silently make F6 easier
        to fire."""
        assert F6_THRESHOLD == len(F6_DENOMINATOR) // 2 + 1

    def test_section_9_still_words_the_threshold_as_more_than_half(self):
        assert "more than half the" in EVALUATION.read_text()

    def test_the_detail_string_cites_the_row_that_registers_this_denominator(self):
        """The detail string is emitted into evaluation.json and rendered on
        the dashboard, so a wrong citation is a published one. Eight arms is
        the 2026-08-25 reading; the 2026-08-24 row registers seven."""
        assert "2026-08-25" in F6_DETAIL
        assert re.search(r"fires iff 5 or more of the 8\b", F6_DETAIL)

    def test_the_detail_string_does_not_attribute_eight_to_the_seven_arm_row(self):
        head = F6_DETAIL.split(":", 1)[0]
        assert not (head.strip().startswith("§10, 2026-08-24")), (
            "F6_DETAIL opens by citing the row that registers the complement of "
            "the set it goes on to describe"
        )
