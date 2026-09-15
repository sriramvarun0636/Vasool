"""The mandate debit: how a SILENT_RETRY or TIMED_RETRY reaches the rail.

A port, like the pre-debit notice beside it (vasool/actions/notice.py), and for
the same reason: the call that performs it has never been observed on this
account. Razorpay documents one — `createRecurring`, against an order created
for the debit, with the mandate's token — and `RazorpayMandateDebiter` in
vasool/actions/executor.py implements exactly that. It is not the default.
Production keeps `NullMandateDebiter`, which refuses and says so, until one live
debit has been observed after activation (docs/EVALUATION.md §10, 2026-09-15).

**What a debit can come back as.** Taken, refused, or unknown. Unknown is the
one that matters: a timeout or a 5xx on a debit may have moved money, and the
answer to it is a status check, never a second debit — `outcome_unknown` is how
the state machine is told which (vasool/policy/machine.py).

**A one-time payment has nothing to debit.** Re-presenting a payment without
customer input needs a token, and a token exists only for a mandate. Nothing
documented re-presents a one-time card payment, so the documented adapter
refuses one rather than guessing at a call.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from vasool.diagnosis.proposal import Proposal
from vasool.mandate.record import MandateRecord

__all__ = ["DebitAttempt", "MandateDebiter", "MandateSource", "NullMandateDebiter"]

MandateSource = Callable[[str], MandateRecord | None]
"""The mandate a payment is presented under, by entity id — production's
mandate store, where it has one. The same record `PolicyFacts.mandate` carries."""


@dataclass(frozen=True, slots=True)
class DebitAttempt:
    """What asking the rail to debit came to."""

    ok: bool
    detail: str
    payment_id: str | None = None
    """The payment the rail created, which a later `payment.captured` or
    `payment.failed` names — what RetryIndex correlates through."""

    response: dict | None = None
    outcome_unknown: bool = False
    """The request may have taken effect and the response does not say. Only
    ever True on a failure."""


class MandateDebiter(Protocol):
    def debit(self, proposal: Proposal) -> DebitAttempt: ...


class NullMandateDebiter:
    """No debit call is wired. Refuses, and nothing reaches the rail."""

    def debit(self, proposal: Proposal) -> DebitAttempt:
        return DebitAttempt(
            ok=False,
            detail=(
                "no mandate debit is wired: Razorpay's documented call has not been "
                "observed on this account, so nothing is sent"
            ),
        )
