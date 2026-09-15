"""RBI e-mandate: notify the customer before a recurring debit.

This guard is the clearest case of the rule that a guard describes and never
performs. It cannot send a notice — it is a pure function — so when one is owed
it defers the debit and returns an inert Obligation saying so. The state machine
turns that into a Proposal, and *that proposal goes through the guard chain like
any other*.

That last part is the bit worth being careful about. A pre-debit notice is a
customer contact. A notice generated at 03:00 and sent immediately would violate
the very contact window the rest of this package enforces, and a design where an
obligation short-circuits into an executor is a hole straight through the policy
plane. Describing it as a proposal and re-gating it costs one extra pass and
closes the hole structurally.

**Who sends the notice, as the sources have it — recorded, not acted on.** §6(a)
of RBI's E-mandate Framework, 2026 says "An issuer shall send a pre-transaction
notification", and for UPI the merchant's side of that is a request through the
rail: the payee's PSP calls NPCI's pre-debit notification API, ReqValCust, 24
hours ahead (OC-149's annexure, code NU), and the PSP and the issuing bank
notify the customer (OC-151A ¶4). So the notice is not a merchant SMS. The
machine still builds it as one, gated through the contact window, DND, DLT and
the frequency cap — conservative rather than unsafe, and a change that moves
numbers, so it waits for its own row (docs/EVALUATION.md §10, 2026-09-15).

# VERIFY: this whole path is stub-only. Subscriptions are unavailable
# pre-activation on this account (docs/VERIFIED.md), so no mandate debit has
# ever been observed, and the mandate record is one a simulator builds rather
# than one any payload carries.
"""
from __future__ import annotations

from datetime import timedelta

from vasool.mandate.citations import cite
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Obligation, ObligationKind, Verdict

CITATION = cite("RBI-EMF-2026 §6(a)")
"""The rule this guard enforces: the 24 hours."""

WHO_SENDS_IT = cite("RBI-EMF-2026 §6(a)", "NPCI-OC-149A Annexure NU", "NPCI-OC-151A ¶4")
"""The evidence for the finding above — the issuer notifies, through the rail —
kept resolvable so the finding cannot drift from its sources before it is acted
on."""

PRE_DEBIT_NOTICE_LEAD = timedelta(hours=24)
"""How far ahead of a mandate debit the customer must be notified: "at least 24
hours prior to the actual charge / debit" (RBI E-mandate Framework, 2026, §6(a)).
Inclusive — a notice exactly 24 hours old has matured."""


class PreDebitNoticeGuard(Guard):
    name = "PreDebitNoticeGuard"
    statute = "RBI E-mandate Framework 2026 §6(a) — pre-debit notification, 24 hours"

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.facts.is_mandate and ctx.proposal.is_retry

    def check(self, ctx: GuardContext) -> Verdict:
        sent_at = ctx.facts.pre_debit_notice_sent_at
        if sent_at is None:
            return self.defer(
                ctx.effective_at + PRE_DEBIT_NOTICE_LEAD,
                "no pre-debit notice has been served for this debit",
                obligations=(
                    Obligation(
                        kind=ObligationKind.SEND_PRE_DEBIT_NOTICE,
                        not_before=ctx.now,
                        reason=(
                            "the debit is held until the customer has had "
                            f"{PRE_DEBIT_NOTICE_LEAD} of notice"
                        ),
                    ),
                ),
            )

        deadline = sent_at + PRE_DEBIT_NOTICE_LEAD
        if ctx.effective_at >= deadline:
            return self.allow()
        return self.defer(
            deadline,
            f"notice served {sent_at.isoformat()}; the customer is owed the full "
            f"{PRE_DEBIT_NOTICE_LEAD} before the account is touched",
        )
