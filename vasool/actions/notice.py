"""The pre-debit notification: the merchant's request for a notice the issuer sends.

RBI's E-mandate Framework, 2026, §6(a): "An issuer shall send a
pre-transaction notification to the customer, at least 24 hours prior to the
actual charge / debit." The merchant does not send it. For UPI its part is a
request through the rail — the payee's PSP calls NPCI's pre-debit notification
API, ReqValCust, 24 hours ahead (OC-149's annexure, code NU) — and the PSP and
the issuing bank then notify the customer (OC-151A ¶4). How a card mandate's
issuer is asked is in no source gathered here.

So this is a port (docs/EVALUATION.md §1.2's rule, via the programme's design),
not a message. `PreDebitNotifier` asks the rail to notify, and nothing about it
chooses a channel, a template or an hour: those belong to whoever sends the
notification, and that is not this system (docs/EVALUATION.md §10, 2026-09-15).

**The null adapter refuses, and says so.** No call that requests a pre-debit
notification has ever been observed on this account — subscriptions are
unavailable pre-activation (docs/VERIFIED.md). A notifier that cannot ask
reports that it did not, and the debit stays held by `PreDebitNoticeGuard`:
the safe direction, since a debit without its notice is the violation and a
notice not requested is only a delay.

**Razorpay documents the request, and it is built but not the default.** Its
*Create Subsequent Payments* pages have the merchant create an order carrying a
`notification` object with the mandate's `token_id`; Razorpay delivers the
notice, and the order then reports `notification.status` and `delivered_at`.
`RazorpayPreDebitNotifier` in vasool/actions/executor.py does that and records
the order in `NoticeOrders`, because the debit is presented against that same
order. It stays off until one live request has been observed after activation
(docs/EVALUATION.md §10, 2026-09-15).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vasool.diagnosis.proposal import Proposal


@dataclass(frozen=True, slots=True)
class NoticeRequest:
    """What asking the rail for a pre-debit notification came to."""

    ok: bool
    detail: str
    reference: str | None = None
    """The rail's reference for the request, where it returns one."""


class PreDebitNotifier(Protocol):
    def request(self, proposal: Proposal) -> NoticeRequest: ...


class NoticeOrders:
    """Which order a payment's pre-debit notice was requested on, by entity id.

    The debit that notice enables is presented against that same order
    (Razorpay's `createRecurring` requires its `order_id`), and the status
    check reads it. Process-local, like RetryIndex beside the executor, and for
    the same reason stated there: nothing yet stores the action plane's own
    call history durably. A restart loses the mapping, and the debit it would
    have found refuses rather than guessing an order.
    """

    def __init__(self) -> None:
        self._by_entity: dict[str, str] = {}

    def record(self, entity_id: str, order_id: str) -> None:
        self._by_entity[entity_id] = order_id

    def order_for(self, entity_id: str) -> str | None:
        return self._by_entity.get(entity_id)


class NullPreDebitNotifier:
    """No rail call is wired. Refuses, rather than pretending a notice went out."""

    def request(self, proposal: Proposal) -> NoticeRequest:
        return NoticeRequest(
            ok=False,
            detail=(
                "no pre-debit notification request is wired: the issuer sends the "
                "notice, and no call that asks for one has been observed on this account"
            ),
        )
