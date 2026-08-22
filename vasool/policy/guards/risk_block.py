"""A fraud engine declined this. Nothing automated, ever.

docs/taxonomy.md §2 argues this on four independent grounds — card-network rules
on retrying declined authorisations, the merchant's decline ratio, handing a
retry loop to a fraudster if it really was fraud, and the fact that an
unexpected payment link to a possibly-compromised customer is structurally
indistinguishable from phishing if it wasn't.

The taxonomy already routes both RISK_BLOCK rows to a human queue, so on the
rules path this guard never fires. That is the point of it. It exists for the
path where something *else* proposed the action: the Session-7 LLM classifier, a
hand-built proposal, a future taxonomy edit that gets a row wrong. This is the
containment, and it is why it keys on the failure class rather than on the
error string.

**Why not the error string.** §6.3's property test asserts on
`error_reason == "payment_risk_check_failed"`, which misses
`payment_failed`/`business` — the other row §4 routes to RISK_BLOCK, and the one
whose classification is precautionary rather than evidential. Keying on the
class covers both, and keeps covering any row added later.
"""
from __future__ import annotations

from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict


class RiskBlockGuard(Guard):
    name = "RiskBlockGuard"
    statute = "Card network norms on retrying declined authorisations"
    # VERIFY: the card networks' retry rules are referenced from Razorpay's and
    # the networks' public documentation, not from a scheme rulebook we hold.
    # The argument in taxonomy.md §2 does not depend on the citation being
    # exact — three of its four grounds stand without it — but the statute
    # column should not imply we have read the rulebook.

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.failure_class is FailureClass.RISK_BLOCK

    def check(self, ctx: GuardContext) -> Verdict:
        if ctx.proposal.is_contact:
            return self.block(
                "zero outbound on a risk decline: if the decline was a false "
                "positive, an unexpected payment message to a customer whose "
                "instrument may be compromised is structurally a phishing "
                "attack, and we would be training them to click one"
            )
        if ctx.proposal.intervention is not InterventionType.HUMAN_QUEUE:
            return self.block(
                f"{ctx.proposal.intervention.value} on a risk-declined payment: "
                "retrying a declined authorisation may breach network rules, "
                "degrades a decline ratio the merchant cannot repair, and if it "
                "was fraud it is the fraudster's tool. A human decides."
            )
        return self.allow()
