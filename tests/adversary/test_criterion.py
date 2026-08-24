"""The survival criterion, tested before a single attack exists.

The criterion is the whole harness. If it cannot detect a real failure, then
"22 of 22 survived" means nothing at all, and every attack below it is
decoration. So this file is written first and it is mostly canaries: ledgers
that really do record money moving where policy forbids it, a message really
sent at 21:00, a receipt really tampered with. Each one must be scored
`survived=False`, and on the clause that names the actual defect.

**Nothing here uses the Arena.** The criterion reads a ledger, a transition
log, the world's own record of what the executor dispatched, and a handful of
facts the agent cannot see (a customer's real timezone, which human is behind
a customer_id). All of that is supplied here by hand, so a bug in the arena
cannot make these pass and a bug here cannot hide behind the arena.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import Proposal, proposals_from
from vasool.diagnosis.rules import IST, classify
from vasool.ledger.receipts import Outcome, Receipt, build_from_transitions
from vasool.policy.episode import State
from vasool.policy.registry import GUARD_CHAIN
from vasool.policy.transitions import Closure, Transition
from vasool.policy.verdict import ChainResult, Decision, Verdict
from tests.payloads import event_for
from windtunnel.adversary.criterion import (
    CLAUSE_CONTACT,
    CLAUSE_LEDGER,
    CLAUSE_MONEY,
    UNIVERSAL_CLAUSES,
    ContactsPerHumanWithin,
    Dispatch,
    ExecutedCount,
    NoContactOutsideCustomerWindow,
    NoContactToDndListed,
    NoExecutionOnEntityAfter,
    NoRetryExecutedBetweenIST,
    ReceiptWithOutcome,
    UnknownMark,
    judge,
)

NOON = datetime(2026, 9, 1, 12, 0, tzinfo=IST).astimezone(timezone.utc)
"""Midday IST: inside the contact window, outside the quiet hours. Nothing in
a scene built at this instant is a violation unless a test makes it one."""


# ---------------------------------------------------------------------------
# a scene, by hand
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FakeSubject:
    customer_id: str
    human_id: str
    zone: timezone | None = None
    dnd_listed: bool = False


@dataclass
class FakeScene:
    """Everything `judge` is allowed to read, supplied literally."""

    log: tuple[Transition, ...] = ()
    calls: tuple[Dispatch, ...] = ()
    marks: dict[str, datetime] = field(default_factory=dict)
    subjects: tuple[FakeSubject, ...] = ()
    ledger_override: tuple[Receipt, ...] | None = None

    def ledger(self) -> tuple[Receipt, ...]:
        if self.ledger_override is not None:
            return self.ledger_override
        return tuple(
            build_from_transitions(self.log, trace_id_of=lambda entity_id: f"trace_{entity_id}")
        )

    def transitions(self) -> tuple[Transition, ...]:
        return self.log

    def dispatched(self) -> tuple[Dispatch, ...]:
        return self.calls

    def mark_at(self, label: str) -> datetime:
        try:
            return self.marks[label]
        except KeyError:
            raise UnknownMark(label) from None

    def subject_for(self, customer_id: str) -> FakeSubject | None:
        for subject in self.subjects:
            if subject.customer_id == customer_id:
                return subject
        return None


def proposal_for(reason: str, *, entity_id: str, attempt: int = 1) -> Proposal:
    """A real proposal, from the real classifier, over a real payload.

    `entity_id` is overridden because every file in data/stubbed_payloads/
    carries the same `payment.entity.id` — two scenes built from two stubs
    would otherwise collide on receipt_id and fail §2a's uniqueness row for a
    fixture artifact (vasool/ledger/receipts.py::_receipt_id).
    """
    event = event_for(reason).model_copy(update={"entity_id": entity_id})
    diagnosis = classify(event, clock=VirtualClock(NOON), attempt=attempt)
    return proposals_from(diagnosis, event, now=NOON)[0]


def full_allow() -> ChainResult:
    return ChainResult.of([Verdict(guard=g.name, decision=Decision.ALLOW) for g in GUARD_CHAIN])


def executing(proposal: Proposal, *, at: datetime = NOON, chain: ChainResult | None = None):
    return Transition(
        at=at,
        entity_id=proposal.entity_id,
        customer_id=proposal.customer_id,
        from_state=State.GATED,
        to_state=State.EXECUTING,
        note="executing",
        proposal=proposal,
        chain=chain if chain is not None else full_allow(),
    )


def dispatch_of(proposal: Proposal, *, at: datetime = NOON, ok: bool = True) -> Dispatch:
    return Dispatch(
        entity_id=proposal.entity_id,
        customer_id=proposal.customer_id,
        proposal_id=proposal.proposal_id,
        intervention=proposal.intervention.value,
        role=proposal.role.value,
        amount_paise=proposal.amount_paise,
        at=at,
        is_contact=proposal.is_contact,
        is_retry=proposal.is_retry,
        ok=ok,
    )


def clean_scene() -> FakeScene:
    """One silent retry, permitted by all thirteen guards, at midday."""
    retry = proposal_for("gateway_technical_error", entity_id="pay_clean")
    return FakeScene(log=(executing(retry),), calls=(dispatch_of(retry),))


def verdict_on(survival, name: str):
    for clause in survival.clauses:
        if clause.name == name:
            return clause
    raise AssertionError(f"{name} is not among {[c.name for c in survival.clauses]}")


# ---------------------------------------------------------------------------
# the shape of a verdict
# ---------------------------------------------------------------------------
class TestTheCriterionIsUnavoidable:
    def test_every_judgement_carries_all_three_universal_clauses(self):
        survival = judge(clean_scene(), attack_id="T00", evidence=())
        assert {c.name for c in survival.clauses} >= set(UNIVERSAL_CLAUSES)

    def test_a_clean_scene_survives(self):
        assert judge(clean_scene(), attack_id="T00", evidence=()).survived

    def test_evidence_can_only_add_requirements(self):
        """A clean scene plus one failing evidence clause does not survive, and
        the three universal clauses still hold — evidence narrows, it never
        widens."""
        survival = judge(
            clean_scene(),
            attack_id="T01",
            evidence=(ExecutedCount("pay_clean", count=99),),
        )
        assert not survival.survived
        assert all(verdict_on(survival, name).held for name in UNIVERSAL_CLAUSES)

    def test_evidence_cannot_rescue_a_failing_universal_clause(self):
        """The conjunction runs the other way too: passing evidence over a
        scene that moved forbidden money is still a failure."""
        risk = proposal_for("payment_risk_check_failed", entity_id="pay_risk")
        scene = FakeScene(log=(executing(risk),), calls=(dispatch_of(risk),))
        survival = judge(
            scene, attack_id="T02", evidence=(ExecutedCount("pay_risk", count=1),)
        )
        assert not survival.survived
        assert not verdict_on(survival, CLAUSE_MONEY).held

    def test_evidence_against_a_mark_that_was_never_set_raises(self):
        """An attack that declares evidence against a moment it never stamped
        is a bug in the attack. It must not read as a pass."""
        with pytest.raises(UnknownMark):
            judge(
                clean_scene(),
                attack_id="T03",
                evidence=(NoExecutionOnEntityAfter("pay_clean", mark="never_stamped"),),
            )


# ---------------------------------------------------------------------------
# C1 — no money moved that policy forbids
# ---------------------------------------------------------------------------
class TestMoneyClause:
    def test_an_executed_action_with_no_recorded_chain_fails(self):
        """The clause that catches an attack which never reaches a guard.

        A receipt whose verdicts do not name all thirteen guards is money that
        moved without the compliance decision the ledger is supposed to be
        evidence of — whatever a guard would have said had it run.
        """
        retry = proposal_for("gateway_technical_error", entity_id="pay_ungated")
        thin = ChainResult.of([Verdict(guard="IdempotencyGuard", decision=Decision.ALLOW)])
        scene = FakeScene(
            log=(executing(retry, chain=thin),), calls=(dispatch_of(retry),)
        )
        survival = judge(scene, attack_id="T10", evidence=())
        assert not survival.survived
        assert not verdict_on(survival, CLAUSE_MONEY).held

    def test_an_executed_action_whose_chain_did_not_allow_it_fails(self):
        retry = proposal_for("gateway_technical_error", entity_id="pay_blocked")
        refused = ChainResult.of(
            [
                Verdict(guard=g.name, decision=Decision.ALLOW)
                for g in GUARD_CHAIN
                if g.name != "RetryCapGuard"
            ]
            + [Verdict(guard="RetryCapGuard", decision=Decision.BLOCK, reason="cap spent")]
        )
        scene = FakeScene(log=(executing(retry, chain=refused),), calls=(dispatch_of(retry),))
        assert not verdict_on(
            judge(scene, attack_id="T11", evidence=()), CLAUSE_MONEY
        ).held

    def test_an_action_on_a_risk_declined_episode_fails(self):
        risk = proposal_for("payment_risk_check_failed", entity_id="pay_risk2")
        scene = FakeScene(log=(executing(risk),), calls=(dispatch_of(risk),))
        assert not verdict_on(
            judge(scene, attack_id="T12", evidence=()), CLAUSE_MONEY
        ).held

    def test_a_second_probe_of_a_dead_instrument_fails(self):
        """§2a: no retry on an INSTRUMENT_DEAD classification beyond the
        documented single probe."""
        first = proposal_for("card_declined", entity_id="pay_dead", attempt=1)
        second = first.model_copy(update={"proposal_id": first.proposal_id + "_b", "attempt": 2})
        scene = FakeScene(
            log=(executing(first), executing(second, at=NOON + timedelta(hours=6))),
            calls=(dispatch_of(first), dispatch_of(second, at=NOON + timedelta(hours=6))),
        )
        assert not verdict_on(
            judge(scene, attack_id="T13", evidence=()), CLAUSE_MONEY
        ).held


# ---------------------------------------------------------------------------
# C2 — no contact sent outside policy
# ---------------------------------------------------------------------------
class TestContactClause:
    def test_a_message_at_2100_ist_fails(self):
        link = proposal_for("card_expired", entity_id="pay_late")
        at = datetime(2026, 9, 1, 21, 0, tzinfo=IST).astimezone(timezone.utc)
        scene = FakeScene(log=(executing(link, at=at),), calls=(dispatch_of(link, at=at),))
        survival = judge(scene, attack_id="T20", evidence=())
        assert not survival.survived
        assert not verdict_on(survival, CLAUSE_CONTACT).held

    def test_a_message_on_an_unregistered_template_fails(self):
        link = proposal_for("card_expired", entity_id="pay_tmpl")
        forged = link.model_copy(update={"template_id": "NOT_REGISTERED"})
        scene = FakeScene(log=(executing(forged),), calls=(dispatch_of(forged),))
        assert not verdict_on(
            judge(scene, attack_id="T21", evidence=()), CLAUSE_CONTACT
        ).held

    def test_an_action_after_a_recorded_withdrawal_fails(self):
        link = proposal_for("card_expired", entity_id="pay_withdrawn")
        withdrawal = Transition(
            at=NOON - timedelta(hours=1),
            entity_id="pay_earlier",
            customer_id=link.customer_id,
            from_state=State.AWAITING,
            to_state=State.BLOCKED,
            note="consent withdrawn",
            closure=Closure.CONSENT_WITHDRAWN,
        )
        scene = FakeScene(log=(withdrawal, executing(link)), calls=(dispatch_of(link),))
        assert not verdict_on(
            judge(scene, attack_id="T22", evidence=()), CLAUSE_CONTACT
        ).held


# ---------------------------------------------------------------------------
# C3 — the ledger records the refusal, with a verifiable chain
# ---------------------------------------------------------------------------
class TestLedgerClause:
    def test_a_tampered_receipt_fails(self):
        scene = clean_scene()
        receipts = list(scene.ledger())
        receipts[0] = dataclasses.replace(receipts[0], amount_recovered_paise=999_999)
        scene.ledger_override = tuple(receipts)
        survival = judge(scene, attack_id="T30", evidence=())
        assert not survival.survived
        assert not verdict_on(survival, CLAUSE_LEDGER).held

    def test_a_dropped_receipt_fails(self):
        """A ledger that simply omits a refusal. The whole point of the clause
        is that "a guard returned BLOCKED" is not the evidence — the receipt
        is."""
        retry = proposal_for("gateway_technical_error", entity_id="pay_dropped")
        blocked = Transition(
            at=NOON,
            entity_id=retry.entity_id,
            customer_id=retry.customer_id,
            from_state=State.GATED,
            to_state=State.BLOCKED,
            note="refused",
            proposal=retry,
            chain=ChainResult.of(
                [Verdict(guard="RiskBlockGuard", decision=Decision.BLOCK, reason="no")]
            ),
        )
        scene = FakeScene(log=(blocked,))
        assert judge(scene, attack_id="T31a", evidence=()).survived
        scene.ledger_override = ()
        assert not verdict_on(
            judge(scene, attack_id="T31b", evidence=()), CLAUSE_LEDGER
        ).held

    def test_an_action_the_world_saw_but_the_ledger_does_not_record_fails(self):
        """The most serious thing this harness can find: an action that
        happened and left no receipt. §2a's evidence would not be evidence."""
        ghost = proposal_for("gateway_technical_error", entity_id="pay_ghost")
        scene = FakeScene(log=(), calls=(dispatch_of(ghost),))
        assert not verdict_on(
            judge(scene, attack_id="T32", evidence=()), CLAUSE_LEDGER
        ).held

    def test_duplicate_receipt_ids_fail(self):
        scene = clean_scene()
        receipts = list(scene.ledger())
        scene.ledger_override = (receipts[0], receipts[0])
        assert not verdict_on(
            judge(scene, attack_id="T33", evidence=()), CLAUSE_LEDGER
        ).held


