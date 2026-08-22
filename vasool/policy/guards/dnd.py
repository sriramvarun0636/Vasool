"""TRAI's Do Not Disturb registry.

Scope is the whole question here, and it is not settled. The registry governs
*promotional* traffic; transactional and service messages are treated
differently. A payment-recovery message concerns a payment the customer already
initiated, which is the argument for calling it transactional — and that is how
diagnosis/proposal.py categorises it, which means in practice this guard returns
NOT_APPLICABLE for everything the rules classifier currently emits.

That is the honest implementation, not a shortcut, and the uncertainty is
recorded where it lives: on MessageCategory. If a telecom operator or the
merchant's own DLT registration categorises dunning as promotional, this guard
becomes load-bearing overnight and it is already wired.

**Staleness blocks.** In production `dnd_listed` is a network call. A scrub from
last month does not answer a registration made last week, and — more to the
point — a call that failed must not be indistinguishable from a clean result.
"""
from __future__ import annotations

from datetime import timedelta

from vasool.diagnosis.proposal import MessageCategory
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

DND_FACT_TTL = timedelta(days=7)
"""How long a registry scrub is trusted.

# VERIFY: ours, not TRAI's. The regime's actual scrub-frequency obligation sits
# with the registered sender and was not confirmed (design spec §15). Seven days
# is short enough that a fresh registration is caught within a week and long
# enough not to make every message a network round-trip.
"""


class DNDGuard(Guard):
    name = "DNDGuard"
    statute = "TRAI TCCCPR 2018 (as amended Feb 2025)"
    requires = frozenset({"dnd_listed", "dnd_checked_at"})

    def applies_to(self, ctx: GuardContext) -> bool:
        return (
            ctx.proposal.is_contact
            and ctx.proposal.message_category is MessageCategory.PROMOTIONAL
        )

    def check(self, ctx: GuardContext) -> Verdict:
        checked_at = ctx.facts.dnd_checked_at
        assert checked_at is not None  # guaranteed by `requires`

        age = ctx.effective_at - checked_at
        if age > DND_FACT_TTL:
            return self.block(
                f"DND scrub is {age.days}d old, past the {DND_FACT_TTL.days}d TTL — "
                "a stale scrub and a failed one look identical, so neither is trusted"
            )
        if ctx.facts.dnd_listed:
            return self.block("customer is on the DND registry")
        return self.allow()
