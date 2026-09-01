"""executor.py: one method per (intervention, role) pair that can actually
reach an executor. HUMAN_QUEUE never does — vasool/policy/machine.py::_gate
escalates it before execute() is ever called — so receiving one here is the
programming error the session brief names, not a silent no-op.

No real Razorpay calls: FakeRazorpayClient stands in for
vasool.actions.razorpay_client.RazorpayClient at its own public interface,
which is the boundary the project rules names for mocking.
"""
from __future__ import annotations

import pytest

from vasool.actions.comms import CommsSender
from vasool.actions.executor import RazorpayExecutor, UnroutableProposal
from vasool.actions.razorpay_client import RazorpayCallFailed
from vasool.diagnosis.proposal import ProposalRole, template_ids
from vasool.diagnosis.taxonomy import InterventionType
from tests.policy.strategies import proposal_for, proposals_for


class FakeRazorpayClient:
    """Stands in for RazorpayClient's public interface — no SDK, no network."""

    def __init__(self):
        self.payment_links: list[dict] = []
        self.notifications: list[dict] = []
        self.retries: list[dict] = []
        self.fail_next: RazorpayCallFailed | None = None

    def _maybe_fail(self):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def create_payment_link(self, **kwargs):
        self._maybe_fail()
        self.payment_links.append(kwargs)
        return {"id": f"plink_{len(self.payment_links)}", "short_url": "https://rzp.io/l/x"}

    def notify_payment_link(self, **kwargs):
        self._maybe_fail()
        self.notifications.append(kwargs)
        return {"success": True}

    def retry_payment(self, **kwargs):
        self._maybe_fail()
        self.retries.append(kwargs)
        return {"id": f"pay_retry_{len(self.retries)}", "status": "created"}


def make_executor(*, registered_templates=None):
    client = FakeRazorpayClient()
    calls: list[tuple] = []

    def deliver(proposal, params):
        calls.append((proposal, params))
        return {"delivered": True}

    templates = registered_templates if registered_templates is not None else template_ids()
    executor = RazorpayExecutor(
        client=client, comms=CommsSender(deliver=deliver), registered_templates=templates
    )
    return executor, client, calls


class TestRouting:
    def test_human_queue_is_unroutable(self):
        executor, _, _ = make_executor()
        proposal = proposal_for("payment_risk_check_failed")
        assert proposal.intervention is InterventionType.HUMAN_QUEUE

        with pytest.raises(UnroutableProposal):
            executor.execute(proposal)

    def test_silent_retry_calls_retry_payment_and_nothing_else(self):
        executor, client, calls = make_executor()
        proposal = proposal_for("gateway_technical_error")

        result = executor.execute(proposal)

        assert result.ok
        assert len(client.retries) == 1
        assert client.payment_links == []
        assert calls == []

    def test_reauth_link_creates_a_link_and_sends_it(self):
        executor, client, calls = make_executor()
        proposal = proposal_for("card_expired")

        result = executor.execute(proposal)

        assert result.ok
        assert len(client.payment_links) == 1
        assert len(calls) == 1
        assert client.retries == []

    def test_a_link_tags_its_entity_id_in_notes(self):
        """vasool/events/settlement.py reads vasool_entity_id back off a
        payment_link.paid webhook to close the episode this link belongs to
        (docs/VERIFIED.md) — it has to be on every link this executor makes,
        not just the ones a particular test happens to check."""
        executor, client, _ = make_executor()
        proposal = proposal_for("card_expired")

        executor.execute(proposal)

        assert client.payment_links[0]["notes"]["vasool_entity_id"] == proposal.entity_id

    def test_a_nudge_only_sends_no_razorpay_call(self):
        executor, client, calls = make_executor()
        nudge = next(p for p in proposals_for("insufficient_fund") if p.role is ProposalRole.NUDGE)

        result = executor.execute(nudge)

        assert result.ok
        assert client.payment_links == []
        assert client.retries == []
        assert len(calls) == 1


class TestJournal:
    def test_every_execute_call_is_recorded_in_the_journal(self):
        executor, _, _ = make_executor()
        proposal = proposal_for("gateway_technical_error")

        executor.execute(proposal)

        record = executor.journal.get(proposal.proposal_id)
        assert record is not None
        assert record.ok is True
        assert record.razorpay_request_id is not None

    def test_an_unrecorded_proposal_id_returns_none(self):
        executor, _, _ = make_executor()
        assert executor.journal.get("prop_never_seen") is None


class TestRetryIndex:
    """item 2: the non-guessed join key a later payment.captured correlates
    against, populated at the one point that has both halves of it."""

    def test_a_successful_retry_records_its_returned_id_against_the_entity_id(self):
        executor, client, _ = make_executor()
        proposal = proposal_for("gateway_technical_error")

        executor.execute(proposal)

        record = executor.journal.get(proposal.proposal_id)
        assert record is not None and record.razorpay_request_id is not None
        assert executor.retry_index.entity_id_for(record.razorpay_request_id) == proposal.entity_id

    def test_an_unknown_payment_id_correlates_to_nothing(self):
        executor, _, _ = make_executor()
        assert executor.retry_index.entity_id_for("pay_never_seen") is None

    def test_a_failed_retry_records_nothing_in_the_retry_index(self):
        executor, client, _ = make_executor()
        client.fail_next = RazorpayCallFailed("boom", retryable=False, cause=Exception("x"))
        proposal = proposal_for("gateway_technical_error")

        executor.execute(proposal)

        assert executor.retry_index.entity_id_for("pay_retry_1") is None

    def test_a_link_never_touches_the_retry_index(self):
        """REAUTH_LINK/REATTEMPT_LINK go through _link, not _retry -- nothing
        about creating a Payment Link should populate a table meant for
        payment ids retry_payment returned."""
        executor, client, _ = make_executor()
        proposal = proposal_for("card_expired")

        executor.execute(proposal)

        record = executor.journal.get(proposal.proposal_id)
        assert record is not None and record.razorpay_request_id == "plink_1"
        assert executor.retry_index.entity_id_for("plink_1") is None


class TestFailureHandling:
    def test_a_razorpay_failure_on_retry_is_recorded_not_raised(self):
        executor, client, _ = make_executor()
        client.fail_next = RazorpayCallFailed("boom", retryable=False, cause=Exception("x"))
        proposal = proposal_for("gateway_technical_error")

        result = executor.execute(proposal)

        assert not result.ok
        record = executor.journal.get(proposal.proposal_id)
        assert record is not None and not record.ok
        assert record.razorpay_request_id is None

    def test_comms_refusal_on_a_link_still_records_the_created_link(self):
        """The payment link genuinely exists on Razorpay's side even though
        the customer was never told — worth keeping in the receipt."""
        executor, client, _ = make_executor(registered_templates=frozenset())
        proposal = proposal_for("card_expired")

        result = executor.execute(proposal)

        assert not result.ok
        assert len(client.payment_links) == 1
        record = executor.journal.get(proposal.proposal_id)
        assert record is not None
        assert record.razorpay_request_id == "plink_1"
        assert record.razorpay_response is not None
