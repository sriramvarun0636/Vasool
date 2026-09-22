"""COMPLIANCE.md's count of `# VERIFY:` markers is held to the code.

The working agreement is that an unverified regulatory threshold gets a
`# VERIFY:` comment rather than a confident assertion, and COMPLIANCE.md states
how many there are. That number went stale once already: two markers closed on
2026-09-22 — `SqlFactStore` learned to say it could not establish a mandate, and
the retry index stopped losing its mapping on a restart — and the document still
said 36 for a week. A count of the project's own admissions of uncertainty is
exactly the figure that must not quietly drift downwards, so it is derived here
instead of maintained by hand.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKER = re.compile(r"#\s*VERIFY:")


def verify_markers() -> dict[pathlib.Path, int]:
    """Every `# VERIFY:` in the shipped Python, by file.

    Tests are excluded: a marker there would be a note about a test, not an
    admission the shipped system makes about itself.
    """
    counts = {}
    for directory in ("vasool", "windtunnel", "tools"):
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            found = len(MARKER.findall(path.read_text()))
            if found:
                counts[path.relative_to(REPO_ROOT)] = found
    return counts


class TestVerifyMarkerCount:
    def test_compliance_md_states_the_number_the_code_has(self):
        total = sum(verify_markers().values())
        text = (REPO_ROOT / "COMPLIANCE.md").read_text()
        assert f"**There are {total} of them**" in text, (
            f"COMPLIANCE.md's `# VERIFY:` count has drifted from the code, which "
            f"has {total}. Update the sentence, and say which marker opened or "
            f"closed and why — a marker closing is a result."
        )

    def test_readme_does_not_restate_the_number(self):
        """The repository map used to carry its own copy, which is how the
        figure went stale in two places at once."""
        readme = (REPO_ROOT / "README.md").read_text()
        row = next(line for line in readme.splitlines() if "COMPLIANCE.md" in line and line.startswith("|"))
        assert not re.search(r"\d+ places", row), (
            "README's repository map states the VERIFY count again; it is "
            "COMPLIANCE.md's to state, once."
        )

    def test_every_marker_is_in_shipped_code(self):
        """A marker is a claim the deployed system makes about itself, so each
        one sits in a module that ships."""
        for path in verify_markers():
            assert path.parts[0] in ("vasool", "windtunnel", "tools")
