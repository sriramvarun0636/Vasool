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
    settle_from_webhook,
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


class FakeMachine:
    """Stands in for PolicyMachine at settlement.py's SettlementTarget seam."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def settled(self, entity_id: str, *, reason: str, amount_paise: int) -> None:
        self.calls.append((entity_id, reason, amount_paise))


class TestSettleFromWebhook:
    """The dispatch itself — which event name is wired to which correlation.

    Extracted out of vasool/events/receiver.py's route so that windtunnel/
    drives the *same* dispatch production does rather than a second copy of
    it. A copy would drift, and the first time it drifted the evaluation
    would be measuring behaviour the receiver does not have. The route still
    owns everything around this call — signature, dedupe, HTTP — and
    tests/test_receiver.py still exercises all of it end to end.
    """

    def test_a_link_carrying_our_notes_tag_settles_the_episode(self):
        machine = FakeMachine()
        body = _body()
        body["payload"]["payment_link"]["entity"]["notes"] = {"vasool_entity_id": "pay_x"}

        settled = settle_from_webhook(event_name="payment_link.paid", body=body, machine=machine)

        assert settled == "pay_x"
        assert machine.calls == [("pay_x", "payment_link.paid", 50000)]

    def test_a_link_with_no_notes_tag_settles_nothing(self):
        machine = FakeMachine()

        settled = settle_from_webhook(
            event_name="payment_link.paid", body=_body(), machine=machine
        )

        assert settled is None
        assert machine.calls == []

    def test_a_capture_matching_our_retry_index_settles_the_episode(self):
        machine = FakeMachine()
        body = _captured_body()
        payment_id = body["payload"]["payment"]["entity"]["id"]

        settled = settle_from_webhook(
            event_name="payment.captured",
            body=body,
            machine=machine,
            retry_index=FakeRetryIndex(**{payment_id: "pay_x"}),
        )

        assert settled == "pay_x"
        assert machine.calls == [("pay_x", "payment.captured", 50000)]

    def test_a_capture_with_no_retry_index_wired_settles_nothing(self):
        machine = FakeMachine()

        settled = settle_from_webhook(
            event_name="payment.captured", body=_captured_body(), machine=machine
        )

        assert settled is None
        assert machine.calls == []

    def test_a_capture_this_agent_never_dispatched_settles_nothing(self):
        """The out-of-band case windtunnel models, and the ordinary-checkout
        case production sees constantly: a real capture whose payment id is
        in nobody's RetryIndex correlates to nothing (docs/taxonomy.md §9.9).
        """
        machine = FakeMachine()

        settled = settle_from_webhook(
            event_name="payment.captured",
            body=_captured_body(),
            machine=machine,
            retry_index=FakeRetryIndex(pay_someone_elses_retry="pay_unrelated"),
        )

        assert settled is None
        assert machine.calls == []

    def test_order_paid_settles_nothing_ever(self):
        """docs/VERIFIED.md: order.paid carries no attributable field at all
        and is deliberately never wired, however truthful a signal it is."""
        machine = FakeMachine()
        body = json.loads((OBSERVED / "order_paid__none__b90866.json").read_text())["body"]

        settled = settle_from_webhook(event_name="order.paid", body=body, machine=machine)

        assert settled is None
        assert machine.calls == []

    def test_a_payment_failed_settles_nothing(self):
        machine = FakeMachine()
        body = json.loads(
            (OBSERVED / "payment_failed__payment_failed__firstcapture.json").read_text()
        )["body"]

        settled = settle_from_webhook(event_name="payment.failed", body=body, machine=machine)

        assert settled is None
        assert machine.calls == []