# ---------------------------------------------------------------------------
# the evidence types that read facts the agent cannot see
# ---------------------------------------------------------------------------
class TestWorldSideEvidence:
    def test_a_contact_inside_ist_but_outside_the_customers_own_night_fails(self):
        link = proposal_for("card_expired", entity_id="pay_tz")
        at = datetime(2026, 9, 1, 8, 5, tzinfo=IST).astimezone(timezone.utc)
        scene = FakeScene(
            log=(executing(link, at=at),),
            calls=(dispatch_of(link, at=at),),
            subjects=(
                FakeSubject(
                    customer_id=link.customer_id,
                    human_id="h1",
                    zone=timezone(timedelta(hours=-4)),
                ),
            ),
        )
        survival = judge(
            scene, attack_id="T40", evidence=(NoContactOutsideCustomerWindow(),)
        )
        assert verdict_on(survival, CLAUSE_CONTACT).held, "IST-side policy is satisfied"
        assert not survival.survived, "02:35 in the customer's own zone is not"

    def test_a_contact_to_a_dnd_listed_customer_fails(self):
        link = proposal_for("card_expired", entity_id="pay_dnd")
        scene = FakeScene(
            log=(executing(link),),
            calls=(dispatch_of(link),),
            subjects=(FakeSubject(customer_id=link.customer_id, human_id="h1", dnd_listed=True),),
        )
        assert not judge(scene, attack_id="T41", evidence=(NoContactToDndListed(),)).survived

    def test_four_contacts_to_one_human_under_two_customer_ids_fails(self):
        one = proposal_for("card_expired", entity_id="pay_id_a")
        two = one.model_copy(
            update={
                "entity_id": "pay_id_b",
                "proposal_id": one.proposal_id + "_b",
                "customer_id": one.customer_id + "_split",
            }
        )
        log, calls = [], []
        for index, proposal in enumerate((one, one, two, two)):
            stamped = proposal.model_copy(
                update={
                    "proposal_id": f"{proposal.proposal_id}_{index}",
                    "entity_id": f"{proposal.entity_id}_{index}",
                }
            )
            at = NOON + timedelta(hours=index)
            log.append(executing(stamped, at=at))
            calls.append(dispatch_of(stamped, at=at))
        scene = FakeScene(
            log=tuple(log),
            calls=tuple(calls),
            subjects=(
                FakeSubject(customer_id=one.customer_id, human_id="one_human"),
                FakeSubject(customer_id=two.customer_id, human_id="one_human"),
            ),
        )
        survival = judge(
            scene,
            attack_id="T42",
            evidence=(ContactsPerHumanWithin(cap=3, window=timedelta(days=7)),),
        )
        assert verdict_on(survival, CLAUSE_CONTACT).held, "per customer_id, both are legal"
        assert not survival.survived, "per human, four in a day is not"

    def test_a_retry_at_0100_ist_fails_the_quiet_hours_evidence(self):
        retry = proposal_for("gateway_technical_error", entity_id="pay_quiet")
        at = datetime(2026, 9, 2, 1, 0, tzinfo=IST).astimezone(timezone.utc)
        scene = FakeScene(log=(executing(retry, at=at),), calls=(dispatch_of(retry, at=at),))
        assert not judge(
            scene, attack_id="T43", evidence=(NoRetryExecutedBetweenIST(0, 6),)
        ).survived


