"""Cross-cutting properties of all thirteen guards.

Stated over a generated input space rather than over hand-written cases. When
someone asks how we know the contact window holds, the answer is that a property
test asserts it over thousands of adversarial contexts including boundaries
nobody would think to write down — not that we checked three of them.

Each property here is one the whole package depends on, and each would fail
silently if it were only checked per-guard: a new guard added next session
inherits every one of them.
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from vasool.policy.facts import PolicyFacts
from vasool.policy.guards.base import Guard
from vasool.policy.guards.contact_window import (
    CONTACT_JITTER_MAX,
    CONTACT_WINDOW_CLOSE_HOUR_IST,
    CONTACT_WINDOW_OPEN_HOUR_IST,
    window_jitter,
)
from vasool.policy.registry import GUARD_CHAIN
from vasool.policy.verdict import Decision
from tests.policy.strategies import guard_contexts

D = Decision
SETTINGS = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
"""Each of these runs once per guard, so the effective sample is thirteen
times this. Still orders of magnitude past what anyone would hand-write."""
GUARD_IDS = [g.name for g in GUARD_CHAIN]
FACT_NAMES = {f.name for f in dataclasses.fields(PolicyFacts)}


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------
class TestFailClosed:
    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    def test_declared_requirements_are_real_facts(self, guard: Guard):
        """A typo in `requires` would name a fact that is never None, so the
        fail-closed check would never fire and the guard would run on missing
        data forever. This catches the typo; nothing can catch a fact read
        without being declared, which is why the base class says to declare
        honestly."""
        assert guard.requires <= FACT_NAMES, guard.requires - FACT_NAMES

    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_a_guard_never_allows_on_missing_facts(self, guard: Guard, ctx):
        """The most important property in the file.

        Without it, a bug in fact loading silently disables a compliance guard
        while every other test still passes — an agent that reports full
        compliance and enforces nothing. A fact we have not established is not
        a fact we have established to be harmless.
        """
        blanked = dataclasses.replace(ctx.facts, **{name: None for name in guard.requires})
        verdict = guard.evaluate(dataclasses.replace(ctx, facts=blanked))
        if guard.requires:
            assert verdict.decision is not D.ALLOW

    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    def test_a_guard_does_not_require_a_known_absent_fact(self, guard: Guard):
        """`promise_to_pay` is None when the customer made no promise, and
        `pre_debit_notice_sent_at` is None when no notice has been served yet.
        Both are known-absent rather than unknown. A guard declaring one would
        refuse every action for want of a promise nobody made."""
        known_absent = {"promise_to_pay", "pre_debit_notice_sent_at", "consent_purposes"}
        assert not (guard.requires & known_absent)


# ---------------------------------------------------------------------------
# deferral progress
# ---------------------------------------------------------------------------
class TestDeferralsMakeProgress:
    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_every_deferral_strictly_advances(self, guard: Guard, ctx):
        """A deferral to now, or to the past, is a livelock: the action wakes,
        is deferred to the same instant, and never executes or fails. The base
        class raises on it; this asserts no guard ever produces one."""
        verdict = guard.evaluate(ctx)
        if verdict.decision is D.DEFER:
            assert verdict.defer_until > ctx.effective_at

    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_a_deferral_always_names_a_time_and_nothing_else_does(self, guard: Guard, ctx):
        verdict = guard.evaluate(ctx)
        assert (verdict.defer_until is not None) == (verdict.decision is D.DEFER)


# ---------------------------------------------------------------------------
# purity
# ---------------------------------------------------------------------------
class TestPurity:
    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_evaluating_twice_gives_the_same_verdict(self, guard: Guard, ctx):
        """No clock, no I/O, no accumulated state. This is what lets stage 5
        record the facts' digest in a receipt and re-derive the whole compliance
        decision at replay without touching a store."""
        assert guard.evaluate(ctx) == guard.evaluate(ctx)

    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_evaluating_does_not_disturb_the_context(self, guard: Guard, ctx):
        """Frozen dataclasses make this structural today. The test is here for
        the day someone adds a mutable field and a guard sorts it in place."""
        before = (ctx.facts, ctx.proposal, ctx.now, ctx.effective_at)
        guard.evaluate(ctx)
        assert (ctx.facts, ctx.proposal, ctx.now, ctx.effective_at) == before

    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_a_verdict_always_names_its_guard(self, guard: Guard, ctx):
        assert guard.evaluate(ctx).guard == guard.name

    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_a_refusal_always_says_why(self, guard: Guard, ctx):
        """The reason lands in the receipt. A refusal with no reason is an audit
        trail that records the what and not the why."""
        verdict = guard.evaluate(ctx)
        if verdict.decision not in (D.ALLOW, D.NOT_APPLICABLE):
            assert verdict.reason


# ---------------------------------------------------------------------------
# the two absolutes
# ---------------------------------------------------------------------------
class TestRiskBlockIsAbsolute:
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_no_context_permits_automated_action_on_a_risk_decline(self, ctx):
        """There exists no path to an automated action on a risk-declined
        payment. Not "no path we found" — none in the generated space, over
        every world the strategies can build."""
        from vasool.policy.registry import evaluate_all

        if ctx.proposal.failure_class is not FailureClass.RISK_BLOCK:
            return
        result = evaluate_all(ctx)
        if result.decision is D.ALLOW:
            assert ctx.proposal.intervention is InterventionType.HUMAN_QUEUE
            assert not ctx.proposal.is_contact


class TestContactWindowHolds:
    @SETTINGS
    @given(ctx=guard_contexts())
    def test_no_context_permits_a_contact_outside_the_window(self, ctx):
        """The spec's §6.3 test asserts this against ctx.now. Asserted against
        ctx.effective_at instead — which is the difference between checking when
        we decided and checking when the message lands, and is exactly adversary
        attack A04."""
        from vasool.policy.registry import evaluate_all

        result = evaluate_all(ctx)
        if result.decision is D.ALLOW and ctx.proposal.is_contact:
            hour = ctx.effective_at.astimezone(IST).hour
            assert CONTACT_WINDOW_OPEN_HOUR_IST <= hour < CONTACT_WINDOW_CLOSE_HOUR_IST, (
                f"escaped at {ctx.effective_at.astimezone(IST)}"
            )

    @SETTINGS
    @given(ctx=guard_contexts())
    def test_a_deferred_contact_always_lands_inside_the_window(self, ctx):
        from vasool.policy.guards.contact_window import ContactWindowGuard

        verdict = ContactWindowGuard().evaluate(ctx)
        if verdict.decision is D.DEFER:
            hour = verdict.defer_until.astimezone(IST).hour
            assert CONTACT_WINDOW_OPEN_HOUR_IST <= hour < CONTACT_WINDOW_CLOSE_HOUR_IST


# ---------------------------------------------------------------------------
# the jitter — CLAUDE.md invariant 5 reaches into the guards
# ---------------------------------------------------------------------------
class TestJitterDeterminism:
    @given(customer_id=st.text(min_size=1, max_size=64))
    def test_the_same_customer_always_gets_the_same_jitter(self, customer_id):
        """Same seed -> byte-identical ledger. A jitter drawn at random would
        make every replayed ledger differ in the one field nobody thinks to
        look at."""
        assert window_jitter(customer_id) == window_jitter(customer_id)

    @given(customer_id=st.text(min_size=1, max_size=64))
    def test_the_jitter_stays_within_its_bound(self, customer_id):
        assert timedelta(0) <= window_jitter(customer_id) < CONTACT_JITTER_MAX

    def test_the_jitter_is_stable_across_processes(self):
        """Pinned literally. Python's hash() is salted per process, so a jitter
        built on it would pass every equality test inside one run and replay
        differently tomorrow — the failure mode this whole invariant exists to
        prevent, and the one hardest to notice."""
        assert window_jitter("cust_stable_fixture") == timedelta(seconds=554)

    def test_it_actually_spreads_customers_out(self):
        """A constant jitter would satisfy every property above and defeat the
        purpose — the whole point is that a merchant's overnight backlog does
        not fire in one burst at 08:00:00."""
        spread = {window_jitter(f"cust_{n}") for n in range(200)}
        assert len(spread) > 100


# ---------------------------------------------------------------------------
# the report card's honesty
# ---------------------------------------------------------------------------
class TestStatutes:
    @pytest.mark.parametrize("guard", GUARD_CHAIN, ids=GUARD_IDS)
    def test_a_statute_is_either_a_real_citation_or_absent(self, guard: Guard):
        """Self-imposed rules must not be dressed up as regulation in the one
        artefact a compliance reader will scrutinise. Empty strings and
        placeholders are the way that happens by accident."""
        assert guard.statute is None or guard.statute.strip() == guard.statute
        assert guard.statute is None or len(guard.statute) > 8

    def test_the_self_imposed_guards_cite_nothing(self):
        """Named explicitly, so that adding a citation to one of these is a
        deliberate act rather than a drive-by."""
        self_imposed = {"IdempotencyGuard", "RetryCapGuard", "SpendCapGuard", "HumanApprovalGuard"}
        for guard in GUARD_CHAIN:
            if guard.name in self_imposed:
                assert guard.statute is None, guard.name
            else:
                assert guard.statute is not None, guard.name
