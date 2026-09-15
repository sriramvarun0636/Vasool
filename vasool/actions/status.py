"""The status check: asking the rail what happened to a debit, instead of asking again.

Two things send a proposal here, and both are cases where money may already
have moved. A failure whose reason says so — a pending payment, a timeout on or
after the debit, a deduction being refunded, a mandate already honoured for the
cycle (docs/taxonomy.md §12's `RECONCILE`). And a debit whose own response was
lost (vasool/actions/razorpay_client.py). A retry in either case can be a second
debit, so the one thing done is to look.

NPCI's protocol for looking is OC-215: the first check "after 90 seconds from
the initiation/authentication of the original transaction", and "maximum of 3
check transaction status APIs, preferably within 2 hours" (¶3–¶4). Razorpay says
the same thing from the merchant's side: "Do not create another subsequent
payment until you get the status of the previous one." The schedule is the
state machine's (vasool/policy/machine.py); this module is the port.

**The null adapter cannot tell, and cannot-tell escalates.** No status call has
been observed on this account. `RazorpayOrderStatusCheck` in
vasool/actions/executor.py reads the documented order entity and is not the
default (docs/EVALUATION.md §10, 2026-09-15).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vasool.diagnosis.proposal import Proposal
from vasool.policy.machine import RailStatus

__all__ = ["NullStatusCheck", "StatusCheck", "StatusReading"]


@dataclass(frozen=True, slots=True)
class StatusReading:
    answer: RailStatus
    detail: str
    amount_paise: int | None = None
    """What the rail reports as debited, where it says. Set only with DEBITED."""

    reference: str | None = None


class StatusCheck(Protocol):
    def check(self, proposal: Proposal) -> StatusReading: ...


class NullStatusCheck:
    """No status call is wired. Says so, and a human decides."""

    def check(self, proposal: Proposal) -> StatusReading:
        return StatusReading(
            answer=RailStatus.CANNOT_TELL,
            detail=(
                "no status check is wired: whether the rail took this debit cannot be "
                "read on this account, so a person decides"
            ),
        )
