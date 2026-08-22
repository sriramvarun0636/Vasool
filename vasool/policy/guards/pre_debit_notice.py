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

# VERIFY: this whole path is stub-only. Subscriptions are unavailable
# pre-activation on this account (docs/VERIFIED.md), so no mandate debit has
# ever been observed, and `is_mandate` is a fact a simulator sets rather than
# one any payload carries.
"""
from __future__ import annotations

from datetime import timedelta

from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Obligation, ObligationKind, Verdict

PRE_DEBIT_NOTICE_LEAD = timedelta(hours=24)
"""How far ahead of a mandate debit the customer must be notified.

# VERIFY: 24h is from the design spec's research on the RBI e-mandate framework
# and is on the day-one checklist (§15) as unconfirmed. The requirement itself
# is well established; the exact lead time is the part to check.
"""


class PreDebitNoticeGuard(Guard):
    name = "PreDebitNoticeGuard"
    statute = "RBI e-mandate framework — pre-debit notification"

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
