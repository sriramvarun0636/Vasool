"""windtunnel/metrics.py: EVALUATION.md §6, computed as ledger scans.

**What these tests are actually for.** §2a's safety predicate is the claim the
submission rests on, and a scan that cannot fail is worth nothing — it reads
as evidence while proving only that it was never pointed at a violation. So
every scan below is tested twice: once against a real seed-0 ledger, where it
must pass, and once against a hand-built receipt that violates the claim,
where it must fail and say which. The second half is the load-bearing one.

The violating receipts are built through `receipt_from_transition` from real
Proposals the real classifier produced, so a receipt that trips a scan is
shaped exactly like one the agent would write — not a stub with a field poked
to an impossible value.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from vasool.diagnosis.proposal import template_ids
from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import FailureClass
from vasool.ledger.receipts import GENESIS_HASH, Outcome, Receipt, receipt_from_transition
from vasool.policy.episode import State
from vasool.policy.transitions import Closure, Transition
from vasool.policy.verdict import ChainResult
from windtunnel.metrics import (
    CLAIM_CHAIN,
    CLAIM_CONSENT,
    CLAIM_CONTACT_CAPS,
    CLAIM_CONTACT_WINDOW,
    CLAIM_DLT,
    CLAIM_INSTRUMENT_DEAD,
    CLAIM_RECEIPT_IDS,
    CLAIM_RISK_BLOCK,
    measure,
    reconcile,
    safety_report,
)
from windtunnel.runner import run_seed

from tests.policy.strategies import proposal_for

PEPPER = "test-pepper-do-not-use-in-prod"

MIDDAY_IST = datetime(2026, 9, 10, 12, 0, tzinfo=IST).astimezone(timezone.utc)
"""A moment comfortably inside the 08:00-19:00 contact window."""


@pytest.fixture(scope="module")
def run():
    return run_seed(0, pepper=PEPPER)


@pytest.fixture(scope="module")
def ledger(run):
    return run.ledger()


# ---------------------------------------------------------------------------
# helpers — real receipts, built the way the ledger builds them
# ---------------------------------------------------------------------------
def executed(proposal, at: datetime = MIDDAY_IST) -> Receipt:
    """An EXECUTED receipt for a real Proposal, at a chosen instant."""
    receipt = receipt_from_transition(
        Transition(
            at=at,
            entity_id=proposal.entity_id,
            customer_id=proposal.customer_id,
            from_state=State.GATED,
            to_state=State.EXECUTING,
            note="executed",
            proposal=proposal,
            chain=ChainResult.of([]),
        ),
        prev_hash=GENESIS_HASH,
        trace_id="trace",
    )
    assert receipt is not None and receipt.executed
    return receipt


def withdrawal(customer_id: str, entity_id: str, at: datetime) -> Receipt:
    """The closure receipt a DPDP withdrawal writes."""
    receipt = receipt_from_transition(
        Transition(
            at=at,
            entity_id=entity_id,
            customer_id=customer_id,
            from_state=State.AWAITING,
            to_state=State.BLOCKED,
            note="consent withdrawn",
            closure=Closure.CONSENT_WITHDRAWN,
        ),
        prev_hash=GENESIS_HASH,
        trace_id="trace",
    )
    assert receipt is not None and receipt.outcome is Outcome.CONSENT_WITHDRAWN
    return receipt


def claim(receipts, name: str):
    report = safety_report(receipts)
    return next(c for c in report.claims if c.name == name)


def a_contact_proposal():
    """A real contact proposal: card_expired's re-auth link."""
    return proposal_for("card_expired")


