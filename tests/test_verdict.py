"""A guard's ruling, and how thirteen of them resolve into one decision.

The coupling that matters here is DEFER <-> defer_until. docs/taxonomy.md's
defer-vs-block rule is that an action may only be deferred when we can name the
instant the blocking condition expires — so a DEFER without a concrete
defer_until is exactly the bug that produces an action deferred forever. That is
enforced by a validator, not by convention, and these tests are what hold it.

Time is pinned throughout. Nothing here depends on when the suite runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vasool.policy.verdict import (
    SEVERITY,
    ChainResult,
    Decision,
    Obligation,
    ObligationKind,
    Verdict,
    resolve,
)

NOW = datetime(2026, 8, 25, 4, 30, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=3)

decisions = st.sampled_from(list(Decision))


def allow(guard: str = "G") -> Verdict:
    return Verdict(guard=guard, decision=Decision.ALLOW)


def defer(until: datetime = LATER, guard: str = "G") -> Verdict:
    return Verdict(guard=guard, decision=Decision.DEFER, reason="r", defer_until=until)


def block(guard: str = "G") -> Verdict:
    return Verdict(guard=guard, decision=Decision.BLOCK, reason="r")


def escalate(guard: str = "G") -> Verdict:
    return Verdict(guard=guard, decision=Decision.ESCALATE, reason="r")


def na(guard: str = "G") -> Verdict:
    return Verdict(guard=guard, decision=Decision.NOT_APPLICABLE)


# ---------------------------------------------------------------------------
# the DEFER <-> defer_until coupling
# ---------------------------------------------------------------------------
class TestDeferCoupling:
    def test_defer_requires_a_defer_until(self):
        """The whole anti-forever-deferral argument rests on this. A guard that
        cannot name when the condition expires must block instead."""
        with pytest.raises(ValidationError, match="defer_until"):
            Verdict(guard="G", decision=Decision.DEFER, reason="r")

    @pytest.mark.parametrize(
        "decision", [Decision.ALLOW, Decision.BLOCK, Decision.ESCALATE, Decision.NOT_APPLICABLE]
    )
    def test_only_defer_may_carry_a_defer_until(self, decision):
        with pytest.raises(ValidationError, match="defer_until"):
            Verdict(guard="G", decision=decision, reason="r", defer_until=LATER)

    def test_defer_until_must_be_timezone_aware(self):
        """A naive datetime here would be silently interpreted as local time by
        the scheduler, which is how a deferral lands in the wrong half-day."""
        with pytest.raises(ValidationError, match="aware"):
            Verdict(
                guard="G",
                decision=Decision.DEFER,
                reason="r",
                defer_until=datetime(2026, 8, 25, 8, 0),
            )

    def test_a_well_formed_defer_is_accepted(self):
        v = defer()
        assert v.decision is Decision.DEFER
        assert v.defer_until == LATER


class TestReasonRequired:
    @pytest.mark.parametrize("decision", [Decision.BLOCK, Decision.ESCALATE])
    def test_a_non_allowing_verdict_must_say_why(self, decision):
        """The reason string is what lands in the receipt. A block with no
        reason is an audit trail that records the what and not the why."""
        with pytest.raises(ValidationError, match="reason"):
            Verdict(guard="G", decision=decision)

    @pytest.mark.parametrize("decision", [Decision.ALLOW, Decision.NOT_APPLICABLE])
    def test_a_passing_verdict_needs_no_reason(self, decision):
        assert Verdict(guard="G", decision=decision).reason is None


class TestImmutability:
    def test_a_verdict_cannot_be_edited_after_the_fact(self):
        v = allow()
        with pytest.raises(ValidationError):
            v.guard = "H"


# ---------------------------------------------------------------------------
# severity
# ---------------------------------------------------------------------------
class TestSeverity:
    def test_the_ordering_is_block_escalate_defer_allow_na(self):
        assert (
            SEVERITY[Decision.BLOCK]
            > SEVERITY[Decision.ESCALATE]
            > SEVERITY[Decision.DEFER]
            > SEVERITY[Decision.ALLOW]
            > SEVERITY[Decision.NOT_APPLICABLE]
        )

    def test_every_decision_has_a_severity(self):
        assert set(SEVERITY) == set(Decision)

    def test_resolve_of_nothing_is_allow(self):
        """An empty chain permits. Only meaningful in tests — the real registry
        is never empty — but leaving it undefined invites a None downstream."""
        assert resolve([]) is Decision.ALLOW

    def test_block_beats_everything(self):
        assert resolve([Decision.ALLOW, Decision.DEFER, Decision.BLOCK, Decision.ESCALATE]) is (
            Decision.BLOCK
        )

    def test_escalate_beats_defer(self):
        """The case the spec's chain order gets wrong: an action that will be
        escalated should not first be scheduled."""
        assert resolve([Decision.DEFER, Decision.ESCALATE]) is Decision.ESCALATE

    def test_not_applicable_never_wins_over_allow(self):
        assert resolve([Decision.NOT_APPLICABLE, Decision.ALLOW]) is Decision.ALLOW

    def test_all_not_applicable_stays_not_applicable(self):
        assert resolve([Decision.NOT_APPLICABLE]) is Decision.NOT_APPLICABLE

    @given(st.lists(decisions, min_size=1))
    def test_resolve_is_order_independent(self, ds):
        """The property that makes registry ordering a presentation detail."""
        assert resolve(ds) is resolve(list(reversed(ds)))

    @given(st.lists(decisions, min_size=1))
    def test_resolve_returns_a_decision_that_was_present(self, ds):
        assert resolve(ds) in ds


# ---------------------------------------------------------------------------
# ChainResult
# ---------------------------------------------------------------------------
class TestChainResult:
    def test_it_records_every_verdict_not_just_the_deciding_one(self):
        """The reason we evaluate all thirteen rather than short-circuiting: a
        receipt that names four violated clauses is worth more than one naming
        whichever guard happened to run first."""
        r = ChainResult.of((allow("A"), block("B"), block("C")))
        assert r.decision is Decision.BLOCK
        assert len(r.verdicts) == 3
        assert [v.guard for v in r.blocking()] == ["B", "C"]

    def test_a_defer_takes_the_latest_of_the_deferrals(self):
        """Satisfying the earliest deferral would wake the action into a guard
        that is still going to defer it, burning deferral budget for nothing."""
        soon, late = NOW + timedelta(hours=1), NOW + timedelta(days=2)
        r = ChainResult.of((defer(soon, "A"), defer(late, "B"), allow("C")))
        assert r.decision is Decision.DEFER
        assert r.defer_until == late

    def test_a_block_alongside_a_defer_yields_no_defer_until(self):
        """The bug in the spec's ordering, stated as an assertion: an action
        that is going to be blocked is never scheduled."""
        r = ChainResult.of((defer(LATER, "A"), block("B")))
        assert r.decision is Decision.BLOCK
        assert r.defer_until is None

    def test_obligations_from_every_guard_are_collected(self):
        ob = Obligation(
            kind=ObligationKind.SEND_PRE_DEBIT_NOTICE, not_before=NOW, reason="24h notice"
        )
        v = Verdict(
            guard="P",
            decision=Decision.DEFER,
            reason="r",
            defer_until=LATER,
            obligations=(ob,),
        )
        assert ChainResult.of((allow("A"), v)).obligations == (ob,)

    def test_all_allow_is_allow(self):
        assert ChainResult.of((allow("A"), allow("B"), na("C"))).decision is Decision.ALLOW

    def test_statutes_lists_only_the_clauses_actually_violated(self):
        """What the report card prints. A guard that passed did not 'apply' a
        statute to this action, and saying so would overstate the enforcement."""
        passed = Verdict(guard="A", decision=Decision.ALLOW, statute="RBI FPC")
        failed = Verdict(guard="B", decision=Decision.BLOCK, reason="r", statute="DPDP s.6")
        assert ChainResult.of((passed, failed)).statutes() == ("DPDP s.6",)
