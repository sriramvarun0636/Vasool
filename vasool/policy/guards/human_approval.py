"""Above a merchant's threshold, a person decides. Last in the chain.

Self-imposed, and the last line of defence rather than a compliance rule: for a
large enough amount, the right answer is that an automated system does not act
unattended however clean the other twelve verdicts are.

Skips anything already bound for a human. Escalating an escalation would queue
the same item twice and make the queue depth a lie.
"""
from __future__ import annotations

from vasool.diagnosis.taxonomy import InterventionType
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict


class HumanApprovalGuard(Guard):
    name = "HumanApprovalGuard"
    statute = None

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.intervention is not InterventionType.HUMAN_QUEUE

    def check(self, ctx: GuardContext) -> Verdict:
        threshold = ctx.facts.merchant.human_approval_threshold_paise
        if ctx.proposal.amount_paise > threshold:
            return self.escalate(
                f"₹{ctx.proposal.amount_paise / 100:,.2f} is over this merchant's "
                f"₹{threshold / 100:,.2f} unattended-action threshold"
            )
        return self.allow()
