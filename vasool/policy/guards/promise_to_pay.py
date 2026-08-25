"""No chasing during an active promise to pay.

The design spec calls this `QuietPeriodGuard`. Renamed, because
vasool/diagnosis/rules.py already uses "quiet period" for the 00:00-06:00 IST
exclusion, and two adjacent modules using one name for two unrelated rules is a
bug waiting to be written by whoever reads them next.

**It holds retries as well as contact.** The two source documents disagree:
design spec §6.2's guard table says "no contact during an active
promise-to-pay", while its own stopping-rule table says "hard stop ... until
promised date +1d". The broader reading is right. Debiting on the 3rd someone
who has promised to pay by the 5th is precisely the bad faith the Fair Practices
Code is about, and the fact that the debit is silent does not make it courteous.

**It defers, never blocks.** A promise is the strongest signal of intent
anywhere in the system. Killing a recovery because the customer said they would
pay would be the most perverse outcome the policy plane could produce.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import InterventionType
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

PROMISE_GRACE = timedelta(days=1)
"""Design spec §5: the hold runs "until promised date +1d".

Read as: we do not chase on the promised day, and the hold lifts at the start of
the following day. The grace is what stops us chasing at one minute past
midnight over a payment that settles during the promised day.

# VERIFY: whether the grace should instead run a full day *past* the promised
# date is a product judgment nobody has made. The stricter reading costs one day
# of latency on a customer who has already told us they intend to pay, which is
# the cheap direction to be wrong in.
"""


class PromiseToPayGuard(Guard):
    name = "PromiseToPayGuard"
    statute = "RBI Fair Practices Code (fair dealing)"
    # VERIFY: the Fair Practices Code's applicability to a payment-gateway
    # recovery flow is by analogy — it governs regulated lenders' recovery
    # conduct. We hold ourselves to it because the conduct is the same conduct,
    # not because we have established that it binds a merchant's agent.

    def applies_to(self, ctx: GuardContext) -> bool:
        """A human handoff is not automated chasing.

        A risk decline must reach an operator immediately.  Holding
        HUMAN_QUEUE until a customer's promise expires delays the very review
        that makes the risk path safe (A19).
        """
        return ctx.proposal.intervention is not InterventionType.HUMAN_QUEUE

    def check(self, ctx: GuardContext) -> Verdict:
        promise = ctx.facts.promise_to_pay
        if promise is None:
            return self.allow()

        lifts_on = promise + PROMISE_GRACE
        lifts_at = datetime(lifts_on.year, lifts_on.month, lifts_on.day, tzinfo=IST)
        if ctx.effective_at >= lifts_at:
            return self.allow()

        return self.defer(
            lifts_at,
            f"customer promised to pay by {promise.isoformat()}; chasing inside "
            "a promise is bad faith whether or not the customer notices",
        )