# ---------------------------------------------------------------------------
# §2a on a real run
# ---------------------------------------------------------------------------
class TestSafetyPredicateOnSeedZero:
    """The headline claim, on a ledger the agent actually wrote."""

    def test_the_predicate_holds(self, ledger):
        report = safety_report(ledger)
        assert report.holds, "§2a failed:\n" + "\n".join(
            f"  {c.name}: {c.violations} — {c.detail}" for c in report.failed()
        )

    def test_every_claim_is_reported_not_just_the_failures(self, ledger):
        """§2a is a conjunction of eight named claims and the report card
        renders all eight. A predicate that only lists failures cannot show a
        reader that a claim was checked at all."""
        assert len(safety_report(ledger).claims) == 8

    def test_the_scan_actually_saw_something(self, ledger):
        """Guards against a predicate that passes because it examined nothing
        — the same failure tests/test_no_wallclock.py protects against."""
        assert any(r.executed for r in ledger)
        assert any(r.proposal is not None and r.proposal.is_contact for r in ledger if r.executed)


# ---------------------------------------------------------------------------
# each scan, against a violation it must catch
# ---------------------------------------------------------------------------
class TestContactWindowScan:
    def test_a_contact_inside_the_window_passes(self):
        assert claim([executed(a_contact_proposal())], CLAIM_CONTACT_WINDOW).passed

    @pytest.mark.parametrize("hour", [0, 5, 7, 19, 21, 23])
    def test_a_contact_outside_08_to_19_ist_fails(self, hour):
        at = datetime(2026, 9, 10, hour, 30, tzinfo=IST).astimezone(timezone.utc)
        found = claim([executed(a_contact_proposal(), at)], CLAIM_CONTACT_WINDOW)
        assert not found.passed and found.violations == 1

    def test_a_retry_outside_the_window_is_not_a_violation(self):
        """The window governs contact, not re-presentation. A 03:00 silent
        retry disturbs nobody, and counting it would report a compliance
        breach where no rule applies (taxonomy §6: two rules, not one)."""
        at = datetime(2026, 9, 10, 3, 0, tzinfo=IST).astimezone(timezone.utc)
        retry = proposal_for("gateway_technical_error")
        assert retry.is_retry
        assert claim([executed(retry, at)], CLAIM_CONTACT_WINDOW).passed

    def test_a_blocked_contact_outside_the_window_is_not_a_violation(self):
        """§2a scans what was *sent*. A guard refusing a 21:00 SMS is the
        system working, and counting the refusal as a violation would invert
        the claim the report card makes."""
        at = datetime(2026, 9, 10, 21, 0, tzinfo=IST).astimezone(timezone.utc)
        blocked = receipt_from_transition(
            Transition(
                at=at,
                entity_id="pay_blocked",
                customer_id="cust_blocked",
                from_state=State.GATED,
                to_state=State.BLOCKED,
                note="outside window",
                proposal=a_contact_proposal(),
                chain=ChainResult.of([]),
            ),
            prev_hash=GENESIS_HASH,
            trace_id="trace",
        )
        assert claim([blocked], CLAIM_CONTACT_WINDOW).passed


class TestDLTTemplateScan:
    def test_a_registered_template_passes(self):
        proposal = a_contact_proposal()
        assert proposal.template_id in template_ids()
        assert claim([executed(proposal)], CLAIM_DLT).passed

    def test_an_unregistered_template_fails(self):
        proposal = a_contact_proposal().model_copy(update={"template_id": "NOT_REGISTERED"})
        found = claim([executed(proposal)], CLAIM_DLT)
        assert not found.passed and found.violations == 1

    def test_a_missing_template_fails(self):
        """None is what DLTTemplateGuard blocks on, so a sent message carrying
        None is the violation in its purest form."""
        proposal = a_contact_proposal().model_copy(update={"template_id": None})
        assert not claim([executed(proposal)], CLAIM_DLT).passed


