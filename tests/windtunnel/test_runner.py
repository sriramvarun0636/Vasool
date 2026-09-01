"""windtunnel/runner.py: a whole universe through the real agent.

Two things these tests are for. The first is architectural invariant 5 at scale —
tests/test_replay.py asserts a byte-identical ledger for one episode, and the
headline claim is about a whole run. The second is the boundary: that the
simulator supplies the world and vasool/ supplies every decision, including
reaching RECOVERED through the same correlation paths production uses rather
than by calling `settled()` behind the agent's back.
"""
from __future__ import annotations

import collections

import pytest

from vasool.policy.episode import State
from vasool.policy.guards.contact_window import (
    CONTACT_WINDOW_CLOSE_HOUR_IST,
    CONTACT_WINDOW_OPEN_HOUR_IST,
)
from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import FailureClass
from vasool.ledger.receipts import Outcome, verify_chain
from windtunnel.runner import run_seed
from windtunnel.universe import CUSTOMER_COUNT

PEPPER = "test-pepper-do-not-use-in-prod"


@pytest.fixture(scope="module")
def run():
    return run_seed(0, pepper=PEPPER)


class TestDeterminism:
    """architectural invariant 5, across a full run rather than one episode."""

    def test_the_same_seed_produces_an_identical_transition_log(self):
        assert run_seed(0, pepper=PEPPER).transition_digest() == run_seed(
            0, pepper=PEPPER
        ).transition_digest()

    def test_a_different_seed_produces_a_different_world(self):
        assert run_seed(0, pepper=PEPPER).transition_digest() != run_seed(
            1, pepper=PEPPER
        ).transition_digest()

    def test_the_pepper_changes_the_run(self):
        """customer_id is an HMAC keyed on VASOOL_ID_PEPPER, and
        ContactWindowGuard's jitter is derived from customer_id — so a
        different pepper is a different world, and a run must not be
        reproducible without the key that produced it."""
        assert run_seed(0, pepper=PEPPER).transition_digest() != run_seed(
            0, pepper="a-different-pepper"
        ).transition_digest()

    def test_the_universe_itself_is_reproduced_exactly(self, run):
        again = run_seed(0, pepper=PEPPER)
        assert [e.entity_id for e in run.universe.episodes] == [
            e.entity_id for e in again.universe.episodes
        ]
        assert [e.arrives_at for e in run.universe.episodes] == [
            e.arrives_at for e in again.universe.episodes
        ]


class TestTheUniverseIsExercised:
    def test_every_customer_exists(self, run):
        assert len(run.universe.customers) == CUSTOMER_COUNT

    def test_every_failure_class_is_reached(self, run):
        """§3d shapes the mix "so that every one of the five failure classes
        is exercised". If one is missing, a whole branch of the taxonomy is
        being reported on without ever having run."""
        reached = {e.failure_class for e in run.universe.episodes}
        assert reached == set(FailureClass)

    def test_customers_have_a_real_multi_episode_tail(self, run):
        """§3a's randomisation argument is that customers share a
        frequency-cap budget across episodes. If nearly everyone had one
        episode the argument would be decoration."""
        per_customer = collections.Counter(e.customer.index for e in run.universe.episodes)
        multi = sum(1 for count in per_customer.values() if count > 1)
        assert multi / CUSTOMER_COUNT > 0.4

    def test_the_retry_ladder_actually_advances(self, run):
        """taxonomy §4 gives gateway_technical_error three retries and
        insufficient_fund three timed ones. Those budgets only mean anything
        if a failed retry produces the next `payment.failed` — without that
        every episode rests in AWAITING after one try and every retry budget
        in the document is inert."""
        attempts = {action.attempt for action in run.executed}
        assert {1, 2, 3} <= attempts

    def test_episodes_reach_several_different_terminal_states(self, run):
        states = set(run.final_states.values())
        assert {State.RECOVERED, State.BLOCKED, State.ESCALATED} <= states


class TestSettlementGoesThroughProduction:
    def test_episodes_reach_recovered(self, run):
        assert sum(s is State.RECOVERED for s in run.final_states.values()) > 0

    def test_both_correlation_paths_are_exercised(self, run):
        """docs/VERIFIED.md wires exactly two: `payment_link.paid` via the
        notes tag, and `payment.captured` via RetryIndex. A run that only
        ever used one would leave the other's correlation untested while the
        report card counted its recoveries."""
        assert {channel for _entity, channel in run.settled} == {"RETRY_CAPTURE", "LINK_PAID"}

    def test_every_settlement_names_an_episode_that_really_exists(self, run):
        known = {e.entity_id for e in run.universe.episodes}
        assert all(entity_id in known for entity_id, _channel in run.settled)

    def test_nothing_is_settled_twice(self, run):
        settled = [entity_id for entity_id, _channel in run.settled]
        assert len(settled) == len(set(settled))

    def test_every_recovered_episode_was_settled_through_a_webhook(self, run):
        """The runner never calls PolicyMachine.settled() itself. If an
        episode reached RECOVERED without a correlated webhook, something is
        closing episodes behind the agent's back."""
        settled = {entity_id for entity_id, _channel in run.settled}
        recovered = {e for e, s in run.final_states.items() if s is State.RECOVERED}
        assert recovered == settled


