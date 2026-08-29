"""08:00-19:00 IST. The guard the whole pitch is built on.

A naive implementation blocks a 7:30pm nudge and loses the recovery. This one
defers it to just after eight: same compliance outcome, money still recovered.

Three things are worth reading closely.

**It reads `effective_at`, never `now`.** Adversary attack A04 is an action
queued at 18:58 that executes at 19:02; checked against the decision time it
passes, and the message lands outside the window. The design spec's own property
test in §6.3 asserts against `ctx.now` and so encodes the bug it is meant to
catch. The state machine closes the same gap from the other side by gating
immediately before execution rather than at propose time.

**It owns contact timing outright.** vasool/diagnosis/rules.py used to hold
outbound contact out of 00:00-06:00 as well, which meant two planes moving the
same action for the same reason — and 08:00-19:00 is a strict superset of that
window, so the earlier move never changed an outcome. taxonomy.md §6 already
argued these were two rules with different force: the retry half is an efficacy
hedge about issuer batch maintenance and stayed in the classifier, the contact
half is a permission rule about disturbing a human and belongs here. One rule,
one owner, and the compliance save now appears in the receipt instead of being
silently pre-applied upstream.

**The jitter is real, not decoration.** It is where the pitch's "8:02am" comes
from. Without it a merchant's entire overnight backlog fires at 08:00:00.000 —
a self-inflicted thundering herd, and a burst of simultaneous messages that
reads to a recipient exactly like the automated dunning the rule is trying to
prevent. It is derived from the customer id rather than drawn at random, because
CLAUDE.md invariant 5 requires the same seed to produce a byte-identical ledger.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from vasool.diagnosis.rules import IST
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

CONTACT_WINDOW_OPEN_HOUR_IST = 8
CONTACT_WINDOW_CLOSE_HOUR_IST = 19
"""RBI Fair Practices Code: recovery contact between 08:00 and 19:00 only.
Half-open — 19:00 itself is outside."""

CONTACT_JITTER_MAX = timedelta(minutes=15)
"""Spread over the first quarter-hour of the window.

# VERIFY: fifteen minutes is ours. It wants sizing against real backlog volume —
# large enough to flatten the burst, small enough that nobody waits meaningfully
# longer than the window's opening.
"""


def window_jitter(customer_id: str) -> timedelta:
    """A stable per-customer offset into the opening of the window.

    Deterministic and process-independent: sha256 rather than hash(), which is
    salted per process and would replay differently tomorrow.
    """
    digest = hashlib.sha256(customer_id.encode()).hexdigest()
    return timedelta(seconds=int(digest[:8], 16) % int(CONTACT_JITTER_MAX.total_seconds()))


class ContactWindowGuard(Guard):
    name = "ContactWindowGuard"
    statute = "RBI Fair Practices Code ¶55"
    # VERIFY: the paragraph number is from the design spec's research and was
    # not confirmed against the current Code (design spec §15 lists it as a
    # day-one check). The 08:00-19:00 window is the well-established part; the
    # citation is the part to check before anyone prints it.

    # Fixed 2026-08-30. This guard evaluated the window in IST unconditionally,
    # so a customer elsewhere was protected by the merchant's clock rather than
    # their own -- adversary attack A08 lands a message at 22:30 customer-local
    # by deferring an 03:00 IST failure to the opening of *our* window. It now
    # reads `facts.customer_zone` and falls back to IST when that is unknown,
    # which is every customer the simulator builds, so no evaluated number
    # moves. taxonomy.md §9.3 carries the before/after.

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.is_contact

    def check(self, ctx: GuardContext) -> Verdict:
        # The customer's zone where we have one, the merchant's where we do
        # not. The deferral target is computed in the same zone it was judged
        # in -- deferring to 08:00 of the *wrong* clock is the original defect,
        # not a smaller version of it.
        zone = ctx.facts.customer_zone or IST
        label = "IST" if zone is IST else f"{zone}"
        local = ctx.effective_at.astimezone(zone)
        if CONTACT_WINDOW_OPEN_HOUR_IST <= local.hour < CONTACT_WINDOW_CLOSE_HOUR_IST:
            return self.allow()

        opens = local.replace(
            hour=CONTACT_WINDOW_OPEN_HOUR_IST, minute=0, second=0, microsecond=0
        )
        if local.hour >= CONTACT_WINDOW_CLOSE_HOUR_IST:
            opens += timedelta(days=1)

        return self.defer(
            opens + window_jitter(ctx.proposal.customer_id),
            f"{local:%H:%M} {label} is outside the {CONTACT_WINDOW_OPEN_HOUR_IST:02d}:00-"
            f"{CONTACT_WINDOW_CLOSE_HOUR_IST:02d}:00 contact window",
        )
