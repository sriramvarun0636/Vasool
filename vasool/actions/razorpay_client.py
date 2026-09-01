"""Thin wrapper over the Razorpay SDK.

The ONLY module allowed to import `razorpay` (architectural invariant 1's sibling
rule for the action plane — "only actions/executor.py may call Razorpay",
narrowed further here so even executor.py reaches the SDK through this one
seam). tests/test_actions_boundary.py enforces both boundaries by grepping
vasool/, the way tests/test_no_wallclock.py enforces the clock invariant.

Config comes from the environment via python-dotenv — never hardcoded
(the project rules "Secrets"). Every write call takes an idempotency key; every call
retries a 5xx with exponential backoff and never retries a 4xx, because a bad
request will be bad again and retrying it only delays the failure a 4xx is
trying to report.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

import razorpay
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
    is True only when every retry was exhausted on a 5xx, False on a 4xx that
    was never retried at all. A caller deciding whether to fall back to a
    different intervention reads this rather than re-deriving it from the
    wrapped exception's type.
    """

    def __init__(self, message: str, *, retryable: bool, cause: Exception) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.cause = cause


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
        cfg = config or RazorpayConfig.from_env()
        self._sdk = sdk_client or razorpay.Client(auth=(cfg.key_id, cfg.key_secret))
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds
        self._sleep = sleep

    def _with_retry(self, description: str, call: Callable[[], dict]) -> dict:
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
            except (GatewayError, ServerError) as exc:
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

    def retry_payment(
        self, *, entity_id: str, amount_paise: int, currency: str, idempotency_key: str
    ) -> dict:
        """Re-present the instrument behind a failed payment, with no new
        customer input. SILENT_RETRY and TIMED_RETRY's only Razorpay call.

        # VERIFY: no live mechanism for this was exercised in
        docs/VERIFIED.md. Session 0A never activated the merchant account, so
        the Razorpay primitives that support a merchant-initiated recharge
        without customer interaction — subscriptions and e-mandates — were
        never reachable to test ("subscriptions unavailable pre-activation").
        A one-time card payment has no saved token to recharge either:
        neither FailureEvent nor Proposal carries one, because a failed
        authorisation never produces one to save. This wraps
        `payment.createRecurring`, the SDK's documented token-based recharge
        call, as the best available mapping — unverified against live
        behaviour, not a confirmed fact. Calling it against a payment with no
        real token fails at Razorpay's boundary rather than silently doing
        the wrong thing, and the caller (executor.py) treats that failure
        exactly like any other RazorpayCallFailed.
        """
        data = {"amount": amount_paise, "currency": currency, "payment_id": entity_id}
        headers = {IDEMPOTENCY_HEADER: idempotency_key}
        return self._with_retry(
            "retry_payment",
            lambda: self._sdk.payment.createRecurring(data, headers=headers),
        )

    def fetch_payment(self, payment_id: str) -> dict:
        """Read-only. No idempotency key — nothing to de-duplicate."""
        return self._with_retry("fetch_payment", lambda: self._sdk.payment.fetch(payment_id))