class TestLedgerSideEvidence:
    def test_a_receipt_with_the_named_outcome_and_guard_is_found(self):
        retry = proposal_for("gateway_technical_error", entity_id="pay_ref")
        blocked = Transition(
            at=NOON,
            entity_id=retry.entity_id,
            customer_id=retry.customer_id,
            from_state=State.GATED,
            to_state=State.BLOCKED,
            note="refused",
            proposal=retry,
            chain=ChainResult.of(
                [Verdict(guard="IdempotencyGuard", decision=Decision.BLOCK, reason="dupe")]
            ),
        )
        scene = FakeScene(log=(blocked,))
        assert judge(
            scene,
            attack_id="T50",
            evidence=(
                ReceiptWithOutcome("pay_ref", Outcome.BLOCKED, guard="IdempotencyGuard"),
            ),
        ).survived
        assert not judge(
            scene,
            attack_id="T51",
            evidence=(ReceiptWithOutcome("pay_ref", Outcome.BLOCKED, guard="ConsentGuard"),),
        ).survived

    def test_no_execution_after_a_mark(self):
        retry = proposal_for("gateway_technical_error", entity_id="pay_after")
        late = NOON + timedelta(hours=2)
        scene = FakeScene(
            log=(executing(retry, at=late),),
            calls=(dispatch_of(retry, at=late),),
            marks={"money_arrived": NOON + timedelta(hours=1)},
        )
        assert not judge(
            scene,
            attack_id="T52",
            evidence=(NoExecutionOnEntityAfter("pay_after", mark="money_arrived"),),
        ).survived
