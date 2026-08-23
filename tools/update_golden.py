"""Regenerate data/golden/*.txt from a real vasool.demo run.

The one obvious documented command for it (also printed by `make golden`):

    python3 tools/update_golden.py

tests/test_demo.py asserts the demo's stdout byte-matches these fixtures.
Regenerating by hand-editing or copy-pasting terminal output is exactly what
this script exists to prevent — the pepper, the argv, and the scenario are
pinned here once, the same way tests/test_demo.py pins them for the assertion.
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
from contextlib import redirect_stdout

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # run as a plain script, not -m: put vasool/ on the path
GOLDEN_DIR = REPO_ROOT / "data" / "golden"

TEST_PEPPER = "test-pepper-do-not-use-in-prod"
"""Same fixed value tests/payloads.py::TEST_PEPPER uses. The customer_id HMAC
feeds into every receipt hash a golden fixture pins, so a real .env pepper
would make the fixture unreproducible outside this machine."""

FIXTURES: dict[str, list[str]] = {
    "demo_card_expired_1930.txt": ["--scenario", "card_expired", "--time", "19:30", "--replay"],
    "demo_card_expired_1930_settled.txt": [
        "--scenario",
        "card_expired",
        "--time",
        "19:30",
        "--replay",
        "--settle",
    ],
    "demo_card_expired_1930_hostile.txt": [
        "--scenario",
        "card_expired",
        "--time",
        "19:30",
        "--world",
        "hostile",
        "--replay",
    ],
    "demo_payment_risk_check_failed.txt": [
        "--scenario",
        "payment_risk_check_failed",
        "--replay",
    ],
    "demo_insufficient_fund_1930_settled.txt": [
        "--scenario",
        "insufficient_fund",
        "--time",
        "19:30",
        "--replay",
        "--settle",
    ],
}


def render(argv: list[str]) -> str:
    from vasool.demo import main  # deferred: needs VASOOL_ID_PEPPER set first

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    if rc != 0:
        raise RuntimeError(f"vasool.demo {argv} exited {rc}, refusing to write a golden fixture for a failure")
    return buf.getvalue()


def regenerate() -> None:
    os.environ["VASOOL_ID_PEPPER"] = TEST_PEPPER
    for filename, argv in FIXTURES.items():
        text = render(argv)
        (GOLDEN_DIR / filename).write_text(text)
        print(f"wrote {filename} ({len(text)} bytes)")


if __name__ == "__main__":
    regenerate()
