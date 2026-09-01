"""Enforces architectural invariant 2: nothing outside vasool/clock.py touches the
wall clock. A real grep would also match .venv/site-packages and give a false
sense of security, so this walks only the vasool/ and windtunnel/ package
directories with pathlib.rglob.
"""
from __future__ import annotations

import pathlib

FORBIDDEN = ("datetime.now(", "datetime.utcnow(", "time.time(")
PACKAGE_ROOTS = ("vasool", "windtunnel")
ALLOWED_FILE = pathlib.PurePosixPath("vasool/clock.py")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _package_source_files():
    for root_name in PACKAGE_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_no_wallclock_outside_clock_module():
    violations: list[str] = []
    for path in _package_source_files():
        rel = pathlib.PurePosixPath(path.relative_to(REPO_ROOT).as_posix())
        if rel == ALLOWED_FILE:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in FORBIDDEN:
                if needle in line:
                    violations.append(f"{rel}:{lineno}: found {needle!r}")

    assert not violations, "wall-clock call(s) outside vasool/clock.py:\n" + "\n".join(
        violations
    )


def test_scan_actually_covers_something():
    """Guards against a silently-empty scan (e.g. rglob given a typo'd root)
    passing this test for the wrong reason."""
    assert any(_package_source_files()), "no .py files found under vasool/ or windtunnel/"
