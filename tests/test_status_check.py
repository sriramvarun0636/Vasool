"""The status check: what the state machine does when money may have moved.

A debit whose response was lost goes to a status check at NPCI's 90 seconds,
never to a second debit; a PENDING answer is asked again, up to NPCI's three,
spaced inside two hours; DEBITED closes the episode as recovered; anything
else goes to a person (vasool/policy/machine.py; docs/EVALUATION.md §10,
2026-09-15).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from vasool.actions.comms import CommsSender
from vasool.actions.debit import DebitAttempt
from vasool.actions.executor import RazorpayExecutor
from vasool.actions.status import StatusReading
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import status_check_proposal_from, template_ids
from vasool.diagnosis.taxonomy import InterventionType
from vasool.ledger.receipts import Outcome, build_from_transitions, verify_chain
from vasool.ledger.tracing import trace_id_for
from vasool.policy.episode import State
from vasool.policy.facts import GuardContext
from vasool.policy.guards.human_approval import HumanApprovalGuard
from vasool.policy.guards.promise_to_pay import PromiseToPayGuard
from vasool.policy.machine import (
    STATUS_CHECK_FIRST_AFTER,
    STATUS_CHECK_SPACING,
    STATUS_CHECKS_MAX,
    PolicyMachine,
    RailStatus,
)
from tests.payloads import event_for
from tests.policy.strategies import permissive_facts, proposal_for
from tests.test_receipts import NOON, FakeRazorpayClient, StubFactStore


class LosesTheResponse:
    """A debit the rail took and never answered."""

    def __init__(self):
        self.debits = 0

    def debit(self, proposal):
        self.debits += 1
        return DebitAttempt(ok=False, detail="response lost", outcome_unknown=True)


class Answers:
    """A status check that says what it is told to, in order, then CANNOT_TELL."""

    def __init__(self, *answers: RailStatus, amount_paise: int | None = None):
        self.answers = list(answers)
        self.asked: list = []
        self.amount = amount_paise

    def check(self, proposal):
        self.asked.append(proposal)
        answer = self.answers.pop(0) if self.answers else RailStatus.CANNOT_TELL
        return StatusReading(answer, f"scripted {answer.value}",
                             amount_paise=self.amount if answer is RailStatus.DEBITED else None)


def _run(*answers: RailStatus, amount_paise: int | None = None):
    clock = VirtualClock(NOON)
    debiter, status = LosesTheResponse(), Answers(*answers, amount_paise=amount_paise)
    executor = RazorpayExecutor(
        client=FakeRazorpayClient(), comms=CommsSender(deliver=lambda p, x: {}),
        registered_templates=template_ids(), debiter=debiter, status=status,
    )
    machine = PolicyMachine(clock=clock, facts=StubFactStore(), executor=executor)
    event = event_for("gateway_technical_error")
    machine.observe(event)
    for _ in range(40):
        due = [item.proposal.execute_at for item in machine.pending()]
        if not due:
            break
        clock.advance_to(min(due))
        machine.tick()
    receipts = build_from_transitions(machine.transitions, call_journal=executor.journal, trace_id_of=trace_id_for)
    return machine, event, debiter, status, list(receipts)


class TestALostResponseIsCheckedNotResent:
    def test_one_debit_then_checks_and_never_a_second_debit(self):
        machine, event, debiter, status, receipts = _run(RailStatus.PENDING, RailStatus.PENDING, RailStatus.PENDING)
        assert debiter.debits == 1
        assert len(status.asked) == STATUS_CHECKS_MAX
        assert machine.state_of(event.entity_id) is State.ESCALATED
        assert [r.outcome for r in receipts].count(Outcome.OUTCOME_UNKNOWN) == 1

    def test_the_checks_are_timed_by_npci_oc_215(self):
        _, _, _, status, receipts = _run(RailStatus.PENDING, RailStatus.PENDING, RailStatus.PENDING)
        lost = next(r for r in receipts if r.outcome is Outcome.OUTCOME_UNKNOWN)
        times = [p.execute_at for p in status.asked]
        assert times[0] == lost.at + STATUS_CHECK_FIRST_AFTER
        assert [t - times[0] for t in times] == list(STATUS_CHECK_SPACING)
        assert times[-1] - lost.at <= timedelta(hours=2), "OC-215 ¶4: preferably within 2 hours"

    def test_each_check_is_its_own_receipt_and_the_chain_holds(self):
        *_, receipts = _run(RailStatus.PENDING, RailStatus.PENDING, RailStatus.PENDING)
        ids = [r.receipt_id for r in receipts]
        assert len(ids) == len(set(ids))
        assert verify_chain(receipts)
        checks = [r for r in receipts if r.proposal and r.proposal.intervention is InterventionType.STATUS_CHECK]
        assert len({r.proposal.proposal_id for r in checks}) == STATUS_CHECKS_MAX

    def test_debited_is_recovered_with_what_the_rail_reports(self):
        machine, event, debiter, _, receipts = _run(RailStatus.DEBITED, amount_paise=12_345)
        assert machine.state_of(event.entity_id) is State.RECOVERED
        recovered = [r for r in receipts if r.outcome is Outcome.RECOVERED]
        assert len(recovered) == 1 and recovered[0].amount_recovered_paise == 12_345
        assert debiter.debits == 1

    @pytest.mark.parametrize("answer", [RailStatus.NOT_DEBITED, RailStatus.CANNOT_TELL])
    def test_any_other_answer_goes_to_a_person_after_one_check(self, answer):
        machine, event, debiter, status, receipts = _run(answer)
        assert len(status.asked) == 1 and debiter.debits == 1
        assert machine.state_of(event.entity_id) is State.ESCALATED
        assert receipts[-1].outcome is Outcome.ESCALATED


class TestTheGuardsAStatusCheckSkips:
    def _ctx(self, **facts):
        debit = proposal_for("gateway_technical_error")
        check = status_check_proposal_from(debit, execute_at=debit.execute_at, check=1)
        return GuardContext(now=check.execute_at, effective_at=check.execute_at,
                            event=event_for("gateway_technical_error"), proposal=check,
                            facts=permissive_facts(**facts))

    def test_a_promise_to_pay_does_not_hold_it(self):
        ctx = self._ctx(promise_to_pay=(NOON + timedelta(days=5)).date())
        assert not PromiseToPayGuard().applies_to(ctx)

    def test_the_unattended_amount_ceiling_does_not_escalate_it(self):
        assert not HumanApprovalGuard().applies_to(self._ctx())
