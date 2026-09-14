"""The CI workflow is a fresh clone with nothing configured, and has to stay one.

POSTMORTEM.md INC-007: the submission failed eight tests and could not replay
its own LLM comparison on a clean checkout while every local run was green,
because every local run had credentials configured. The workflow exists to be
a stranger. The day it is handed a secret it stops being one, and nothing else
in the repository would notice — so this does.
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _instructions() -> str:
    """The workflow without its comments, which are allowed to name what
    the workflow must never be given."""
    return "\n".join(
        line for line in WORKFLOW.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


class TestTheWorkflowIsAStranger:
    def test_it_runs_the_whole_suite_on_every_push_and_pull_request(self):
        text = _instructions()
        assert "push:" in text and "pull_request:" in text
        assert "run: pytest" in text
        assert "pip install -r requirements.txt" in text

    def test_it_is_never_handed_a_secret(self):
        text = _instructions()
        assert "secrets." not in text
        for name in ("VASOOL_ID_PEPPER", "RAZORPAY_", "GEMINI_API_KEY", ".env"):
            assert name not in text, f"the workflow names {name}"

    def test_it_regenerates_the_published_artifacts_rather_than_trusting_them(self):
        """Every committed artifact a fresh clone can rebuild in minutes is
        rebuilt and diffed. The evaluation manifest is the exception — the
        base protocol alone is twenty minutes on eight cores — and is held
        instead by tests/windtunnel/test_pepper.py's receipts and the
        fingerprint tests."""
        text = _instructions()
        for artifact in ("out/adversary/redteam.json", "out/shadow/",
                         "docs/index.html docs/assets/"):
            assert f"git diff --exit-code -- {artifact}" in text, artifact
