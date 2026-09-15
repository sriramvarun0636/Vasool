"""RBI e-mandate: the customer is notified before a recurring debit.

This guard is the clearest case of the rule that a guard describes and never
performs. It cannot request a notice — it is a pure function — so when one is
owed it defers the debit and returns an inert Obligation saying so. The state
machine turns that into a Proposal, and *that proposal goes through the guard
chain like any other*: an obligation that short-circuited into an executor
would be a hole straight through the policy plane, and describing it as a
proposal and re-gating it closes the hole structurally.

**The notice is the issuer's; the merchant only asks for it.** §6(a) of RBI's
E-mandate Framework, 2026: "An issuer shall send a pre-transaction
notification to the customer, at least 24 hours prior to the actual charge /
debit." For UPI the merchant's part is a request through the rail — the
payee's PSP calls NPCI's pre-debit notification API, ReqValCust, 24 hours ahead
(OC-149's annexure, code NU) — and the PSP and the issuing bank then notify the
customer (OC-151A ¶4). So the proposal the obligation becomes is not a
message: it has no channel and no template, the executor hands it to
`vasool/actions/notice.py` rather than to comms, and the rules written for a
merchant's messages — the contact window, DND, DLT, the contact caps — have no
jurisdiction over it. Until docs/EVALUATION.md §10, 2026-09-15, it was built as
the merchant's SMS and gated through all four, which held many debits for a
notice those rules refused.

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
"""The evidence that the issuer notifies, through the rail — and so why the
notice is not a contact (docs/EVALUATION.md §10, 2026-09-15)."""

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
