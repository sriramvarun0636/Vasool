"""windtunnel/payloads.py: every simulated event comes off disk.

the project rules: a reason in neither data/observed_payloads/ nor
data/stubbed_payloads/ does not exist — do not guess, do not infer from
documentation, do not "helpfully" add plausible ones. The simulator generates
several hundred thousand events, so the discipline has to be structural: this
module stamps identity onto a real envelope and never assembles one.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import pytest

from vasool.diagnosis.taxonomy import known_reasons
from windtunnel.parameters import PAYMENT_FAILED_SOURCE_MIX, REASON_MIX
from windtunnel.payloads import (
    NoSuchPayload,
    available_pairs,
    capture_body,
    failure_event,
    link_paid_body,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
PEPPER = "test-pepper-do-not-use-in-prod"
WHEN = datetime(2026, 9, 15, 10, 30, tzinfo=timezone.utc)


def _event(reason: str, source: str, **overrides):
    kwargs = dict(
        reason=reason,
        source=source,
        entity_id="pay_simulated0001",
        contact="+919876543210",
        email="someone@example.com",
        amount_paise=120_000,
        occurred_at=WHEN,
        pepper=PEPPER,
    )
    return failure_event(**{**kwargs, **overrides})


class TestProvenance:
    def test_every_reason_the_registered_mix_uses_has_a_payload(self):
        """§3d names ten reasons. If one of them had no envelope on disk the
        simulator could not generate it without inventing one, and this is
        where that has to fail — not silently at run time."""
        for reason, _share in REASON_MIX:
            if reason == "payment_failed":
                continue
            assert any(r == reason for r, _ in available_pairs()), reason

    def test_every_registered_source_for_the_generic_reason_is_reachable(self):
        for source, _share in PAYMENT_FAILED_SOURCE_MIX:
            assert _event("payment_failed", source).error_source == source

    def test_the_mix_never_names_a_reason_the_taxonomy_cannot_classify(self):
        assert {reason for reason, _ in REASON_MIX} <= known_reasons()

    def test_an_unknown_reason_cannot_be_generated(self):
        with pytest.raises(NoSuchPayload):
            _event("card_on_fire", "gateway")

    def test_a_source_string_that_exists_nowhere_on_disk_cannot_be_generated(self):
        """"network" is a source Razorpay documents and this account has
        never seen. tests/payloads.py uses it precisely because it is on no
        payload — so it must not be reachable from here either."""
        with pytest.raises(NoSuchPayload):
            _event("payment_failed", "network")

    def test_the_error_fields_are_never_altered_by_stamping_identity(self):
        """The four error fields are the ones the project rules protects. Identity —
        who, how much, when — is ours to set; the failure itself is not."""
        on_disk = json.loads(
            (
                REPO_ROOT / "data" / "stubbed_payloads" / "SIMULATED__payment_failed__card_expired.json"
            ).read_text()
        )["body"]["payload"]["payment"]["entity"]

        event = _event("card_expired", on_disk["error_source"])

        assert event.error_reason == on_disk["error_reason"]
        assert event.error_source == on_disk["error_source"]
        assert event.error_code == on_disk["error_code"]
        assert event.error_step == on_disk["error_step"]


class TestTheBusinessSourcedGenericFailure:
    """§3d's 70/25/5 split needs payment_failed/business, and no single file
    on disk carries that pair — the only `business` ever written down sits on
    the risk-check stub, and every payment_failed envelope is gateway or bank.

    Both strings exist in data/; only their combination does not. So it is
    assembled by reading each off disk, the same move
    tests/payloads.py::event_for(reason, source) already makes. Nothing is
    typed, and nothing is written to either payload directory.
    """

    def test_no_single_payload_on_disk_carries_the_pair(self):
        assert ("payment_failed", "business") not in available_pairs()

    def test_the_pair_is_still_generable(self):
        event = _event("payment_failed", "business")
        assert (event.error_reason, event.error_source) == ("payment_failed", "business")

    def test_the_source_string_is_read_off_a_real_payload(self):
        stub = json.loads(
            (
                REPO_ROOT
                / "data"
                / "stubbed_payloads"
                / "SIMULATED__payment_failed__payment_risk_check_failed.json"
            ).read_text()
        )
        assert stub["body"]["payload"]["payment"]["entity"]["error_source"] == "business"

    def test_it_classifies_as_the_risk_block_row_the_split_intends(self):
        from vasool.diagnosis.taxonomy import FailureClass, lookup

        _reason, rule = lookup("payment_failed", "business")
        assert rule.failure_class is FailureClass.RISK_BLOCK

    def test_the_rest_of_the_envelope_is_the_real_generic_failure(self):
        gateway = _event("payment_failed", "gateway")
        business = _event("payment_failed", "business")
        assert business.error_code == gateway.error_code
        assert business.error_step == gateway.error_step


class TestIdentityStamping:
    def test_the_entity_id_is_the_one_asked_for(self):
        assert _event("card_expired", "bank", entity_id="pay_xyz").entity_id == "pay_xyz"

    def test_the_amount_is_the_one_asked_for(self):
        assert _event("card_expired", "bank", amount_paise=999_900).amount_paise == 999_900

    def test_the_occurrence_time_is_the_one_asked_for(self):
        when = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
        assert _event("card_expired", "bank", occurred_at=when).occurred_at == when

    def test_two_customers_get_different_pseudonymous_ids(self):
        a = _event("card_expired", "bank", contact="+919876543210")
        b = _event("card_expired", "bank", contact="+919812345678")
        assert a.customer_id != b.customer_id

    def test_the_same_customer_gets_the_same_id_across_episodes(self):
        a = _event("card_expired", "bank", entity_id="pay_1", contact="+919876543210")
        b = _event("insufficient_fund", "bank", entity_id="pay_2", contact="+919876543210")
        assert a.customer_id == b.customer_id

    def test_the_customer_id_is_peppered_not_a_bare_hash(self):
        a = _event("card_expired", "bank", pepper="pepper-one")
        b = _event("card_expired", "bank", pepper="pepper-two")
        assert a.customer_id != b.customer_id

    def test_distinct_entities_get_distinct_event_ids(self):
        """Razorpay's own dedupe key. Two episodes sharing one would make the
        second invisible to EventStore — and every payload on disk shares a
        single captured event id, so this cannot be left to the envelope."""
        ids = {_event("card_expired", "bank", entity_id=f"pay_{i}").event_id for i in range(50)}
        assert len(ids) == 50

    def test_generation_is_deterministic(self):
        assert _event("card_expired", "bank") == _event("card_expired", "bank")


class TestSettlementEnvelopes:
    def test_a_link_body_carries_the_notes_tag_the_executor_stamps(self):
        body = link_paid_body(entity_id="pay_abc", amount_paise=120_000)
        notes = body["payload"]["payment_link"]["entity"]["notes"]
        assert notes["vasool_entity_id"] == "pay_abc"

    def test_a_link_body_is_readable_by_the_real_correlation_function(self):
        from vasool.events.settlement import (
            amount_paise_from_payment_link_paid,
            entity_id_from_payment_link_paid,
        )

        body = link_paid_body(entity_id="pay_abc", amount_paise=120_000)

        assert entity_id_from_payment_link_paid(body) == "pay_abc"
        assert amount_paise_from_payment_link_paid(body) == 120_000

    def test_a_capture_body_carries_the_payment_id_it_was_given(self):
        body = capture_body(payment_id="pay_retry_7", amount_paise=5_000)
        assert body["payload"]["payment"]["entity"]["id"] == "pay_retry_7"

    def test_a_capture_body_is_readable_by_the_real_correlation_function(self):
        from vasool.events.settlement import amount_paise_from_payment_captured

        body = capture_body(payment_id="pay_retry_7", amount_paise=5_000)
        assert amount_paise_from_payment_captured(body) == 5_000

    def test_the_settlement_envelopes_are_the_real_captures(self):
        """Both are stamped copies of the only payment_link.paid and
        payment.captured this account has ever captured live — the same two
        envelopes vasool/demo.py replays, and for the same reason."""
        assert link_paid_body(entity_id="x", amount_paise=1)["event"] == "payment_link.paid"
        assert capture_body(payment_id="x", amount_paise=1)["event"] == "payment.captured"

    def test_stamping_never_mutates_the_file_backed_template(self):
        first = link_paid_body(entity_id="pay_one", amount_paise=1)
        second = link_paid_body(entity_id="pay_two", amount_paise=2)
        assert first["payload"]["payment_link"]["entity"]["notes"]["vasool_entity_id"] == "pay_one"
        assert second["payload"]["payment_link"]["entity"]["notes"]["vasool_entity_id"] == "pay_two"
