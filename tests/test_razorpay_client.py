"""razorpay_client.py: idempotency keys on every call, exponential backoff on
a 5xx, no retry on a 4xx. No real API calls — every test injects a fake SDK
client and a fake sleep, per the project's "no real API calls in tests".
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from razorpay.errors import BadRequestError, GatewayError, ServerError

from vasool.actions.razorpay_client import (
    IDEMPOTENCY_HEADER,
    RazorpayCallFailed,
    RazorpayClient,
    RazorpayConfig,
)


def make_client(sdk, *, sleeps=None, **kwargs) -> RazorpayClient:
    sink = sleeps if sleeps is not None else []
    kwargs.setdefault("base_delay_seconds", 0.01)
    return RazorpayClient(sdk_client=sdk, sleep=sink.append, **kwargs)


class TestRetryOn5xx:
    def test_a_gateway_error_is_retried_until_it_succeeds(self):
        sdk = Mock()
        sdk.payment_link.create.side_effect = [
            GatewayError("blip"),
            {"id": "plink_1", "short_url": "https://rzp.io/l/x"},
        ]
        sleeps: list[float] = []
        client = make_client(sdk, sleeps=sleeps)

        result = client.create_payment_link(
            amount_paise=10000, currency="INR", description="d", notes={}, idempotency_key="k1"
        )

        assert result["id"] == "plink_1"
        assert sdk.payment_link.create.call_count == 2
        assert len(sleeps) == 1

    def test_a_server_error_backs_off_exponentially(self):
        sdk = Mock()
        sdk.payment_link.create.side_effect = [ServerError("x"), ServerError("x"), {"id": "plink_1"}]
        sleeps: list[float] = []
        client = make_client(sdk, sleeps=sleeps, base_delay_seconds=1.0)

        client.create_payment_link(
            amount_paise=10000, currency="INR", description="d", notes={}, idempotency_key="k1"
        )

        assert sleeps == [1.0, 2.0]

    def test_5xx_exhausts_the_retry_budget_and_raises(self):
        sdk = Mock()
        sdk.payment_link.create.side_effect = [ServerError("down")] * 10
        client = make_client(sdk, max_attempts=3)

        with pytest.raises(RazorpayCallFailed) as excinfo:
            client.create_payment_link(
                amount_paise=10000, currency="INR", description="d", notes={}, idempotency_key="k1"
            )

        assert excinfo.value.retryable is True
        assert sdk.payment_link.create.call_count == 3


class TestNoRetryOn4xx:
    def test_a_bad_request_is_never_retried(self):
        sdk = Mock()
        sdk.payment_link.create.side_effect = [BadRequestError("nope")]
        sleeps: list[float] = []
        client = make_client(sdk, sleeps=sleeps)

        with pytest.raises(RazorpayCallFailed) as excinfo:
            client.create_payment_link(
                amount_paise=10000, currency="INR", description="d", notes={}, idempotency_key="k1"
            )

        assert excinfo.value.retryable is False
        assert sdk.payment_link.create.call_count == 1
        assert sleeps == []


class TestIdempotency:
    def test_every_write_call_carries_the_idempotency_header(self):
        sdk = Mock()
        sdk.payment_link.create.return_value = {"id": "plink_1"}
        client = make_client(sdk)

        client.create_payment_link(
            amount_paise=10000, currency="INR", description="d", notes={}, idempotency_key="my-key"
        )

        _, kwargs = sdk.payment_link.create.call_args
        assert kwargs["headers"][IDEMPOTENCY_HEADER] == "my-key"

    def test_notify_payment_link_carries_the_idempotency_header(self):
        sdk = Mock()
        sdk.payment_link.notifyBy.return_value = {"success": True}
        client = make_client(sdk)

        client.notify_payment_link(payment_link_id="plink_1", medium="sms", idempotency_key="k2")

        args, kwargs = sdk.payment_link.notifyBy.call_args
        assert args == ("plink_1", "sms")
        assert kwargs["headers"][IDEMPOTENCY_HEADER] == "k2"

    def test_retry_payment_carries_the_idempotency_header(self):
        sdk = Mock()
        sdk.payment.createRecurring.return_value = {"id": "pay_1"}
        client = make_client(sdk)

        client.retry_payment(entity_id="pay_1", amount_paise=500, currency="INR", idempotency_key="k3")

        _, kwargs = sdk.payment.createRecurring.call_args
        assert kwargs["headers"][IDEMPOTENCY_HEADER] == "k3"


class TestPaymentLinkNeverCarriesRawContact:
    def test_create_payment_link_sends_no_customer_block(self):
        """derive_customer_id (vasool/events/schemas.py) is a one-way HMAC —
        nothing downstream should be reconstructing a phone number or email
        to hand Razorpay."""
        sdk = Mock()
        sdk.payment_link.create.return_value = {"id": "plink_1"}
        client = make_client(sdk)

        client.create_payment_link(
            amount_paise=10000, currency="INR", description="d", notes={}, idempotency_key="k1"
        )

        (data,), _ = sdk.payment_link.create.call_args
        assert "customer" not in data
        assert data["notify"] == {"sms": False, "email": False}


class TestConfigFromEnv:
    def test_missing_keys_raise(self, monkeypatch):
        monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
        monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
        with pytest.raises(RuntimeError):
            RazorpayConfig.from_env()

    def test_present_keys_are_read(self, monkeypatch):
        monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
        monkeypatch.setenv("RAZORPAY_KEY_SECRET", "shh")
        config = RazorpayConfig.from_env()
        assert config.key_id == "rzp_test_abc"
        assert config.key_secret == "shh"
