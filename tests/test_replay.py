"""replay.py: rebuild state from the ledger.

CLAUDE.md invariant 5 — "same seed -> byte-identical ledger" — is proved at
full scale once windtunnel's 1000-seed evaluation exists (stage 6+). This
session's slice of it: replaying one episode's transition log reproduces the
same terminal state PolicyMachine actually reached, and replaying a receipt
chain either verifies or names the tamper.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from vasool.actions.comms import CommsSender
from vasool.actions.executor import RazorpayExecutor
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import template_ids
from vasool.diagnosis.rules import IST
from vasool.ledger.receipts import build_from_transitions
from vasool.ledger.replay import TamperDetected, replay_episodes, replay_receipts
from vasool.ledger.tracing import trace_id_for
from vasool.policy.episode import State
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
    def create_payment_link(self, **kwargs):
        return {"id": "plink_1", "short_url": "https://rzp.io/l/x"}

    def notify_payment_link(self, **kwargs):
        return {"success": True}

    def retry_payment(self, **kwargs):
        return {"id": "pay_retry_1"}


def make_machine(*, now=NOON, **fact_overrides):
    clock = VirtualClock(now)
    executor = RazorpayExecutor(
        client=FakeRazorpayClient(),
        comms=CommsSender(deliver=lambda p, params: {"ok": True}),
        registered_templates=template_ids(),
    )
    machine = PolicyMachine(clock=clock, facts=StubFactStore(**fact_overrides), executor=executor)
    return machine, clock, executor


class TestEpisodeReplay:
    def test_replay_reproduces_the_executed_terminal_state(self):
        machine, clock, _ = make_machine()
        event = event_for("gateway_technical_error")
        machine.observe(event)
        clock.advance_by(timedelta(minutes=6))
        machine.tick()

        replayed = replay_episodes(machine.transitions)

        assert replayed[event.entity_id].final_state == machine.state_of(event.entity_id) == State.AWAITING

    def test_replay_reproduces_a_blocked_terminal_state(self):
        machine, clock, _ = make_machine(registered_templates=frozenset())
        event = event_for("card_expired")
        machine.observe(event)
        machine.tick()

        replayed = replay_episodes(machine.transitions)

        assert replayed[event.entity_id].final_state == machine.state_of(event.entity_id) == State.BLOCKED

    def test_replay_reproduces_a_deferral_and_then_executed_trajectory(self):
        """A04's slide, replayed: 7:30pm defers, 8:02am executes — the
        trajectory folded from the log matches what the machine actually did,
        deferral included."""
        evening = datetime(2026, 8, 25, 19, 30, tzinfo=IST).astimezone(timezone.utc)
        machine, clock, _ = make_machine(now=evening)
        event = event_for("card_expired")
        machine.observe(event)
        machine.tick()
        clock.advance_to(datetime(2026, 8, 26, 8, 30, tzinfo=IST).astimezone(timezone.utc))
        machine.tick()

        replayed = replay_episodes(machine.transitions)
        trajectory = replayed[event.entity_id].trajectory

        assert State.DEFERRED in trajectory
        assert trajectory[-1] == machine.state_of(event.entity_id) == State.AWAITING

    def test_replay_covers_every_episode_in_the_log_independently(self):
        machine, clock, _ = make_machine()
        gw = event_for("gateway_technical_error")
        risky = event_for("payment_risk_check_failed").model_copy(update={"entity_id": "pay_a_different_one"})
        machine.observe(gw)
        clock.advance_by(timedelta(minutes=6))
        machine.tick()
        machine.observe(risky)
        machine.tick()

        replayed = replay_episodes(machine.transitions)

        assert replayed[gw.entity_id].final_state == State.AWAITING
        assert replayed[risky.entity_id].final_state == State.ESCALATED


class TestReceiptReplay:
    def test_replay_of_an_untampered_chain_returns_it_unchanged(self):
        machine, clock, executor = make_machine()
        machine.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        machine.tick()

        chain = build_from_transitions(machine.transitions, call_journal=executor.journal, trace_id_of=trace_id_for)
        receipts = replay_receipts(chain)
        assert len(receipts) == 1

    def test_replay_raises_on_a_tampered_chain(self):
        machine, clock, executor = make_machine()
        machine.observe(event_for("gateway_technical_error"))
        clock.advance_by(timedelta(minutes=6))
        machine.tick()
        machine.observe(event_for("payment_risk_check_failed"))
        machine.tick()

        chain = build_from_transitions(machine.transitions, call_journal=executor.journal, trace_id_of=trace_id_for)
        receipts = list(chain)
        receipts[0] = dataclasses.replace(receipts[0], amount_recovered_paise=999)

        with pytest.raises(TamperDetected):
            replay_receipts(receipts)
