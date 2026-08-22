"""How many times the instrument may be re-presented, in total.

This is the *platform's* ceiling, and it is not the same number as the
taxonomy's per-row budget. §4 gives `card_expired` zero retries and
`gateway_technical_error` three; those are arguments about whether a retry could
possibly work, they are applied in classify(), and they are tighter than this in
every row. This guard is the outer bound: whatever proposed the action — the
rules, an LLM, a hand-built object — nothing exceeds what Razorpay itself will
tolerate before it halts the subscription.

Counted across the episode, not the event. Razorpay halts on *consecutive
failures* against a subscription, so a counter that reset per webhook would
measure the wrong thing entirely.

The one-time cap is stricter than the mandate cap on purpose. Nothing halts a
one-time payment on our behalf, so the restraint has to be ours.
"""
from __future__ import annotations

from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

MANDATE_ATTEMPT_CAP = 4
"""Razorpay halts a subscription after four consecutive failures.

# VERIFY: documented, never observed. Subscriptions are unavailable
# pre-activation on this account (docs/VERIFIED.md), so the halt rule could not
# be exercised and this number is taken from documentation — the one number in
# the policy plane that is.
"""

ONETIME_ATTEMPT_CAP = 3
"""Self-imposed. Nothing halts a one-time payment for us, so the ceiling is
ours to set, and it is set below the mandate cap rather than at it."""


class RetryCapGuard(Guard):
    name = "RetryCapGuard"
    statute = None
    """Razorpay's halt behaviour is platform behaviour, not regulation."""

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.is_retry

    def check(self, ctx: GuardContext) -> Verdict:
        cap = MANDATE_ATTEMPT_CAP if ctx.facts.is_mandate else ONETIME_ATTEMPT_CAP
        used = ctx.facts.attempts_used
        if used >= cap:
            kind = "mandate" if ctx.facts.is_mandate else "one-time payment"
            return self.block(
                f"{used} of {cap} attempts already spent on this {kind} — "
                "further re-presentation buys nothing and, on a mandate, is what "
                "triggers the halt"
            )
        return self.allow()
