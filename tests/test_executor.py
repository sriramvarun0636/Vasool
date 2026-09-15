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
from vasool.actions.debit import DebitAttempt, NullMandateDebiter
from vasool.actions.executor import RazorpayExecutor, UnroutableProposal
from vasool.actions.notice import NoticeRequest, NullPreDebitNotifier
from vasool.actions.razorpay_client import RazorpayCallFailed
from vasool.diagnosis.proposal import ProposalRole, notice_proposal_from, template_ids
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


class FakeDebiter:
    """Stands in for the mandate-debit port (vasool/actions/debit.py): takes
    every debit and answers with a fresh payment id, unless told to fail."""

    def __init__(self):
        self.retries: list = []
        self.fail_next: DebitAttempt | None = None

    def debit(self, proposal):
        if self.fail_next is not None:
            attempt, self.fail_next = self.fail_next, None
            return attempt
        self.retries.append(proposal)
        payment_id = f"pay_retry_{len(self.retries)}"
        return DebitAttempt(ok=True, detail="debit requested", payment_id=payment_id, response={"id": payment_id})


def make_executor(*, registered_templates=None):
    client = FakeRazorpayClient()
    calls: list[tuple] = []

    def deliver(proposal, params):
        calls.append((proposal, params))
        return {"delivered": True}

    templates = registered_templates if registered_templates is not None else template_ids()
    executor = RazorpayExecutor(
        client=client, comms=CommsSender(deliver=deliver), registered_templates=templates,
        debiter=FakeDebiter(),
    )
    client.retries = executor.debiter.retries
    return executor, client, calls



class RecordingNotifier:
    """A rail that accepts every pre-debit notification request."""

    def __init__(self):
        self.requests: list = []

    def request(self, proposal):
        self.requests.append(proposal)
        return NoticeRequest(ok=True, detail="requested", reference="rvc_test")


class TestThePreDebitNotice:
    """The notice is the issuer's; the executor only asks the rail for it
    (vasool/actions/notice.py, docs/EVALUATION.md §10, 2026-09-15)."""

    def notice(self):
        debit = proposal_for("gateway_technical_error")
        return notice_proposal_from(debit, execute_at=debit.execute_at)

    def test_it_goes_to_the_notifier_and_never_to_comms(self):
        executor, client, calls = make_executor()
        notifier = RecordingNotifier()
        executor.notifier = notifier

        result = executor.execute(self.notice())

        assert result.ok
        assert len(notifier.requests) == 1
        assert calls == [], "a notice the issuer sends has no DLT template of ours to carry"
        assert client.retries == [] and client.payment_links == []

    def test_the_default_notifier_refuses_rather_than_pretending(self):
        """No call that requests a notice has been observed on this account,
        so the adapter wired by default cannot make one, and says so."""
        executor, _, calls = make_executor()
        assert isinstance(executor.notifier, NullPreDebitNotifier)

        result = executor.execute(self.notice())

        assert not result.ok
        assert "no pre-debit notification request is wired" in result.detail
        assert calls == []

    def test_a_nudge_is_still_a_message(self):
        executor, _, calls = make_executor()
        executor.notifier = RecordingNotifier()
        _, nudge = proposals_for("insufficient_fund")
        assert nudge.role is ProposalRole.NUDGE

        executor.execute(nudge)

        assert len(calls) == 1 and executor.notifier.requests == []


class TestRouting:
    def test_human_queue_is_unroutable(self):
        executor, _, _ = make_executor()
        proposal = proposal_for("payment_risk_check_failed")
        assert proposal.intervention is InterventionType.HUMAN_QUEUE

        with pytest.raises(UnroutableProposal):
            executor.execute(proposal)

    def test_silent_retry_goes_to_the_debiter_and_nothing_else(self):
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
        executor.debiter.fail_next = DebitAttempt(ok=False, detail="boom")
        proposal = proposal_for("gateway_technical_error")

        executor.execute(proposal)

        assert executor.retry_index.entity_id_for("pay_retry_1") is None

    def test_a_link_never_touches_the_retry_index(self):
        """REAUTH_LINK/REATTEMPT_LINK go through _link, not _retry -- nothing
        about creating a Payment Link should populate a table meant for
        payment ids a debit returned."""
        executor, client, _ = make_executor()
        proposal = proposal_for("card_expired")

        executor.execute(proposal)

        record = executor.journal.get(proposal.proposal_id)
        assert record is not None and record.razorpay_request_id == "plink_1"
        assert executor.retry_index.entity_id_for("plink_1") is None


class TestFailureHandling:
    def test_a_razorpay_failure_on_retry_is_recorded_not_raised(self):
        executor, client, _ = make_executor()
        executor.debiter.fail_next = DebitAttempt(ok=False, detail="boom")
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


class TestTheDebitPort:
    """The debit is a port whose default refuses (vasool/actions/debit.py), and
    a debit whose outcome is unknown says so all the way to the ledger
    (docs/EVALUATION.md §10, 2026-09-15)."""

    def test_the_default_debiter_refuses_and_nothing_reaches_the_client(self):
        client = FakeRazorpayClient()
        executor = RazorpayExecutor(
            client=client, comms=CommsSender(deliver=lambda p, x: {}), registered_templates=template_ids()
        )
        assert isinstance(executor.debiter, NullMandateDebiter)
        result = executor.execute(proposal_for("gateway_technical_error"))
        assert not result.ok and not result.outcome_unknown
        assert client.payment_links == [] and client.notifications == []

    def test_an_unknown_outcome_travels_to_the_result_and_the_journal(self):
        executor, _, _ = make_executor()
        executor.debiter.fail_next = DebitAttempt(ok=False, detail="response lost", outcome_unknown=True)
        proposal = proposal_for("gateway_technical_error")
        result = executor.execute(proposal)
        assert not result.ok and result.outcome_unknown
        assert executor.journal.get(proposal.proposal_id).outcome_unknown


