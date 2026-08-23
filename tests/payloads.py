"""Real captured payloads on disk -> FailureEvent, for the policy-plane tests.

Same discipline as tests/test_rules.py: reason and source strings are read off
disk, never typed in. CLAUDE.md — a reason in neither data/observed_payloads/
nor data/stubbed_payloads/ does not exist.

This is a separate module rather than a conftest fixture because the guards need
these as plain values inside hypothesis strategies, where a pytest fixture
cannot reach.
"""
from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

from vasool.events.schemas import FailureEvent, from_webhook

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED = REPO_ROOT / "data" / "observed_payloads"
STUBBED = REPO_ROOT / "data" / "stubbed_payloads"
TEST_PEPPER = "test-pepper-do-not-use-in-prod"

NEVER_OBSERVED_SOURCE = "network"
"""A source string on no payload in data/. Used to reach the payment_failed /
*other* row, which by definition no observed source reaches."""


def _paths() -> list[pathlib.Path]:
    return sorted(OBSERVED.glob("payment_failed__*.json")) + sorted(
        STUBBED.glob("SIMULATED__payment_failed__*.json")
    )


def _event_from(path: pathlib.Path) -> FailureEvent:
    fixture = json.loads(path.read_text())
    return from_webhook(
        event_id=fixture["headers"]["x-razorpay-event-id"],
        body=fixture["body"],
        pepper=TEST_PEPPER,
    )


def all_events() -> list[FailureEvent]:
    return [_event_from(p) for p in _paths()]


def event_for(reason: str, source: str | None = None) -> FailureEvent:
    """A real captured event for `reason`, optionally re-sourced."""
    for event in all_events():
        if event.error_reason == reason:
            return event if source is None else event.model_copy(update={"error_source": source})
    raise LookupError(f"no payload on disk for {reason!r}")


def body_for(reason: str) -> dict[str, Any]:
    """The raw webhook body behind `event_for(reason)`, as a fresh copy.

    For the tests that need the envelope rather than the decoded event —
    anything exercising `from_webhook` itself, which is where a failed
    retry's payment id is correlated back to its episode. A copy, so stamping
    an id onto it cannot leak into another test's fixture.
    """
    for path in _paths():
        fixture = json.loads(path.read_text())
        if fixture["body"]["payload"]["payment"]["entity"]["error_reason"] == reason:
            return copy.deepcopy(fixture["body"])
    raise LookupError(f"no payload on disk for {reason!r}")


def one_event_per_pair() -> list[FailureEvent]:
    """One representative event per (reason, source) pair on disk."""
    seen: dict[tuple[str, str], FailureEvent] = {}
    for event in all_events():
        seen.setdefault((event.error_reason, event.error_source), event)
    return [seen[k] for k in sorted(seen)]