class TestRiskBlockScan:
    def test_a_risk_episode_with_no_executed_action_passes(self):
        assert claim([], CLAIM_RISK_BLOCK).passed

    def test_any_executed_action_on_a_risk_episode_fails(self):
        """taxonomy §2: RISK_BLOCK gets nothing automated, ever. The scan keys
        on the classification the Proposal carries, so it catches an action on
        a risk episode whatever intervention was chosen."""
        risky = proposal_for("payment_risk_check_failed")
        assert risky.failure_class is FailureClass.RISK_BLOCK
        found = claim([executed(risky)], CLAIM_RISK_BLOCK)
        assert not found.passed and found.violations == 1

    def test_escalating_a_risk_episode_is_not_a_violation(self):
        """HUMAN_QUEUE is the correct behaviour, and it writes a receipt
        precisely so restraint is visible (taxonomy §5). Counting it would
        make the right answer look like the wrong one."""
        escalated = receipt_from_transition(
            Transition(
                at=MIDDAY_IST,
                entity_id="pay_risk",
                customer_id="cust_risk",
                from_state=State.GATED,
                to_state=State.ESCALATED,
                note="risk block",
                proposal=proposal_for("payment_risk_check_failed"),
                chain=ChainResult.of([]),
            ),
            prev_hash=GENESIS_HASH,
            trace_id="trace",
        )
        assert claim([escalated], CLAIM_RISK_BLOCK).passed


class TestConsentScan:
    def test_action_before_a_withdrawal_passes(self):
        proposal = a_contact_proposal()
        receipts = [
            executed(proposal, MIDDAY_IST),
            withdrawal(proposal.customer_id, proposal.entity_id, MIDDAY_IST + timedelta(hours=1)),
        ]
        assert claim(receipts, CLAIM_CONSENT).passed

    def test_action_after_a_withdrawal_fails(self):
        proposal = a_contact_proposal()
        receipts = [
            withdrawal(proposal.customer_id, proposal.entity_id, MIDDAY_IST),
            executed(proposal, MIDDAY_IST + timedelta(hours=1)),
        ]
        found = claim(receipts, CLAIM_CONSENT)
        assert not found.passed and found.violations == 1

    def test_the_scan_reaches_the_customers_other_episodes(self):
        """§3a: a withdrawal is a statement about a person, not a payment. The
        receipt carries customer_id for exactly this — without it a withdrawal
        in the ledger names one entity and the scan cannot reach the rest.
        """
        first, second = proposal_for("card_expired"), proposal_for("card_declined")
        assert first.entity_id == second.entity_id  # one fixture, one payment id
        other = second.model_copy(update={"entity_id": "pay_other_episode"})
        receipts = [
            withdrawal(first.customer_id, first.entity_id, MIDDAY_IST),
            executed(other, MIDDAY_IST + timedelta(hours=2)),
        ]
        assert not claim(receipts, CLAIM_CONSENT).passed


class TestInstrumentDeadScan:
    def test_a_single_probe_passes(self):
        """taxonomy §2 permits exactly one, and §2a's wording is 'beyond the
        documented single probe'."""
        probe = proposal_for("card_declined")
        assert probe.is_retry and probe.failure_class is FailureClass.INSTRUMENT_DEAD
        assert claim([executed(probe)], CLAIM_INSTRUMENT_DEAD).passed

    def test_a_second_retry_on_a_dead_instrument_fails(self):
        probe = proposal_for("card_declined")
        again = probe.model_copy(update={"attempt": 2, "proposal_id": "prop_second"})
        found = claim([executed(probe), executed(again)], CLAIM_INSTRUMENT_DEAD)
        assert not found.passed and found.violations == 1

    def test_the_cap_is_per_episode_not_per_run(self):
        """Two episodes each taking their own single probe is two legal
        probes, not a breach — the budget belongs to the payment."""
        first = proposal_for("card_declined")
        second = first.model_copy(update={"entity_id": "pay_second", "proposal_id": "prop_2"})
        assert claim([executed(first), executed(second)], CLAIM_INSTRUMENT_DEAD).passed


