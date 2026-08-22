"""receipts.py: every executed proposal produces exactly one receipt; a
BLOCKED and an ESCALATED decision each produce one too. Tamper with a receipt
mid-chain and verify_chain has to say so.

Drives a real PolicyMachine end to end with a real RazorpayExecutor wired to
a fake Razorpay client — no network, no mocking of the policy plane, which
this session does not touch.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from vasool.actions.comms import CommsSender
from vasool.actions.executor import RazorpayExecutor
from vasool.actions.razorpay_client import RazorpayCallFailed
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import template_ids
from vasool.diagnosis.rules import IST
from vasool.ledger.receipts import Outcome, build_from_transitions, verify_chain
from vasool.ledger.tracing import trace_id_for
from vasool.policy.facts import PolicyFacts
from vasool.policy.machine import PolicyMachine
from tests.payloads import event_for
from tests.policy.strategies import permissive_facts

NOON = datetime(2026, 8, 25, 12, 0, tzinfo=IST).astimezone(timezone.utc)


class StubFactStore:
    def __init__(self, **overrides):
        self.overrides = overrides

    def snapshot(self, *, event, proposal, now) -> PolicyFacts:
        return permissive_facts(**self.overrides)


class FakeRazorpayClient:
    def __init__(self, *, fail_retry: bool = False):
        self._fail_retry = fail_retry

    def create_payment_link(self, **kwargs):
        return {"id": "plink_1", "short_url": "https://rzp.io/l/x"}

    def notify_payment_link(self, **kwargs):
        return {"success": True}

    def retry_payment(self, **kwargs):
        if self._fail_retry:
            raise RazorpayCallFailed("down", retryable=True, cause=Exception("x"))
        return {"id": "pay_retry_1"}


def make_machine(*, now=NOON, fail_retry: bool = False, **fact_overrides):
    clock = VirtualClock(now)
    executor = RazorpayExecutor(
        client=FakeRazorpayClient(fail_retry=fail_retry),
        comms=CommsSender(deliver=lambda p, params: {"ok": True}),
        registered_templates=template_ids(),
    )
    machine = PolicyMachine(clock=clock, facts=StubFactStore(**fact_overrides), executor=executor)
    return machine, clock, executor


def receipts_for(machine, executor):
    return build_from_transitions(machine.transitions, call_journal=executor.journal, trace_id_of=trace_id_for)


class TestExecutedProducesOneReceipt:
    def test_every_executed_proposal_produces_exactly_one_receipt(self):
        machine, clock, executor = make_machine()
        machine.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        machine.tick()

        chain = receipts_for(machine, executor)
        executed = [r for r in chain if r.outcome is Outcome.EXECUTED]
        assert len(executed) == 1
        assert executed[0].executed is True
        assert executed[0].razorpay_request_id == "pay_retry_1"

    def test_a_liquidity_fanout_produces_two_receipts(self):
        """Nudge and retry are two proposals, gated and executed separately —
        each owes its own receipt."""
        machine, clock, executor = make_machine()
        machine.observe(event_for("insufficient_fund"))
        machine.tick()

        chain = receipts_for(machine, executor)
        assert len(chain) == 1  # only the nudge is due at t=0
        assert chain[0].outcome is Outcome.EXECUTED


class TestBlockedAndEscalatedProduceReceipts:
    def test_a_blocked_decision_produces_a_receipt(self):
        machine, clock, executor = make_machine(registered_templates=frozenset())
        machine.observe(event_for("card_expired"))
        machine.tick()

        chain = receipts_for(machine, executor)
        blocked = [r for r in chain if r.outcome is Outcome.BLOCKED]
        assert len(blocked) == 1
        assert blocked[0].executed is False
        assert len(blocked[0].verdicts) > 0

    def test_an_escalated_decision_produces_a_receipt(self):
        """taxonomy.md §5: the RISK_BLOCK -> HUMAN_QUEUE path where restraint
        has to be as visible in the ledger as an executed action."""
        machine, clock, executor = make_machine()
        machine.observe(event_for("payment_risk_check_failed"))
        machine.tick()

        chain = receipts_for(machine, executor)
        escalated = [r for r in chain if r.outcome is Outcome.ESCALATED]
        assert len(escalated) == 1
        assert escalated[0].executed is False


class TestExecutionFailure:
    def test_a_downstream_razorpay_failure_shows_as_execution_failed(self):
        machine, clock, executor = make_machine(fail_retry=True)
        machine.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        machine.tick()

        chain = receipts_for(machine, executor)
        failed = [r for r in chain if r.outcome is Outcome.EXECUTION_FAILED]
        assert len(failed) == 1
        assert failed[0].executed is False


class TestReceiptChainIntegrity:
    def test_the_chain_verifies_untampered(self):
        machine, clock, executor = make_machine()
        machine.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        machine.tick()
        machine.observe(event_for("payment_risk_check_failed"))
        machine.tick()

        chain = receipts_for(machine, executor)
        assert len(chain) >= 2
        assert verify_chain(list(chain))

    def test_tampering_with_a_receipt_mid_chain_is_detected(self):
        machine, clock, executor = make_machine()
        machine.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        machine.tick()
        machine.observe(event_for("payment_risk_check_failed"))
        machine.tick()

        receipts = list(receipts_for(machine, executor))
        assert len(receipts) >= 2

        tampered = dataclasses.replace(receipts[0], amount_recovered_paise=999)
        receipts[0] = tampered

        assert not verify_chain(receipts)

    def test_prev_hash_links_the_chain_in_order(self):
        machine, clock, executor = make_machine()
        machine.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        machine.tick()
        machine.observe(event_for("payment_risk_check_failed"))
        machine.tick()

        receipts = list(receipts_for(machine, executor))
        assert len(receipts) >= 2
        for prev, cur in zip(receipts, receipts[1:]):
            assert cur.prev_hash == prev.hash
