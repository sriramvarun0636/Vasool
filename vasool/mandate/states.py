"""Where a mandate is in its life. Closed.

Six states, the design's, and no more. Each one's docstring says what a debit
presented against it would meet, because that is the only question the policy
plane ever asks of a mandate.
"""
from __future__ import annotations

from enum import StrEnum


class MandateState(StrEnum):
    CREATED = "CREATED"
    """The merchant has asked for a mandate; the customer has not seen it yet.
    No authority exists."""

    PENDING_AUTH = "PENDING_AUTH"
    """The request is with the customer. RBI's Framework registers a mandate
    only after the customer's additional factor of authentication (§4(a)), so
    until then there is still no authority."""

    ACTIVE = "ACTIVE"
    """Registered, authenticated and within its validity period: the only state
    a debit may be presented in."""

    PAUSED = "PAUSED"
    """The customer has paused it from their UPI app. A debit now is declined
    by the rail — NPCI response code VT, "MANDATE IS PAUSED" — and is one the
    customer has asked not to receive. Not terminal: a pause ends."""

    REVOKED = "REVOKED"
    """Debit authority withdrawn — by the customer (§4(b) of the Framework, and
    §6(c)'s opt-out of the e-mandate), by the merchant through its acquiring
    bank (NPCI OC-182 §3.2), or refused at registration. NPCI code VA."""

    EXPIRED = "EXPIRED"
    """Its time ran out: the validity period every mandate must specify has
    ended (§4(b)), or a registration request lapsed before the customer
    authenticated it (NPCI §4.4, U69, "COLLECT EXPIRED"). NPCI code VU."""


TERMINAL: frozenset[MandateState] = frozenset({MandateState.REVOKED, MandateState.EXPIRED})
"""Nothing leaves these. A customer who wants to be debited again registers a
new mandate, which is a new record with a new id, not this one revived."""
