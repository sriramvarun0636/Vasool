"""receipts.py: every executed proposal produces exactly one receipt; a
BLOCKED and an ESCALATED decision each produce one too. Tamper with a receipt
mid-chain and verify_chain has to say so.

Drives a real PolicyMachine end to end with a real RazorpayExecutor wired to
a fake Razorpay client — no network, no mocking of the policy plane, which
this session does not touch.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from vasool.actions.comms import CommsSender
from vasool.actions.executor import RazorpayExecutor
from vasool.actions.razorpay_client import RazorpayCallFailed
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import ProposalRole, _derive_id, template_ids
from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import InterventionType
from vasool.ledger.receipts import Outcome, _receipt_id, build_from_transitions, verify_chain
from vasool.ledger.tracing import trace_id_for
from vasool.policy.episode import State
from vasool.policy.facts import PolicyFacts
from vasool.policy.machine import PolicyMachine
from tests.payloads import event_for
from tests.policy.strategies import permissive_facts

NOON = datetime(2026, 8, 25, 12, 0, tzinfo=IST).astimezone(timezone.utc)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
STUBBED_DIR = REPO_ROOT / "data" / "stubbed_payloads"


def _stub_entity_id(scenario: str) -> str:
    body = json.loads((STUBBED_DIR / f"SIMULATED__payment_failed__{scenario}.json").read_text())["body"]
    return body["payload"]["payment"]["entity"]["id"]


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


class TestSettlementProducesARecoveredReceipt:
    """amount_recovered_paise is the headline metric of the whole project
    (CLAUDE.md) — this is what makes it real instead of structurally 0."""

    def test_settlement_after_an_execution_carries_the_real_amount(self):
        machine, clock, executor = make_machine()
        event = event_for("gateway_technical_error")
        machine.observe(event)
        clock.advance_by(timedelta(minutes=6))
        machine.tick()

        machine.settled(event.entity_id, reason="captured", amount_paise=event.amount_paise)

        chain = receipts_for(machine, executor)
        recovered = [r for r in chain if r.outcome is Outcome.RECOVERED]
        assert len(recovered) == 1
        assert recovered[0].amount_recovered_paise == event.amount_paise
        assert recovered[0].executed is False
        assert recovered[0].proposal is None
        assert recovered[0].event_id is None

    def test_the_earlier_executed_receipt_is_untouched_by_settlement(self):
        """The chain's answer to "amend or append": the EXECUTED receipt
        still reads amount_recovered_paise == 0 after settlement — it is
        never rewritten, only a new receipt is appended after it."""
        machine, clock, executor = make_machine()
        event = event_for("gateway_technical_error")
        machine.observe(event)
        clock.advance_by(timedelta(minutes=6))
        machine.tick()

        machine.settled(event.entity_id, reason="captured", amount_paise=event.amount_paise)

        chain = receipts_for(machine, executor)
        executed = [r for r in chain if r.outcome is Outcome.EXECUTED]
        assert len(executed) == 1
        assert executed[0].amount_recovered_paise == 0

    def test_settlement_with_nothing_ever_executed_still_carries_the_amount(self):
        """A07: an out-of-band payment can close an episode before a single
        proposal was ever gated. There is no Proposal to hang a receipt off
        of, but the amount still has to reach the ledger."""
        machine, clock, executor = make_machine()
        event = event_for("insufficient_fund")
        machine.observe(event)

        machine.settled(event.entity_id, reason="out-of-band payment", amount_paise=event.amount_paise)

        chain = receipts_for(machine, executor)
        recovered = [r for r in chain if r.outcome is Outcome.RECOVERED]
        assert len(recovered) == 1
        assert recovered[0].amount_recovered_paise == event.amount_paise
        assert recovered[0].proposal is None

    def test_the_recovered_receipt_extends_the_hash_chain(self):
        machine, clock, executor = make_machine()
        event = event_for("gateway_technical_error")
        machine.observe(event)
        clock.advance_by(timedelta(minutes=6))
        machine.tick()
        machine.settled(event.entity_id, reason="captured", amount_paise=event.amount_paise)

        chain = list(receipts_for(machine, executor))
        assert len(chain) == 2
        assert chain[1].prev_hash == chain[0].hash
        assert verify_chain(chain)

    def test_tampering_with_the_recovered_amount_is_detected(self):
        machine, clock, executor = make_machine()
        event = event_for("gateway_technical_error")
        machine.observe(event)
        clock.advance_by(timedelta(minutes=6))
        machine.tick()
        machine.settled(event.entity_id, reason="captured", amount_paise=event.amount_paise)

        receipts = list(receipts_for(machine, executor))
        recovered_index = next(i for i, r in enumerate(receipts) if r.outcome is Outcome.RECOVERED)
        receipts[recovered_index] = dataclasses.replace(
            receipts[recovered_index], amount_recovered_paise=event.amount_paise + 1
        )

        assert not verify_chain(receipts)


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


# ---------------------------------------------------------------------------
# item 4: the rcpt_aa8ce1313a1ceab9 collision -- fixture artifact, not a
# defect in _receipt_id. See that function's own docstring for the full
# investigation; this proves the guarantee that actually holds.
# ---------------------------------------------------------------------------
class TestReceiptIdUniqueness:
    def test_card_expired_and_card_disabled_share_a_stub_entity_id(self):
        """The root cause, confirmed against disk rather than asserted:
        tools/make_stubs.py derives every stub from the same real capture, so
        every file in data/stubbed_payloads/ carries the same payment id."""
        assert _stub_entity_id("card_expired") == _stub_entity_id("card_disabled_for_online_payments")

    def test_the_observed_collision_reproduces_and_is_explained_by_the_shared_id(self):
        """docs/taxonomy.md §4: both scenarios map to REAUTH_LINK, attempt 1,
        role PRIMARY -- so with an identical entity_id, _derive_id (which
        this collision is not about) hands _receipt_id an identical
        proposal_id too, and the collision is exactly what the basis string
        predicts, not a hash accident."""
        entity_id = _stub_entity_id("card_expired")
        proposal_id = _derive_id(entity_id, InterventionType.REAUTH_LINK, 1, ProposalRole.PRIMARY.value)

        collided = _receipt_id(entity_id, proposal_id, State.EXECUTING)
        assert collided == "rcpt_aa8ce1313a1ceab9"  # the id actually observed this session

        # The same logical action against a genuinely different payment does not collide.
        different = _receipt_id("pay_a_genuinely_different_customers_payment", proposal_id, State.EXECUTING)
        assert different != collided

    @given(
        entity_id_a=st.text(
            alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="|"), min_size=1, max_size=24
        ),
        entity_id_b=st.text(
            alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="|"), min_size=1, max_size=24
        ),
        proposal_id=st.one_of(
            st.none(),
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="|"),
                min_size=1,
                max_size=24,
            ),
        ),
        to_state=st.sampled_from(sorted(State, key=lambda s: s.value)),
    )
    def test_different_entity_ids_never_collide(self, entity_id_a, entity_id_b, proposal_id, to_state):
        """The guarantee that actually holds in production: entity_id is
        Razorpay's own globally-unique payment id, so two receipts for two
        different payments cannot share a receipt_id regardless of what
        proposal_id or to_state they'd otherwise share."""
        if entity_id_a == entity_id_b:
            return
        assert _receipt_id(entity_id_a, proposal_id, to_state) != _receipt_id(entity_id_b, proposal_id, to_state)
