"""TRAI's Do Not Disturb registry.

Scope is the whole question here, and it is not settled. The registry governs
*promotional* traffic; transactional and service messages are treated
differently. Under TCCCPR a message's category is the category its template is
registered under on DLT — the merchant's registration, not the message's
content — so whether a payment-recovery message is transactional, service or
promotional is a fact about the merchant, and this system cannot know it.

**So an undeclared category is judged, not waved through.** The effective
category is the merchant's declaration for the proposal's template
(`PolicyFacts.template_categories`) when there is one, and the proposal's own
otherwise — which the diagnosis now always builds as UNKNOWN. This guard has
jurisdiction over PROMOTIONAL and over UNKNOWN. A merchant that declares a
template TRANSACTIONAL or SERVICE takes that message out of its jurisdiction,
and takes on the obligation of the declaration being true.

Until 2026-09-15 every contact was built as TRANSACTIONAL, so this guard
returned NOT_APPLICABLE for everything the system ever sent, and adversary
attack A09 — a message to a DND-listed customer — was registered as a failure
(docs/EVALUATION.md §10, 2026-09-15).

**Unknown blocks.** A registry that cannot answer leaves `dnd_listed` None, and
the guard base fails closed on a required fact it does not have: "unknown,
therefore blocked", never "unknown, therefore fine". `vasool/policy/
dnd_registry.py` is the port a real scrub will arrive through, and its null
adapter answers exactly that.

**Staleness blocks too.** In production `dnd_listed` is a network call. A scrub
from last month does not answer a registration made last week, and — more to
the point — a call that failed must not be indistinguishable from a clean
result.
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

JURISDICTION = frozenset({MessageCategory.PROMOTIONAL, MessageCategory.UNKNOWN})
"""The categories the registry is held to govern. UNKNOWN is here because an
unknown category is not a transactional one."""


def effective_category(ctx: GuardContext) -> MessageCategory | None:
    """The merchant's declaration for this template, else the proposal's own."""
    declared = ctx.facts.declared_category(ctx.proposal.template_id)
    return declared if declared is not None else ctx.proposal.message_category


class DNDGuard(Guard):
    name = "DNDGuard"
    statute = "TRAI TCCCPR 2018 (as amended Feb 2025)"
    requires = frozenset({"dnd_listed", "dnd_checked_at"})

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.is_contact and effective_category(ctx) in JURISDICTION

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
            return self.block(
                f"customer is on the DND registry, and this message's category is "
                f"{effective_category(ctx).value}"
            )
        return self.allow()
