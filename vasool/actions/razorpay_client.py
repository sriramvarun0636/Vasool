"""Thin wrapper over the Razorpay SDK.

The ONLY module allowed to import `razorpay` (architectural invariant 1's sibling
rule for the action plane — "only actions/executor.py may call Razorpay",
narrowed further here so even executor.py reaches the SDK through this one
seam). tests/test_actions_boundary.py enforces both boundaries by grepping
vasool/, the way tests/test_no_wallclock.py enforces the clock invariant.

Config comes from the environment via python-dotenv — never hardcoded
(the project rules "Secrets"). Every write call takes an idempotency key. A 4xx
is never retried, because a bad request will be bad again and retrying it only
delays the failure a 4xx is trying to report.

**A write that moves money is never re-sent.** Every other call retries a 5xx,
a timeout or a dropped connection with exponential backoff. A debit does not:
a 5xx or a lost response on `createRecurring` means the rail may already have
taken the money, and the only thing that can say whether it did is a status
check — NPCI OC-215 ¶3–¶4, and Razorpay's own "Do not create another
subsequent payment until you get the status of the previous one." So a failed
debit raises at once, marked `outcome_unknown`, and the caller asks the rail
instead of asking again. Until 2026-09-15 the debit was re-sent up to four
times on a gateway error, resting on an idempotency header never seen honoured,
and a timeout escaped this module as a raw `requests` exception, unrecorded
(docs/EVALUATION.md §10, 2026-09-15).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

import razorpay
import requests
from razorpay.errors import BadRequestError, GatewayError, ServerError

log = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "X-Razorpay-Idempotency-Key"
"""# VERIFY: named per Razorpay's public API documentation for idempotent
Payment Links / Orders creation. Never exercised against a live idempotent
replay in docs/VERIFIED.md — Session 0A never issued the same key twice
against the real API, so whether Razorpay actually de-duplicates on it is
documentation, not an observed fact. Included regardless: a client that sends
every write exactly once and hopes is not idempotent at all, and the header
costs nothing to send even if Razorpay silently ignores it."""

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_SECONDS = 1.0


class RazorpayCallFailed(Exception):
    """Wraps whatever the SDK raised, so callers depend on this module's
    boundary rather than on razorpay.errors directly.

    `retryable` records what actually happened, not what was attempted — it
    is True only when every retry was exhausted on a 5xx or a transport
    failure, False on a 4xx that was never retried at all, and False on a
    money-moving write, which is never retried. A caller deciding whether to
    fall back to a different intervention reads this rather than re-deriving
    it from the wrapped exception's type.

    `outcome_unknown` is True only for a write that moves money whose effect
    cannot be known from the response: a 5xx, a timeout, a dropped connection
    or an unreadable body. It is the one failure a caller must never answer
    with the same call again.
    """

    def __init__(
        self, message: str, *, retryable: bool, cause: Exception, outcome_unknown: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.cause = cause
        self.outcome_unknown = outcome_unknown


_UNCERTAIN = (GatewayError, ServerError, requests.exceptions.RequestException)
"""Failures after which the request may or may not have taken effect. The SDK
raises `requests` exceptions unwrapped when a response is lost or unreadable
(its own retry is off by default), so they are caught here with the 5xx."""


@dataclass(frozen=True, slots=True)
class RazorpayConfig:
    key_id: str
    key_secret: str

    @classmethod
    def from_env(cls) -> RazorpayConfig:
        """Reads RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET. Does not call
        dotenv.load_dotenv() itself — that is an entrypoint's job, done once,
        not something a client constructor should have side effects on."""
        key_id = os.environ.get("RAZORPAY_KEY_ID")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RuntimeError(
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. Read via "
                "python-dotenv from .env — see .env.example. Never hardcode a "
                "key (the project rules)."
            )
        return cls(key_id=key_id, key_secret=key_secret)


class RazorpayClient:
    """Every method: an idempotency key is required, a 5xx is retried with
    exponential backoff, a 4xx never is.

    `sdk_client` and `sleep` are injectable so tests never make a real API
    call or a real wait (the project's "no real API calls in tests" and the
    project's ban on unmocked wall-clock waits in a test suite that has to
    stay fast).

    **Credentials are resolved only when this class has to build a client.**
    An injected `sdk_client` is already authenticated by whoever built it, so
    reading `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` on that path asks for a
    secret that is never used. That is not a style point: `cfg` used to be
    computed before the `sdk_client` branch, so every test in
    tests/test_razorpay_client.py — all of which inject a fake and none of
    which touch the network — demanded credentials, and the suite therefore
    passed only on a machine that happened to have a populated .env. On a
    fresh clone it was eight failures. A test suite whose result depends on
    the environment it runs in is not a test suite, and this project's whole
    claim is that a clean clone reproduces the artifact.
    """

    def __init__(
        self,
        *,
        config: RazorpayConfig | None = None,
        sdk_client: razorpay.Client | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if sdk_client is not None:
            self._sdk = sdk_client
        else:
            cfg = config or RazorpayConfig.from_env()
            self._sdk = razorpay.Client(auth=(cfg.key_id, cfg.key_secret))
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds
        self._sleep = sleep

    def _with_retry(
        self, description: str, call: Callable[[], dict], *, moves_money: bool = False
    ) -> dict:
        attempt = 0
        while True:
            attempt += 1
            try:
                return call()
            except BadRequestError as exc:
                raise RazorpayCallFailed(
                    f"{description}: bad request, not retrying: {exc}",
                    retryable=False,
                    cause=exc,
                ) from exc
            except _UNCERTAIN as exc:
                if moves_money:
                    raise RazorpayCallFailed(
                        f"{description}: {type(exc).__name__} — the rail may have taken "
                        f"the money, so this is not re-sent; a status check decides: {exc}",
                        retryable=False,
                        cause=exc,
                        outcome_unknown=True,
                    ) from exc
                if attempt >= self._max_attempts:
                    raise RazorpayCallFailed(
                        f"{description}: exhausted {self._max_attempts} attempts: {exc}",
                        retryable=True,
                        cause=exc,
                    ) from exc
                delay = self._base_delay_seconds * (2 ** (attempt - 1))
                log.warning(
                    "%s: attempt %d/%d failed (%s), retrying in %.1fs",
                    description,
                    attempt,
                    self._max_attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str,
        description: str,
        notes: dict,
        idempotency_key: str,
        expire_by: int | None = None,
    ) -> dict:
        """REATTEMPT_LINK / REAUTH_LINK's Razorpay call (design spec §12.1:
        "Payment links / re-auth — Payment Links API").

        No `customer` block: derive_customer_id (vasool/events/schemas.py) is
        a one-way HMAC by design, so nothing downstream of it recovers a raw
        phone number or email to hand Razorpay. `notify` is forced off —
        comms.py owns the customer-facing message, not Razorpay's own
        notification, so DLTTemplateGuard's template stays the one actually
        sent.
        """
        data: dict = {
            "amount": amount_paise,
            "currency": currency,
            "description": description,
            "notes": notes,
            "notify": {"sms": False, "email": False},
        }
        if expire_by is not None:
            data["expire_by"] = expire_by
        headers = {IDEMPOTENCY_HEADER: idempotency_key}
        return self._with_retry(
            "create_payment_link",
            lambda: self._sdk.payment_link.create(data, headers=headers),
        )

    def notify_payment_link(self, *, payment_link_id: str, medium: str, idempotency_key: str) -> dict:
        """Razorpay's own delivery for a message that already carries a
        payment link. `medium` is "sms" or "email" — the SDK's `notifyBy`
        signature; there is no "whatsapp" medium, which is why
        Channel.WHATSAPP has no path through this method (see executor.py)."""
        headers = {IDEMPOTENCY_HEADER: idempotency_key}
        return self._with_retry(
            "notify_payment_link",
            lambda: self._sdk.payment_link.notifyBy(payment_link_id, medium, headers=headers),
        )

    def create_notice_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        token_id: str,
        notes: dict,
        idempotency_key: str,
    ) -> dict:
        """The order a mandate debit is presented against, carrying the
        `notification` object that has Razorpay deliver the pre-debit notice.

        Razorpay, *Create Subsequent Payments* (UPI and cards): "You can use
        the notification object in the request if you want to control
        pre-debit notifications and recurring debits", with `token_id`
        mandatory. The object is always passed: without it "we will
        automatically try to debit 25 hours after the pre-debit notification
        is delivered", and Razorpay's own retries stacked on this agent's
        would exceed NPCI's one attempt and three retries per sequence number.
        `payment_after` is left to Razorpay's default.

        # VERIFY: documented, never observed — subscriptions and UPI are
        # unavailable on this account before activation (docs/VERIFIED.md).
        # And the default `payment_after` is "25 hours after the pre-debit
        # notification is delivered", an hour past RBI's 24: a debit this
        # agent presents between the two may be refused.
        """
        data: dict = {
            "amount": amount_paise,
            "currency": currency,
            "payment_capture": True,
            "notification": {"token_id": token_id},
            "notes": notes,
        }
        headers = {IDEMPOTENCY_HEADER: idempotency_key}
        return self._with_retry("create_notice_order", lambda: self._sdk.order.create(data, headers=headers))

    def fetch_order(self, order_id: str) -> dict:
        """Read-only: an order's `status` (created, attempted, paid), its
        `attempts`, and its `notification` with `status` and `delivered_at`."""
        return self._with_retry("fetch_order", lambda: self._sdk.order.fetch(order_id))

    def fetch_customer(self, customer_id: str) -> dict:
        """Read-only. The customer's email and contact, which a recurring
        payment requires and this system deliberately never holds — read at
        call time and never stored (vasool/events/schemas.py, derive_customer_id)."""
        return self._with_retry("fetch_customer", lambda: self._sdk.customer.fetch(customer_id))

    def create_recurring_payment(
        self,
        *,
        email: str,
        contact: str,
        amount_paise: int,
        currency: str,
        order_id: str,
        customer_id: str,
        token_id: str,
        notes: dict,
        idempotency_key: str,
    ) -> dict:
        """Present a mandate debit: SILENT_RETRY and TIMED_RETRY's Razorpay call.

        The eight fields Razorpay's *Create Subsequent Payments* marks
        mandatory — `email`, `contact`, `currency`, `amount`, `order_id`,
        `customer_id`, `token`, `recurring` — and the optional `notes`, which
        carries this episode's id back on the payment. It replaces
        `retry_payment`, which sent a `payment_id` the documentation does not
        name and omitted six of those eight (docs/EVALUATION.md §10,
        2026-09-15). The documented success response is `razorpay_payment_id`.

        **Never re-sent.** See this module's docstring.

        # VERIFY: documented, never observed. No recurring payment has been
        # created on this account, which cannot hold a token before activation.
        """
        data = {
            "email": email,
            "contact": contact,
            "amount": amount_paise,
            "currency": currency,
            "order_id": order_id,
            "customer_id": customer_id,
            "token": token_id,
            "recurring": True,
            "notes": notes,
        }
        headers = {IDEMPOTENCY_HEADER: idempotency_key}
        return self._with_retry(
            "create_recurring_payment",
            lambda: self._sdk.payment.createRecurring(data, headers=headers),
            moves_money=True,
        )

    def fetch_payment(self, payment_id: str) -> dict:
        """Read-only. No idempotency key — nothing to de-duplicate."""
        return self._with_retry("fetch_payment", lambda: self._sdk.payment.fetch(payment_id))