class TestContactCapScan:
    def test_two_contacts_in_an_episode_passes(self):
        """EPISODE_CONTACT_CAP is 2: one is a reminder, two is pressure."""
        pair = [
            executed(a_contact_proposal(), MIDDAY_IST),
            executed(
                a_contact_proposal().model_copy(update={"proposal_id": "prop_b"}),
                MIDDAY_IST + timedelta(days=1),
            ),
        ]
        assert claim(pair, CLAIM_CONTACT_CAPS).passed

    def test_three_contacts_in_one_episode_fails(self):
        receipts = [
            executed(
                a_contact_proposal().model_copy(update={"proposal_id": f"prop_{i}"}),
                MIDDAY_IST + timedelta(days=i),
            )
            for i in range(3)
        ]
        assert not claim(receipts, CLAIM_CONTACT_CAPS).passed

    def test_four_contacts_to_one_customer_in_seven_days_fails(self):
        """FREQUENCY_CAP_COUNT is 3 per rolling 7 days, across episodes — the
        coupling §3a names as the reason randomisation is at the customer."""
        receipts = [
            executed(
                a_contact_proposal().model_copy(
                    update={"entity_id": f"pay_{i}", "proposal_id": f"prop_{i}"}
                ),
                MIDDAY_IST + timedelta(days=i),
            )
            for i in range(4)
        ]
        assert not claim(receipts, CLAIM_CONTACT_CAPS).passed

    def test_four_contacts_spread_past_the_window_passes(self):
        """The cap is a rolling window, not a run total. Four contacts eight
        days apart never put four inside seven days."""
        receipts = [
            executed(
                a_contact_proposal().model_copy(
                    update={"entity_id": f"pay_{i}", "proposal_id": f"prop_{i}"}
                ),
                MIDDAY_IST + timedelta(days=8 * i),
            )
            for i in range(4)
        ]
        assert claim(receipts, CLAIM_CONTACT_CAPS).passed


class TestChainScans:
    def test_a_real_ledger_verifies(self, ledger):
        assert claim(ledger, CLAIM_CHAIN).passed
        assert claim(ledger, CLAIM_RECEIPT_IDS).passed

    def test_a_tampered_receipt_breaks_the_chain(self, ledger):
        """The tamper test §2a's 'every money action has a hash-chained
        receipt' actually asserts. Editing one field must invalidate it."""
        tampered = list(ledger)
        victim = next(i for i, r in enumerate(tampered) if r.outcome is Outcome.RECOVERED)
        tampered[victim] = tampered[victim].__class__(
            **{
                **{f: getattr(tampered[victim], f) for f in tampered[victim].__dataclass_fields__},
                "amount_recovered_paise": 999_999_99,
            }
        )
        assert not claim(tampered, CLAIM_CHAIN).passed

    def test_a_duplicated_receipt_id_fails(self, ledger):
        doubled = list(ledger) + [ledger[0]]
        assert not claim(doubled, CLAIM_RECEIPT_IDS).passed


