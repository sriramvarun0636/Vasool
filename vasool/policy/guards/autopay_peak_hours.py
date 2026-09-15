"""UPI Autopay executions happen outside NPCI's peak hours.

NPCI's OC-215A/2025-26 lists "Autopay Mandate Execution" among the APIs its
members must moderate: "To be initiated in non-peak hours", where peak hours
are "10:00 hrs to 13:00 hrs and from 17:00 hrs to 21:30 hrs" and any other
time is non-peak. A recovery retry on a UPI Autopay mandate is an execution,
so it is held out of both windows. Card mandates are not NPCI's, and nothing
here touches them.

Defers rather than blocks: the rule is about when, never whether, exactly as
the contact window is.

**Two readings the circular leaves open, and the ones taken.** It names no
time zone; UPI's hours are India's, so the windows are read in IST. It does
not say whether 13:00 and 21:30 themselves are peak, so both ends are treated
as inside, and a held execution resumes a minute after — outside the window
on either reading.

**The jitter answers the same circular's other half.** Row 5(a) asks that
executions be "initiated at moderated TPS", and a guard that released every
held execution at 21:31:00 would build the burst the circular exists to
prevent. So each resumes somewhere in the quarter-hour after, at an offset
derived from the payment rather than drawn, for the reason
`contact_window.window_jitter` gives: the same seed must replay to the same
ledger.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta

from vasool.diagnosis.rules import IST
from vasool.mandate.citations import cite
from vasool.mandate.record import MandateRail
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

CITATION = cite("NPCI-OC-215A row 5", "NPCI-OC-215A ¶3")

PEAK_HOURS_IST: tuple[tuple[time, time], ...] = ((time(10, 0), time(13, 0)), (time(17, 0), time(21, 30)))
"""Both ends inside. NPCI OC-215A/2025-26 ¶3."""

RESUME_AFTER_PEAK = timedelta(minutes=1)

EXECUTION_JITTER_MAX = timedelta(minutes=15)
"""# VERIFY: fifteen minutes is ours, matching the contact window's spread. The
circular asks for "moderated TPS" and names no number."""


def execution_jitter(entity_id: str) -> timedelta:
    """A stable per-payment offset into the quarter-hour after a peak."""
    digest = hashlib.sha256(entity_id.encode()).hexdigest()
    return timedelta(seconds=int(digest[:8], 16) % int(EXECUTION_JITTER_MAX.total_seconds()))


class AutopayPeakHoursGuard(Guard):
    name = "AutopayPeakHoursGuard"
    statute = "NPCI OC-215A/2025-26 — Autopay execution in non-peak hours"

    def applies_to(self, ctx: GuardContext) -> bool:
        mandate = ctx.facts.mandate
        return (
            ctx.proposal.is_retry
            and mandate is not None
            and mandate.rail is MandateRail.UPI_AUTOPAY
        )

    def check(self, ctx: GuardContext) -> Verdict:
        local = ctx.effective_at.astimezone(IST)
        for opens, closes in PEAK_HOURS_IST:
            if opens <= local.time() <= closes:
                resume = datetime.combine(local.date(), closes, tzinfo=IST) + RESUME_AFTER_PEAK
                return self.defer(
                    resume + execution_jitter(ctx.proposal.entity_id),
                    f"{local:%H:%M} IST is inside NPCI's {opens:%H:%M}-{closes:%H:%M} peak "
                    "window, when a UPI Autopay execution may not be initiated",
                )
        return self.allow()
