"""FailureEvent must be derivable entirely from real captured payloads.

Fixtures come from data/observed_payloads/ (live) and data/stubbed_payloads/
(hand-built, _SIMULATED: true) per CLAUDE.md — never invented here. See
docs/VERIFIED.md for why only error_reason "payment_failed" is reproducible
live and everything else needs a stub.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from vasool.events.schemas import FailureEvent, derive_customer_id, from_webhook

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED = REPO_ROOT / "data" / "observed_payloads"
STUBBED = REPO_ROOT / "data" / "stubbed_payloads"
TEST_PEPPER = "test-pepper-do-not-use-in-prod"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def _payment_failed_fixtures() -> list[pathlib.Path]:
    return sorted(OBSERVED.glob("payment_failed__*.json")) + sorted(
        STUBBED.glob("SIMULATED__payment_failed__*.json")
    )


@pytest.mark.parametrize("path", _payment_failed_fixtures(), ids=lambda p: p.name)
def test_from_webhook_matches_source_payload(path: pathlib.Path):
    fixture = _load(path)
    event_id = fixture["headers"]["x-razorpay-event-id"]
    body = fixture["body"]
    payment = body["payload"]["payment"]["entity"]

    event = from_webhook(event_id=event_id, body=body, pepper=TEST_PEPPER)

    assert event.event_id == event_id
    assert event.entity_id == payment["id"]
    assert event.merchant_id == body["account_id"]
    assert event.amount_paise == payment["amount"]
    assert event.currency == payment["currency"]
    assert event.method == payment["method"]
    assert event.error_code == payment["error_code"]
    assert event.error_source == payment["error_source"]
    assert event.error_step == payment["error_step"]
    assert event.error_reason == payment["error_reason"]
    assert event.customer_id == derive_customer_id(
        payment.get("contact"), payment.get("email"), pepper=TEST_PEPPER
    )
    assert event.occurred_at.timestamp() == body["created_at"]


def test_event_id_comes_from_header_not_body():
    """Confirms VERIFIED.md: x-razorpay-event-id lives in the header. There is
    no event id field anywhere in the body itself."""
    fixture = _load(_payment_failed_fixtures()[0])
    body_str = json.dumps(fixture["body"])
    header_event_id = fixture["headers"]["x-razorpay-event-id"]
    assert header_event_id not in body_str


def test_only_payment_failed_is_reproducible_live():
    """VERIFIED.md: every live capture has error_reason == payment_failed.
    Any other reason in observed_payloads/ would mean this finding is stale."""
    for path in sorted(OBSERVED.glob("payment_failed__*.json")):
        fixture = _load(path)
        reason = fixture["body"]["payload"]["payment"]["entity"]["error_reason"]
        assert reason == "payment_failed", f"{path.name} broke the VERIFIED.md finding"


def test_stub_fixtures_are_marked_simulated():
    for path in sorted(STUBBED.glob("SIMULATED__payment_failed__*.json")):
        assert _load(path)["_SIMULATED"] is True


class TestDeriveCustomerId:
    def test_deterministic(self):
        a = derive_customer_id("+919392284464", "void@razorpay.com", pepper=TEST_PEPPER)
        b = derive_customer_id("+919392284464", "void@razorpay.com", pepper=TEST_PEPPER)
        assert a == b

    def test_distinct_inputs_diverge(self):
        a = derive_customer_id("+919392284464", "void@razorpay.com", pepper=TEST_PEPPER)
        b = derive_customer_id("+919999999999", "void@razorpay.com", pepper=TEST_PEPPER)
        assert a != b

    def test_does_not_leak_raw_contact(self):
        contact = "+919392284464"
        customer_id = derive_customer_id(contact, "void@razorpay.com", pepper=TEST_PEPPER)
        assert contact not in customer_id

    def test_same_human_two_emails_diverges(self):
        """KNOWN LIMITATION (see module docstring in schemas.py): a customer_id
        keyed on contact+email means the same human with two emails gets two
        customer_ids, which is exactly how A13 (duplicate identity / frequency
        cap bypass) gets in. Documented, not fixed, here."""
        a = derive_customer_id("+919392284464", "one@example.com", pepper=TEST_PEPPER)
        b = derive_customer_id("+919392284464", "two@example.com", pepper=TEST_PEPPER)
        assert a != b

    def test_pepper_changes_output(self):
        """The whole point of keying the hash: without the pepper the id
        can't be reproduced, even from the exact same contact+email."""
        a = derive_customer_id("+919392284464", "void@razorpay.com", pepper=TEST_PEPPER)
        b = derive_customer_id("+919392284464", "void@razorpay.com", pepper="a-different-pepper")
        assert a != b


def test_failure_event_is_frozen():
    fixture = _load(_payment_failed_fixtures()[0])
    event = from_webhook(
        event_id=fixture["headers"]["x-razorpay-event-id"], body=fixture["body"], pepper=TEST_PEPPER
    )
    with pytest.raises(ValidationError):
        event.amount_paise = 1
