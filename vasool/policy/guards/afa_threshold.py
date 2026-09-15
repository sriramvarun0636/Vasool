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

**The limit depends on what the mandate pays for.** §8(a) of RBI's E-mandate
Framework, 2026 lets every recurring debit run without AFA "up to ₹15,000/-";
§8(b) raises that to ₹1,00,000 for insurance premiums, mutual-fund
subscriptions and credit-card bills. NPCI applies the higher tier by merchant
category code (OC-151A: "more than ₹1,00,000/-" needs the UPI PIN, "less than or
equal to" does not), so both limits are inclusive — exactly ₹15,000 passes and
one paisa more does not — and the category is read from the mandate record,
not held here as one constant.
"""
from __future__ import annotations

from vasool.mandate.citations import cite
from vasool.mandate.record import MandateCategory
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

CITATION = cite(
    "RBI-EMF-2026 §8(a)", "RBI-EMF-2026 §8(b)", "NPCI-OC-151A ¶1", "NPCI-OC-151A Annexure A"
)

AFA_THRESHOLD_PAISE = 1_500_000
"""₹15,000 — §8(a), for every recurring debit not in a higher tier."""

AFA_HIGHER_TIER_PAISE = 10_000_000
"""₹1,00,000 — §8(b), for the three categories below."""

HIGHER_TIER: frozenset[MandateCategory] = frozenset(
    {
        MandateCategory.INSURANCE_PREMIUM,
        MandateCategory.MUTUAL_FUND,
        MandateCategory.CREDIT_CARD_BILL,
    }
)


def afa_limit_paise(category: MandateCategory) -> int:
    """The largest debit this category may take without additional authentication."""
    return AFA_HIGHER_TIER_PAISE if category in HIGHER_TIER else AFA_THRESHOLD_PAISE


class AFAThresholdGuard(Guard):
    name = "AFAThresholdGuard"
    statute = "RBI E-mandate Framework 2026 §8 — AFA above ₹15,000, or ₹1,00,000 by category"

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.facts.is_mandate and ctx.proposal.is_retry

    def check(self, ctx: GuardContext) -> Verdict:
        assert ctx.facts.mandate is not None  # is_mandate reads the record
        limit = afa_limit_paise(ctx.facts.mandate.category)
        if ctx.proposal.amount_paise > limit:
            return self.escalate(
                f"₹{ctx.proposal.amount_paise / 100:,.2f} exceeds the ₹"
                f"{limit / 100:,.0f} e-mandate limit for this mandate's category — this "
                "debit needs an authentication factor no unattended system can supply"
            )
        return self.allow()
