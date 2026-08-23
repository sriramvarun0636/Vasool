"""vasool/events/settlement.py: correlating a payment_link.paid webhook back
to the entity_id whose recovery episode it closes.

Uses the real captured payload plus copies of it with notes altered — never a
hand-typed envelope shape, per CLAUDE.md. Session 0A's own capture predates
the vasool_entity_id tag (docs/VERIFIED.md), so it is the "None" case here by
construction, not a contrived one.
"""
from __future__ import annotations

import json
import pathlib

from vasool.events.settlement import (
    amount_paise_from_payment_captured,
    amount_paise_from_payment_link_paid,
    entity_id_from_payment_captured,
    entity_id_from_payment_link_paid,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED = REPO_ROOT / "data" / "observed_payloads"
FIXTURE = OBSERVED / "payment_link_paid__none__12b6f2.json"
CAPTURED_FIXTURE = OBSERVED / "payment_captured__none__0ced11.json"


def _body() -> dict:
    return json.loads(FIXTURE.read_text())["body"]


def _captured_body() -> dict:
    return json.loads(CAPTURED_FIXTURE.read_text())["body"]


class FakeRetryIndex:
    """Stands in for vasool/actions/executor.py::RetryIndex at its own
    public interface — no executor, no real retry ever dispatched."""

    def __init__(self, **known: str) -> None:
        self._known = known

    def entity_id_for(self, payment_id: str) -> str | None:
        return self._known.get(payment_id)


class TestEntityIdCorrelation:
    def test_a_capture_that_predates_the_notes_tag_correlates_to_nothing(self):
        """The only payment_link.paid ever captured live has notes: null —
        it was created by hand before executor.py tagged anything. This must
        return None, not raise, and not guess."""
        assert entity_id_from_payment_link_paid(_body()) is None

    def test_our_own_notes_tag_correlates_to_the_entity_it_names(self):
        """notes is merchant-supplied metadata, not a Razorpay-authored
        field, so setting it on a copy of the real envelope isn't inventing
        anything Razorpay says — it's exercising the one field that's ours."""
        body = _body()
        body["payload"]["payment_link"]["entity"]["notes"] = {
            "vasool_proposal_id": "prop_deadbeef",
            "vasool_entity_id": "pay_TSLgGeBCNb460J",
        }
        assert entity_id_from_payment_link_paid(body) == "pay_TSLgGeBCNb460J"

    def test_a_third_party_payment_link_with_unrelated_notes_correlates_to_nothing(self):
        body = _body()
        body["payload"]["payment_link"]["entity"]["notes"] = {"merchant_order_ref": "INV-004"}
        assert entity_id_from_payment_link_paid(body) is None

    def test_a_non_string_notes_value_correlates_to_nothing(self):
        """Defensive against a malformed or tampered notes value rather than
        crashing the receiver on an unexpected shape."""
        body = _body()
        body["payload"]["payment_link"]["entity"]["notes"] = {"vasool_entity_id": 12345}
        assert entity_id_from_payment_link_paid(body) is None

    def test_a_missing_payment_link_key_correlates_to_nothing(self):
        body = {"payload": {}}
        assert entity_id_from_payment_link_paid(body) is None


class TestSettledAmount:
    def test_reads_the_real_payment_amount_off_the_same_webhook(self):
        assert amount_paise_from_payment_link_paid(_body()) == 50000

    def test_never_assumes_the_original_failed_amount(self):
        """The amount that settled is read fresh from this webhook's own
        payment entity, independent of whatever the original failure said."""
        body = _body()
        body["payload"]["payment"]["entity"]["amount"] = 12345
        assert amount_paise_from_payment_link_paid(body) == 12345


# ---------------------------------------------------------------------------
# item 2: SILENT_RETRY/TIMED_RETRY correlation via RetryIndex
# ---------------------------------------------------------------------------
class TestRetryCorrelation:
    def test_a_payment_id_our_retry_index_knows_correlates_to_its_entity_id(self):
        """The one real payment.captured capture on this account, with its
        payment id swapped for one RetryIndex was actually told about — never
        a hand-typed envelope shape."""
        body = _captured_body()
        body["payload"]["payment"]["entity"]["id"] = "pay_retry_result_1"
        retry_index = FakeRetryIndex(pay_retry_result_1="pay_original_fail")

        assert entity_id_from_payment_captured(body, retry_index=retry_index) == "pay_original_fail"

    def test_a_payment_id_retry_index_does_not_know_correlates_to_nothing(self):
        """The ordinary case: a customer's first-ever checkout, or a Payment
        Links payment, or a retry we never dispatched. Must return None, not
        guess and not raise."""
        retry_index = FakeRetryIndex(pay_something_else="pay_original_fail")

        assert entity_id_from_payment_captured(_captured_body(), retry_index=retry_index) is None

    def test_an_empty_retry_index_correlates_to_nothing(self):
        assert entity_id_from_payment_captured(_captured_body(), retry_index=FakeRetryIndex()) is None

    def test_a_missing_payment_key_correlates_to_nothing(self):
        body = {"payload": {}}
        assert entity_id_from_payment_captured(body, retry_index=FakeRetryIndex()) is None

    def test_a_non_string_payment_id_correlates_to_nothing(self):
        body = _captured_body()
        body["payload"]["payment"]["entity"]["id"] = 12345
        assert entity_id_from_payment_captured(body, retry_index=FakeRetryIndex()) is None


class TestCapturedAmount:
    def test_reads_the_real_captured_amount_off_the_same_webhook(self):
        assert amount_paise_from_payment_captured(_captured_body()) == 50000

    def test_never_assumes_the_original_failed_amount(self):
        body = _captured_body()
        body["payload"]["payment"]["entity"]["amount"] = 12345
        assert amount_paise_from_payment_captured(body) == 12345
