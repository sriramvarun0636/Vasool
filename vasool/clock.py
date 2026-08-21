"""The only module allowed to touch the wall clock.

Every other module under vasool/ and windtunnel/ receives time through an
injected Clock. This is what makes replay deterministic: the simulator drives
a VirtualClock, production wiring drives a RealClock, and no code path can
tell the difference. tests/test_no_wallclock.py enforces this by scanning
vasool/ and windtunnel/ for direct datetime.now() / datetime.utcnow() /
time.time() calls outside this file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    """The sole permitted call site for the real wall clock."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class VirtualClock:
    """Deterministic clock for simulation and replay. Time only moves forward."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance_to(self, t: datetime) -> None:
        self._t = max(self._t, t)

    def advance_by(self, delta: timedelta) -> None:
        self.advance_to(self._t + delta)