# ---------------------------------------------------------------------------
# §6's metrics
# ---------------------------------------------------------------------------
class TestMetrics:
    def test_the_primary_metric_is_a_ledger_scan(self, run):
        """§6's primary is recovered ÷ total episodes. The numerator comes off
        the ledger; the denominator is a world fact, because an episode the
        agent never acted on leaves no trace in the ledger to be counted."""
        m = measure(run, arm="vasool")
        assert m.episodes == len(run.universe.episodes)
        assert m.recovered == len(
            {r.entity_id for r in run.ledger() if r.outcome is Outcome.RECOVERED}
        )
        assert m.recovery_rate == pytest.approx(m.recovered / m.episodes)

    def test_rupees_recovered_come_from_the_receipts(self, run):
        m = measure(run, arm="vasool")
        assert m.recovered_paise == sum(
            r.amount_recovered_paise for r in run.ledger() if r.outcome is Outcome.RECOVERED
        )
        assert m.recovered_paise > 0

    def test_secondary_metrics_are_populated(self, run):
        m = measure(run, arm="vasool")
        assert m.retries_executed > 0
        assert m.contacts_executed > 0
        assert m.attempts_per_recovery is not None and m.attempts_per_recovery > 0
        assert m.contacts_per_episode > 0
        assert m.time_to_recovery_median_hours is not None
        assert m.time_to_recovery_p90_hours >= m.time_to_recovery_median_hours

    def test_measurement_is_deterministic(self):
        """CLAUDE.md invariant 5 has to survive the evaluator, not stop at the
        runner: the same seed must produce the same numbers."""
        first = measure(run_seed(3, pepper=PEPPER), arm="vasool")
        second = measure(run_seed(3, pepper=PEPPER), arm="vasool")
        assert first == second

    def test_a_cohort_restricts_every_number(self, run):
        """§3c evaluates a subset of customers, so every metric has to be
        computable over one — including the safety predicate."""
        half = frozenset(list({c.customer_id for c in run.universe.customers})[:120])
        m = measure(run, arm="vasool", cohort="dev", customers=half)
        whole = measure(run, arm="vasool")
        assert m.cohort == "dev"
        assert 0 < m.episodes < whole.episodes
        assert m.recovered <= whole.recovered
        assert m.recovered_paise <= whole.recovered_paise

    def test_cohorts_partition_the_run(self, run):
        """Two complementary cohorts must sum to the whole. A metric that does
        not partition would mean the split is losing or double-counting
        episodes, which would corrupt every paired difference downstream."""
        ids = sorted({c.customer_id for c in run.universe.customers})
        left, right = frozenset(ids[:200]), frozenset(ids[200:])
        a = measure(run, arm="vasool", customers=left)
        b = measure(run, arm="vasool", customers=right)
        whole = measure(run, arm="vasool")
        assert a.episodes + b.episodes == whole.episodes
        assert a.recovered + b.recovered == whole.recovered
        assert a.recovered_paise + b.recovered_paise == whole.recovered_paise


class TestReconciliation:
    """§2a's claims are ledger scans *because* the ledger is the artefact a
    hostile reader verifies. That the world's own record agrees with it is a
    claim, not an assumption — so it is checked and reported, never used to
    patch a number."""

    def test_the_ledger_and_the_world_agree_on_seed_zero(self, run):
        report = measure(run, arm="vasool").reconciliation
        assert report.agrees, "ledger and world disagree:\n" + "\n".join(
            f"  {f.kind}: {f.count} — {f.detail}" for f in report.findings
        )

    def test_a_disagreement_is_surfaced_rather_than_absorbed(self, run):
        """If the world saw an action the ledger does not record, that is the
        single most important finding this harness can produce — it would mean
        the audit trail is incomplete. It must appear as a finding, and the
        metric must not quietly use the world's number instead."""
        report = reconcile(run, receipts=run.ledger(), executed=run.executed[:-5])
        assert not report.agrees
        assert any(f.kind == "receipt_without_world_action" for f in report.findings)

    def test_an_action_the_ledger_missed_is_the_loudest_finding(self, run):
        """The other direction, and the serious one: an action the world
        dispatched with no receipt behind it would mean §2a's evidence is
        incomplete."""
        phantom = run.executed[0].__class__(
            **{**{f: getattr(run.executed[0], f) for f in run.executed[0].__dataclass_fields__},
               "proposal_id": "prop_never_receipted"}
        )
        report = reconcile(run, receipts=run.ledger(), executed=[*run.executed, phantom])
        assert any(f.kind == "action_missing_from_ledger" for f in report.findings)

    def test_out_of_band_exposure_is_reported(self, run):
        """taxonomy §9.10: out-of-band money is never recovered in any arm, so
        what the parameter produces is double-collection exposure. Both the
        count and the fraction are reported, because they are different kinds
        of claim (§2)."""
        m = measure(run, arm="vasool")
        assert m.out_of_band_occurrences > 0
        assert m.actions_after_out_of_band == len(run.actions_after_out_of_band())
