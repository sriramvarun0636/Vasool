"""DPDP: valid consent, matching purpose, not withdrawn.

Two rules, not one, because consent does not cover everything equally.

**Contact needs consent for this purpose.** Purpose limitation is the substance
of the regime: a record listing "marketing" does not authorise a dunning
message, and treating any consent as blanket consent would be the most ordinary
way to get this wrong.

**A silent retry does not.** Re-presenting an instrument the customer already
authorised is not a communication — the lawful basis is the mandate or the
contract, not permission to be messaged. Blocking a retry for want of *messaging*
consent would refuse the one intervention in the taxonomy that disturbs nobody.

**Withdrawal stops everything.** Not just contact. The design spec's stopping
rule is "immediate, purge queue", and adversary attack A12 is specifically about
a system that mutes the messages and quietly keeps charging. A withdrawal is a
signal about the relationship, not about a channel.

Purging is the state machine's job — a guard is pure and cannot empty a queue.
This is the backstop that catches anything already in flight.
"""
from __future__ import annotations

from vasool.policy.facts import CONSENT_PURPOSE_RECOVERY, GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict


class ConsentGuard(Guard):
    name = "ConsentGuard"
    statute = "DPDP Act 2023 s.6 + DPDP Rules 2025"
    # VERIFY: the DPDP Rules 2025 commencement timeline is on the day-one
    # checklist (design spec §15) and was not confirmed. The section reference
    # for consent is from the Act as passed.

    requires = frozenset({"consent"})
    """None means no record, which is unknown rather than absent. Fail closed:
    a customer we hold no consent record for is not a customer who consented."""

    def check(self, ctx: GuardContext) -> Verdict:
        consent = ctx.facts.consent
        assert consent is not None  # guaranteed by `requires`

        if consent.is_withdrawn(ctx.effective_at):
            return self.block(
                "consent withdrawn — a hard stop on the whole episode, not a "
                "mute on the messaging half"
            )
        if ctx.proposal.is_contact and not consent.covers(
            CONSENT_PURPOSE_RECOVERY, ctx.effective_at
        ):
            return self.block(
                f"no live consent for {CONSENT_PURPOSE_RECOVERY!r} — consent held "
                f"for {sorted(consent.purposes)} does not extend to it"
            )
        return self.allow()