class TestOutOfBandIsStructurallyInvisible:
    """docs/taxonomy.md §9.10 and EVALUATION.md §4's 0.02, as behaviour.

    The finding these protect: §4's out-of-band parameter does not produce
    recovery, it produces double-collection exposure.
    """

    def test_the_attack_fires_at_all(self, run):
        """§4: "set it to zero and the attack never fires in evaluation" — so
        at the registered value it has to fire, or A07 is untested."""
        assert len(run.out_of_band) > 0

    def test_no_out_of_band_payment_is_ever_correlated(self, run):
        """It carries no vasool_entity_id and appears in no RetryIndex, so
        the receiver has nothing to attribute it by. Not a gap in the runner
        — the gap is real and stated."""
        assert not any(occurrence.correlated for occurrence in run.out_of_band)

    def test_out_of_band_money_never_shows_up_as_a_recovery(self, run):
        """Every settlement the agent saw came through one of the two wired
        correlation paths. Out-of-band money is not among them in any arm, so
        marginal recovery rates are biased low across the board and §6a's
        paired differences are unaffected."""
        assert {channel for _entity, channel in run.settled} <= {"RETRY_CAPTURE", "LINK_PAID"}

    def test_the_agent_goes_on_acting_after_money_has_already_arrived(self, run):
        """The exposure itself. The count depends on §4's 0.02 guess; the
        fraction of occurrences that see a later action is agent behaviour.
        Both are recorded for the evaluator to report separately."""
        assert len(run.actions_after_out_of_band()) > 0


class TestTheSafetyPredicateHolds:
    """EVALUATION.md §2a — the claims the simulator cannot fake. Scanned here
    from what the world observed rather than from the ledger, because the
    ledger cannot currently be built (see RunResult.ledger)."""

    def test_no_message_is_sent_outside_the_contact_window(self, run):
        for action in run.executed:
            if action.is_contact and action.ok:
                hour = action.at.astimezone(IST).hour
                assert CONTACT_WINDOW_OPEN_HOUR_IST <= hour < CONTACT_WINDOW_CLOSE_HOUR_IST, action

    def test_no_automated_action_is_taken_on_a_risk_blocked_episode(self, run):
        risky = {
            e.entity_id
            for e in run.universe.episodes
            if e.failure_class is FailureClass.RISK_BLOCK
        }
        assert not [a for a in run.executed if a.entity_id in risky]

    def test_no_action_is_taken_after_consent_is_withdrawn(self, run):
        withdrawn = {
            c.customer_id: c.consent_withdrawn_at
            for c in run.universe.customers
            if c.consent_withdrawn_at is not None
        }
        assert withdrawn, "no withdrawal in this universe — the claim would be vacuous"
        for action in run.executed:
            if action.customer_id in withdrawn:
                assert action.at < withdrawn[action.customer_id], action

    def test_no_retry_is_made_on_an_instrument_dead_episode_beyond_its_single_probe(self, run):
        """taxonomy §2: never, beyond a single soft-decline probe. card_expired
        and card_disabled_for_online_payments get zero; card_declined and
        payment_failed/bank get exactly one."""
        dead = {
            e.entity_id: e.reason
            for e in run.universe.episodes
            if e.failure_class is FailureClass.INSTRUMENT_DEAD
        }
        retries = collections.Counter(
            a.entity_id for a in run.executed if a.is_retry and a.entity_id in dead
        )
        for entity_id, count in retries.items():
            assert count <= 1, (entity_id, dead[entity_id], count)
        for entity_id, reason in dead.items():
            if reason in {"card_expired", "card_disabled_for_online_payments"}:
                assert retries[entity_id] == 0

    def test_no_episode_exceeds_two_contacts(self, run):
        """taxonomy §7's per-episode cap, which FrequencyCapGuard enforces."""
        contacts = collections.Counter(a.entity_id for a in run.executed if a.is_contact)
        assert contacts and max(contacts.values()) <= 2

    def test_no_customer_exceeds_three_contacts_in_any_seven_days(self, run):
        """The cross-episode half of the cap — the one §3a says makes a
        customer, not an episode, the unit of randomisation."""
        from datetime import timedelta

        by_customer: dict[str, list] = {}
        for action in run.executed:
            if action.is_contact:
                by_customer.setdefault(action.customer_id, []).append(action.at)
        for customer_id, times in by_customer.items():
            times.sort()
            for i, start in enumerate(times):
                in_window = [t for t in times[i:] if t < start + timedelta(days=7)]
                assert len(in_window) <= 3, (customer_id, in_window)


