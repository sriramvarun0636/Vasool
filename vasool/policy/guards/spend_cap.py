"""The merchant's daily blast radius.

A ceiling on the value re-presented in one day. Self-imposed — this is an
operability limit, not regulation, and it must not appear in the report card's
statute column.

**Defers where the spec blocks.** A daily ceiling expires: it resets. A recovery
killed because it happened to arrive late in the queue on a busy day is lost
revenue for no compliance gain, and there is a real instant to name. So it
defers, and logs loudly — hitting the cap is operational signal about the day,
not about the payment.

**Except when deferring could never help.** A single retry worth more than the
entire daily ceiling would breach the cap tomorrow, and the day after, forever.
That is not a condition that expires, so it blocks. This is the edge case the
defer-iff-it-expires rule exists to catch, and it is easy to miss because the
guard's usual behaviour is so obviously a deferral.

**The deferral respects the retry quiet hours.** Splitting the quiet-hours rule
between the planes left one gap: the classifier holds retries out of 00:00-06:00
IST at classify time, and nothing re-checks it afterwards, so a guard deferring
to midnight would walk an action straight back into the window. Deferring to the
first legal moment instead closes it, and reuses the classifier's arithmetic
rather than restating it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from vasool.diagnosis.rules import IST, hold_out_of_quiet_hours
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

log = logging.getLogger(__name__)


def _next_reset(effective_at: datetime) -> datetime:
    """The first legal moment of the next IST day."""
    local = effective_at.astimezone(IST)
    midnight = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return hold_out_of_quiet_hours(midnight)


class SpendCapGuard(Guard):
    name = "SpendCapGuard"
    statute = None
    """Self-imposed. A merchant's own ceiling is not a clause."""

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.is_retry

    def check(self, ctx: GuardContext) -> Verdict:
        cap = ctx.facts.merchant.daily_retry_cap_paise
        amount = ctx.proposal.amount_paise

        if amount > cap:
            return self.block(
                f"a single ₹{amount / 100:,.2f} retry exceeds the whole ₹"
                f"{cap / 100:,.2f} daily ceiling — no reset makes it fit, so "
                "deferring it would defer it forever"
            )

        if ctx.facts.spent_today_paise + amount > cap:
            log.warning(
                "merchant %s daily retry cap reached: %d + %d > %d — deferring",
                ctx.facts.merchant.merchant_id,
                ctx.facts.spent_today_paise,
                amount,
                cap,
            )
            return self.defer(
                _next_reset(ctx.effective_at),
                f"₹{ctx.facts.spent_today_paise / 100:,.2f} already re-presented "
                f"today against a ₹{cap / 100:,.2f} ceiling",
            )
        return self.allow()
