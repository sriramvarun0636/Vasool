"""The inert object the diagnosis plane emits, and the fan-out that keeps a
soft nudge from riding through the guards on a retry's coat-tails.

The fan-out is the substance here. docs/taxonomy.md §4 gives LIQUIDITY
"TIMED_RETRY ×3 + soft nudge" — one Diagnosis describing two actions that reach
the world in completely different ways. A retry re-presents an instrument and
touches no one; a nudge is a message to a human at a particular hour of the day.
Carried on one Proposal, the nudge inherits the retry's verdict and never meets
ContactWindowGuard, DNDGuard, DLTTemplateGuard or FrequencyCapGuard at all.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import (
    Channel,
    MessageCategory,
    Proposal,
    ProposalRole,
    proposals_from,
)
from vasool.diagnosis.rules import classify
from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from tests.payloads import NEVER_OBSERVED_SOURCE, event_for, one_event_per_pair

I = InterventionType

# Tue 25 Aug 2026, 10:00 IST — the same pinned instant tests/test_rules.py uses.
NOW = datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc)


def diagnose(reason: str, source: str | None = None, attempt: int = 1):
    return classify(event_for(reason, source), clock=VirtualClock(NOW), attempt=attempt)


def propose(reason: str, source: str | None = None, attempt: int = 1) -> tuple[Proposal, ...]:
    event = event_for(reason, source)
    return proposals_from(classify(event, clock=VirtualClock(NOW), attempt=attempt), event, now=NOW)


# ---------------------------------------------------------------------------
# the fan-out
# ---------------------------------------------------------------------------
class TestFanOut:
    def test_a_plain_retry_produces_exactly_one_proposal(self):
        (p,) = propose("gateway_technical_error")
        assert p.role is ProposalRole.PRIMARY
        assert p.intervention is I.SILENT_RETRY

    def test_liquidity_attempt_one_produces_a_retry_and_a_nudge(self):
        """§4: TIMED_RETRY + soft nudge. Two actions, so two proposals."""
        retry, nudge = propose("insufficient_fund")
        assert (retry.role, retry.intervention) == (ProposalRole.PRIMARY, I.TIMED_RETRY)
        assert nudge.role is ProposalRole.NUDGE

    def test_the_nudge_is_a_contact_and_the_retry_is_not(self):
        """The distinction the whole fan-out exists to preserve. Everything the
        comms guards do keys on this."""
        retry, nudge = propose("insufficient_fund")
        assert not retry.is_contact
        assert nudge.is_contact
        assert nudge.channel is not None

    def test_only_the_retry_counts_against_the_attempt_budget(self):
        """The nudge shares the retry's intervention — §4 models it as a
        modifier on TIMED_RETRY, not as an intervention of its own — so
        is_retry must key on the role, or a nudge would burn one of the four
        attempts Razorpay allows before halting a subscription."""
        retry, nudge = propose("insufficient_fund")
        assert retry.is_retry
        assert nudge.intervention is I.TIMED_RETRY
        assert not nudge.is_retry

    def test_the_nudge_goes_out_now_and_the_retry_waits_for_payday(self):
        """A nudge that arrives with the retry is useless — the point is to give
        the customer time to move money before we re-present."""
        retry, nudge = propose("insufficient_fund")
        assert nudge.execute_at == NOW
        assert retry.execute_at > NOW + timedelta(days=1)

    def test_siblings_point_at_each_other(self):
        retry, nudge = propose("insufficient_fund")
        assert retry.sibling_id == nudge.proposal_id
        assert nudge.sibling_id == retry.proposal_id

    def test_siblings_have_distinct_idempotency_keys(self):
        """They share entity, intervention and attempt. Without the role in the
        key, executing the retry would mark the nudge as already done."""
        retry, nudge = propose("insufficient_fund")
        assert retry.idempotency_key != nudge.idempotency_key

    def test_the_nudge_is_capped_at_attempt_one(self):
        """§2 allows LIQUIDITY exactly one soft nudge; rules.py caps it. This
        asserts the fan-out doesn't quietly re-add it on later attempts."""
        assert len(propose("insufficient_fund", attempt=2)) == 1
        assert len(propose("insufficient_fund", attempt=3)) == 1

    def test_an_exhausted_diagnosis_proposes_nothing(self):
        """intervention is None is the EXHAUSTED terminal — no action at all,
        which must not become a proposal that guards then rule on."""
        d = replace(diagnose("card_expired"), intervention=None, execute_at=None)
        assert proposals_from(d, event_for("card_expired"), now=NOW) == ()

    @pytest.mark.parametrize("event", one_event_per_pair(), ids=lambda e: e.error_reason)
    def test_every_row_on_disk_proposes_one_or_two_actions(self, event):
        d = classify(event, clock=VirtualClock(NOW))
        assert 1 <= len(proposals_from(d, event, now=NOW)) <= 2


