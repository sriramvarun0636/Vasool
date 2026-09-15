"""A debit needs a live mandate.

The mandate is the only authority a recurring debit has. §4(b) of RBI's
E-mandate Framework, 2026 gives the customer a facility to withdraw it "at any
point of time", and a recovery retry is built days before it executes — the
salary ladder waits for payday — so the authority it was built on can be gone
by the time it runs. This guard reads the mandate at the moment the debit would
reach the rail, through `MandateRecord.state_at`, which also applies the two
transitions nobody has to apply: a validity period ending and a pause lapsing.

**It blocks, and does not defer — a pause included.** A paused mandate will
work again when the pause ends, so deferring to that moment is available. It is
not taken: the customer paused the mandate to stop being debited, and a retry
held until the pause lifts is that debit, collected anyway. The rail would
decline it now in any case (NPCI code VT, "MANDATE IS PAUSED"). Whether to ask
the customer for the money another way is a contact, and the contact guards
rule on that separately.
"""
from __future__ import annotations

from vasool.mandate.citations import cite
from vasool.mandate.states import MandateState
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

CITATION = cite(
    "RBI-EMF-2026 §4(a)",
    "RBI-EMF-2026 §4(b)",
    "NPCI-UPI-CODES-2.9 §3.1 VA",
    "NPCI-UPI-CODES-2.9 §3.1 VT",
    "NPCI-UPI-CODES-2.9 §3.1 VU",
)
"""The clauses behind every refusal below, quoted in vasool/mandate/citations.py."""

_REFUSALS: dict[MandateState, str] = {
    MandateState.CREATED: (
        "the mandate is not registered: registration completes only on the customer's "
        "additional factor of authentication"
    ),
    MandateState.PENDING_AUTH: (
        "the mandate is awaiting the customer's authentication and is not yet registered"
    ),
    MandateState.PAUSED: (
        "the customer has paused this mandate; a debit now is one they asked not to "
        "receive (NPCI VT, MANDATE IS PAUSED)"
    ),
    MandateState.REVOKED: (
        "the mandate has been revoked, and a withdrawn authority does not come back "
        "(NPCI VA, MANDATE HAS BEEN REVOKED)"
    ),
    MandateState.EXPIRED: (
        "the mandate's validity period has ended (NPCI VU, MANDATE HAS EXPIRED)"
    ),
}


class MandateStateGuard(Guard):
    name = "MandateStateGuard"
    statute = "RBI E-mandate Framework 2026 §4 — debit only under a live mandate"

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.facts.is_mandate and ctx.proposal.is_retry

    def check(self, ctx: GuardContext) -> Verdict:
        assert ctx.facts.mandate is not None  # is_mandate reads the record
        state = ctx.facts.mandate.state_at(ctx.effective_at)
        if state is MandateState.ACTIVE:
            return self.allow()
        return self.block(_REFUSALS[state])
