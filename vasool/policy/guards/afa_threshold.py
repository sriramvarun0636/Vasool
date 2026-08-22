"""Additional Factor of Authentication above the e-mandate threshold.

Escalates rather than blocks. The action is not forbidden — it needs a factor an
unattended system cannot supply. Refusing it outright would throw away a
recoverable payment over a step a human can complete in a minute.

**What the spec asks for, and why this does something else.** §6.2's behaviour
column says "route to AFA flow". There is no AFA_LINK in InterventionType, and
taxonomy.py's first rule is that adding a member is a taxonomy change that
belongs in docs/taxonomy.md before it belongs in code — precisely so that a
guard cannot invent an action at a call site. So this escalates to the human
queue, which is the honest available action, and the AFA row stays an open
question for the taxonomy rather than a quiet enum edit here.
"""
from __future__ import annotations

from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

AFA_THRESHOLD_PAISE = 1_500_000
"""₹15,000 — the general RBI e-mandate limit above which each recurring debit
needs additional authentication.

# VERIFY: the design spec's research notes higher limits for insurance premiums,
# mutual-fund SIPs and credit-card bill payments, and §15 lists the current
# thresholds as a day-one check that was never completed. We apply the general
# limit to everything, which is the conservative direction — it escalates some
# debits that would have been permitted unattended, rather than executing any
# that should not have been.
"""


class AFAThresholdGuard(Guard):
    name = "AFAThresholdGuard"
    statute = "RBI e-mandate framework — AFA above ₹15,000"

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.facts.is_mandate and ctx.proposal.is_retry

    def check(self, ctx: GuardContext) -> Verdict:
        if ctx.proposal.amount_paise > AFA_THRESHOLD_PAISE:
            return self.escalate(
                f"₹{ctx.proposal.amount_paise / 100:,.2f} exceeds the ₹"
                f"{AFA_THRESHOLD_PAISE / 100:,.0f} e-mandate limit — this debit "
                "needs an authentication factor no unattended system can supply"
            )
        return self.allow()