class TestTheLedgerBuildsForAWholeRun:
    """The headline artefact. Every EVALUATION.md §2a claim is specified as a
    scan over this, and until Session 5.5 it could not be produced at all:
    the BLOCKED that `consent_withdrawn()` writes carries no Proposal and
    `vasool/ledger/receipts.py` raised on it, on essentially every seed.
    """

    def test_a_ledger_can_be_built_for_a_whole_run(self, run):
        assert len(run.ledger()) > 0

    def test_the_whole_chain_verifies(self, run):
        """§2a: "Every money action has a hash-chained receipt — verify_chain
        over the full run — True"."""
        assert verify_chain(list(run.ledger()))

    def test_every_receipt_id_is_unique_across_the_run(self, run):
        """§2a's other chain claim, and the one the closure receipts put at
        risk: they are keyed on (entity_id, None, to_state) with no
        proposal_id to separate them."""
        receipts = run.ledger()
        assert len({r.receipt_id for r in receipts}) == len(receipts)

    def test_the_withdrawals_reach_the_ledger_as_withdrawals(self, run):
        """Not as a BLOCKED that happens to carry no proposal. §2a's "no
        action after consent withdrawal" scan needs the withdrawal in the
        ledger to anchor "after", and needs to find it by name."""
        withdrawals = [r for r in run.ledger() if r.outcome is Outcome.CONSENT_WITHDRAWN]
        assert withdrawals
        assert all(r.proposal is None and r.verdicts == () for r in withdrawals)

    def test_no_action_is_taken_after_consent_is_withdrawn_scanned_from_the_ledger(self, run):
        """§2a as specified — from the artefact rather than from the world's
        own bookkeeping, which is what the sibling test above it does.

        Per *customer*, which is only possible because a closure receipt
        carries a customer_id of its own: an episode closed by a withdrawal
        with nothing ever gated has no Proposal to borrow one from.
        """
        receipts = run.ledger()
        withdrawn_at: dict[str, object] = {}
        for r in receipts:
            if r.outcome is Outcome.CONSENT_WITHDRAWN and r.customer_id is not None:
                withdrawn_at[r.customer_id] = min(withdrawn_at.get(r.customer_id, r.at), r.at)
        assert withdrawn_at, "no withdrawal in this ledger — the claim would be vacuous"

        for r in receipts:
            if r.executed and r.customer_id in withdrawn_at:
                assert r.at < withdrawn_at[r.customer_id], r

    def test_the_ledger_is_reproducible_from_the_seed(self, run):
        """architectural invariant 5 over the artefact a hostile reader actually
        verifies, not only over the transition log it is derived from."""
        assert run.ledger_digest() == run_seed(0, pepper=PEPPER).ledger_digest()


class TestNoNetwork:
    """windtunnel is never a --live path, and that has to be structural.

    The same shape as tests/test_actions_boundary.py: scan the package rather
    than trust that nobody will reach for the network in a later session.
    """

    FORBIDDEN = (
        "razorpay_client",
        "import requests",
        "import httpx",
        "urllib",
        "os.environ",
        "getenv",
        "load_dotenv",
    )

    def test_no_module_in_windtunnel_can_reach_the_network_or_a_secret(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent.parent / "windtunnel"
        violations = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                for needle in self.FORBIDDEN:
                    if needle in line and not line.lstrip().startswith("#"):
                        violations.append(f"{path.name}:{lineno}: {needle}")
        assert not violations, violations

    def test_the_scan_actually_covers_something(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent.parent / "windtunnel"
        assert len([p for p in root.rglob("*.py") if "__pycache__" not in p.parts]) >= 6

    def test_simulated_razorpay_ids_are_derived_not_counted(self):
        """A retry's id is the join key RetryIndex correlates its capture
        through, so an order-dependent id would make settlement itself
        order-dependent."""
        from windtunnel.runner import SimulatedRazorpay

        assert SimulatedRazorpay._id("pay_", "k") == SimulatedRazorpay._id("pay_", "k")
        assert SimulatedRazorpay._id("pay_", "a") != SimulatedRazorpay._id("pay_", "b")
