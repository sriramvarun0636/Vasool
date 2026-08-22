"""Enforces the action plane's two import boundaries:

1. Only vasool/actions/razorpay_client.py may import the Razorpay SDK.
2. Only vasool/actions/executor.py may import that module.

Same technique as tests/test_no_wallclock.py — walks vasool/ and windtunnel/
with pathlib.rglob rather than a real grep, so .venv/site-packages never
gives a false sense of security.
"""
from __future__ import annotations

import pathlib

PACKAGE_ROOTS = ("vasool", "windtunnel")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

RAZORPAY_CLIENT_FILE = pathlib.PurePosixPath("vasool/actions/razorpay_client.py")
EXECUTOR_FILE = pathlib.PurePosixPath("vasool/actions/executor.py")

SDK_IMPORT_NEEDLES = ("import razorpay", "from razorpay")
CLIENT_IMPORT_NEEDLES = (
    "from vasool.actions.razorpay_client import",
    "from vasool.actions import razorpay_client",
    "import vasool.actions.razorpay_client",
)


def _package_source_files():
    for root_name in PACKAGE_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_only_razorpay_client_imports_the_sdk():
    violations: list[str] = []
    for path in _package_source_files():
        rel = pathlib.PurePosixPath(path.relative_to(REPO_ROOT).as_posix())
        if rel == RAZORPAY_CLIENT_FILE:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in SDK_IMPORT_NEEDLES:
                if needle in line:
                    violations.append(f"{rel}:{lineno}: found {needle!r}")

    assert not violations, "razorpay SDK imported outside razorpay_client.py:\n" + "\n".join(
        violations
    )


def test_only_executor_imports_the_client():
    violations: list[str] = []
    for path in _package_source_files():
        rel = pathlib.PurePosixPath(path.relative_to(REPO_ROOT).as_posix())
        if rel in (RAZORPAY_CLIENT_FILE, EXECUTOR_FILE):
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in CLIENT_IMPORT_NEEDLES:
                if needle in line:
                    violations.append(f"{rel}:{lineno}: found {needle!r}")

    assert not violations, "razorpay_client imported outside executor.py:\n" + "\n".join(
        violations
    )


def test_scan_actually_covers_something():
    """Guards against a silently-empty scan passing this test for the wrong
    reason (test_no_wallclock.py holds the same line)."""
    assert any(_package_source_files())