class TestTheStatusCheckPort:
    def test_the_default_cannot_tell_and_says_the_check_told_us_nothing(self):
        from vasool.diagnosis.proposal import status_check_proposal_from
        from vasool.policy.machine import RailStatus

        executor, _, _ = make_executor()
        debit = proposal_for("gateway_technical_error")
        check = status_check_proposal_from(debit, execute_at=debit.execute_at, check=1)
        result = executor.execute(check)
        assert result.status is RailStatus.CANNOT_TELL and not result.ok


class FakeSDK:
    """Razorpay's SDK surface the documented adapters use — orders, customers,
    recurring payments — recording every request."""

    def __init__(self, *, order_status="paid", attempts=1):
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Orders:
            def create(self, data, **kwargs):
                outer.requests.append(("order.create", data))
                return {"id": "order_1", "status": "created", "attempts": 0,
                        "notification": {"token_id": data["notification"]["token_id"]}}

            def fetch(self, order_id, **kwargs):
                outer.requests.append(("order.fetch", {"id": order_id}))
                return {"id": order_id, "status": order_status, "attempts": attempts, "amount_paid": 1500}

        class Customers:
            def fetch(self, customer_id, **kwargs):
                outer.requests.append(("customer.fetch", {"id": customer_id}))
                return {"id": customer_id, "email": "c@example.invalid", "contact": "+919812345678"}

        class Payments:
            def createRecurring(self, data, **kwargs):  # noqa: N802
                outer.requests.append(("payment.createRecurring", data))
                return {"razorpay_payment_id": "pay_documented_1"}

        self.order, self.customer, self.payment = Orders(), Customers(), Payments()


def _upi_mandate(**changes):
    from datetime import datetime, timezone

    from vasool.mandate.record import MandateCategory, MandateRail, MandateRecord
    from vasool.mandate.states import MandateState

    fields = dict(mandate_id="umn_1", rail=MandateRail.UPI_AUTOPAY, category=MandateCategory.GENERAL,
                  state=MandateState.ACTIVE, valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
                  token_id="token_1", razorpay_customer_id="cust_1")
    fields.update(changes)
    return MandateRecord(**fields)


class TestTheDocumentedAdapters:
    """Held to Razorpay's *Create Subsequent Payments*: the notice is an order
    carrying a notification object, the debit is createRecurring with its eight
    mandatory fields against that order, and the status is read off the order."""

    MANDATORY = {"email", "contact", "currency", "amount", "order_id", "customer_id", "token", "recurring"}

    def _adapters(self, sdk, mandate=None):
        from vasool.actions.executor import documented_adapters

        record = mandate if mandate is not None else _upi_mandate()
        return documented_adapters(sdk_client=sdk, mandates=lambda entity_id: record)

    def test_the_notice_is_an_order_with_a_notification_object(self):
        sdk = FakeSDK()
        request = self._adapters(sdk).notifier.request(proposal_for("gateway_technical_error"))
        assert request.ok and request.reference == "order_1"
        name, data = sdk.requests[0]
        assert name == "order.create" and data["notification"] == {"token_id": "token_1"}

    def test_the_debit_sends_the_eight_mandatory_fields_and_no_undocumented_one(self):
        sdk = FakeSDK()
        adapters = self._adapters(sdk)
        proposal = proposal_for("gateway_technical_error")
        adapters.notifier.request(proposal)
        attempt = adapters.debiter.debit(proposal)
        assert attempt.ok and attempt.payment_id == "pay_documented_1"
        data = dict(sdk.requests)["payment.createRecurring"]
        assert self.MANDATORY <= set(data)
        assert "payment_id" not in data, "retry_payment's undocumented field is gone"
        assert data["order_id"] == "order_1" and data["token"] == "token_1"
        assert data["notes"]["vasool_entity_id"] == proposal.entity_id

    def test_the_debit_refuses_what_nothing_documented_can_present(self):
        from vasool.actions.executor import documented_adapters

        proposal = proposal_for("gateway_technical_error")
        one_time = documented_adapters(sdk_client=FakeSDK(), mandates=lambda entity_id: None)
        assert not one_time.debiter.debit(proposal).ok, "a one-time payment has no token"
        unnotified = self._adapters(FakeSDK())
        assert not unnotified.debiter.debit(proposal).ok, "no notified order to debit against"
        tokenless = self._adapters(FakeSDK(), mandate=_upi_mandate(token_id=None))
        assert not tokenless.debiter.debit(proposal).ok

    def test_the_status_is_read_off_the_order(self):
        from vasool.policy.machine import RailStatus

        proposal = proposal_for("gateway_technical_error")
        for status, attempts, answer in (("paid", 1, RailStatus.DEBITED),
                                         ("created", 0, RailStatus.NOT_DEBITED),
                                         ("attempted", 1, RailStatus.PENDING)):
            adapters = self._adapters(FakeSDK(order_status=status, attempts=attempts))
            adapters.notifier.request(proposal)
            assert adapters.status.check(proposal).answer is answer
        unnotified = self._adapters(FakeSDK())
        assert unnotified.status.check(proposal).answer is RailStatus.CANNOT_TELL