# ---------------------------------------------------------------------------
# what the guards read
# ---------------------------------------------------------------------------
class TestContactShape:
    @pytest.mark.parametrize("reason", ["card_expired", "card_number_invalid"])
    def test_a_link_carries_a_channel_a_category_and_a_template(self, reason):
        (p,) = propose(reason)
        assert p.channel is Channel.SMS
        assert p.message_category is MessageCategory.TRANSACTIONAL
        assert p.template_id is not None

    def test_a_silent_retry_carries_none_of_them(self):
        (p,) = propose("gateway_technical_error")
        assert (p.channel, p.message_category, p.template_id) == (None, None, None)

    def test_a_human_queue_handoff_is_not_a_customer_contact(self):
        """Nothing is sent to anyone — it is an internal handoff, and treating
        it as a contact would subject an operator queue entry to the RBI contact
        window."""
        (p,) = propose("payment_risk_check_failed")
        assert p.intervention is I.HUMAN_QUEUE
        assert not p.is_contact

    def test_the_explain_flag_survives_into_the_proposal(self):
        """card_disabled_for_online_payments: the wording is the intervention,
        so the flag has to reach the comms layer intact."""
        (p,) = propose("card_disabled_for_online_payments")
        assert p.explain
        assert p.template_id != propose("card_expired")[0].template_id

    def test_a_contact_intervention_is_a_contact_even_with_no_channel_set(self):
        """Fail closed. A REAUTH_LINK built by hand with channel=None must not
        slip past the comms guards by looking like a silent retry."""
        p = propose("card_expired")[0].model_copy(update={"channel": None})
        assert p.is_contact


# ---------------------------------------------------------------------------
# deterministic identity — replay, and A02
# ---------------------------------------------------------------------------
class TestIdentity:
    def test_the_same_inputs_yield_the_same_id(self):
        """No uuid4 anywhere: same seed -> byte-identical ledger (the project rules
        invariant 5) means proposal ids have to be derived, not generated."""
        assert propose("card_expired")[0].proposal_id == propose("card_expired")[0].proposal_id

    def test_the_id_is_stable_across_processes(self):
        """Pinned literally, because Python's hash() is salted per process and
        would pass an equality test inside one run while breaking replay."""
        assert propose("card_expired")[0].proposal_id == "prop_75d309ca0abe07aa"

    def test_a_replayed_webhook_with_a_new_event_id_is_the_same_proposal(self):
        """A02. The spec keys idempotency on (event_id, intervention), which
        this defeats: same payment, new event id, and the guard sees an action
        it has never executed. Keying on the entity closes it."""
        original = event_for("card_expired")
        replayed = original.model_copy(update={"event_id": "evt_a_completely_different_id"})
        (a,) = proposals_from(classify(original, clock=VirtualClock(NOW)), original, now=NOW)
        (b,) = proposals_from(classify(replayed, clock=VirtualClock(NOW)), replayed, now=NOW)
        assert a.idempotency_key == b.idempotency_key
        assert a.proposal_id == b.proposal_id

    def test_different_attempts_are_different_actions(self):
        """The other half of the spec's idempotency bug: gateway_technical_error
        gets three silent retries for one event, and keying on (event_id,
        intervention) would block the second as a duplicate of the first."""
        keys = {propose("gateway_technical_error", attempt=n)[0].idempotency_key for n in (1, 2, 3)}
        assert len(keys) == 3

    def test_a_different_payment_is_a_different_action(self):
        other = event_for("card_expired").model_copy(update={"entity_id": "pay_somethingelse"})
        (p,) = proposals_from(classify(other, clock=VirtualClock(NOW)), other, now=NOW)
        assert p.idempotency_key != propose("card_expired")[0].idempotency_key


class TestProvenance:
    def test_the_rules_classifier_signs_its_work(self):
        (p,) = propose("card_expired")
        assert (p.proposed_by, p.confidence) == ("rules", 1.0)

    def test_the_rationale_survives_into_the_proposal(self):
        """It ends up in the receipt. An audit trail has to say why, not just
        what — and for card_expired the why is the whole argument."""
        (p,) = propose("card_expired")
        assert "zero" in p.rationale.lower()

    def test_the_failure_class_survives(self):
        (p,) = propose("payment_risk_check_failed")
        assert p.failure_class is FailureClass.RISK_BLOCK

    def test_the_unmapped_row_still_proposes(self):
        """The fail-safe path has to produce a gateable proposal like any
        other, or an unknown reason would silently do nothing at all."""
        (p,) = propose("payment_failed", NEVER_OBSERVED_SOURCE)
        assert p.intervention is I.SILENT_RETRY


class TestImmutability:
    def test_a_proposal_cannot_be_edited(self):
        """The hash chain in stage 5 depends on this, and so does the deferral
        design: re-entry creates a successor rather than mutating the original."""
        (p,) = propose("card_expired")
        with pytest.raises(Exception):
            p.execute_at = NOW
