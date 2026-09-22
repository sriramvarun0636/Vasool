"""What the reconciliation may conclude, and what it must refuse to.

docs/EVALUATION.md §10, 2026-09-21. A01 is the last open attack: an out-of-band
payment carries no join key, so the agent keeps chasing money the merchant
already has — measured at 34,666 actions after the money arrived, across the
evaluated cohort.

The fix is deliberately weak, and these tests are mostly about the weakness
being real. An amount match from the same customer inside a window is evidence
that the episode should *stop*; it is not evidence that this episode was paid.
The two ways to be wrong are different sizes — chasing paid money can collect
it twice, stopping wrongly costs a delayed recovery — so the rule takes the
cheap error, and a test here would fail if someone later made it take the
expensive one by marking the episode recovered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from vasool.actions.reconcile import (
    DEFAULT_WINDOW,
    Candidate,
    NullSettlementLookup,
    reconcile,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
FAILED_AT = NOW - timedelta(days=2)
AMOUNT = 149_900


class _Lookup:
    """A rail that answers with exactly these payments."""

    def __init__(self, *candidates: Candidate) -> None:
        self._candidates = candidates
        self.asked: list[tuple[str, datetime]] = []

    def captured_since(self, *, customer_id: str, since: datetime):
        self.asked.append((customer_id, since))
        return self._candidates


def _reconcile(lookup, *, ours=frozenset(), amount=AMOUNT, now=NOW):
    return reconcile(
        lookup,
        customer_id="cust_1",
        amount_paise=amount,
        failed_at=FAILED_AT,
        now=now,
        ours=ours,
    )


class TestNothingIsWired:
    def test_the_null_lookup_halts_nothing(self):
        """Production behaves exactly as before until a merchant wires a real
        lookup. Reconciliation reads a merchant's whole payment stream, which
        is a decision they take rather than a default they inherit."""
        result = _reconcile(NullSettlementLookup())
        assert result.should_halt is False and result.candidate is None


class TestWhatCountsAsEvidence:
    def test_a_matching_payment_halts_the_episode(self):
        paid = Candidate("pay_elsewhere", AMOUNT, NOW - timedelta(days=1))
        result = _reconcile(_Lookup(paid))
        assert result.should_halt and result.candidate == paid

    def test_our_own_payments_are_never_the_evidence(self):
        """The agent's own successful retry appears on the same account. Read
        back as out-of-band, it would halt the episode that had just succeeded
        and report the reason wrongly."""
        ours = Candidate("pay_ours", AMOUNT, NOW - timedelta(hours=1))
        result = _reconcile(_Lookup(ours), ours=frozenset({"pay_ours"}))
        assert result.should_halt is False

    def test_a_different_amount_is_not_this_episode(self):
        """Exact, in paise. A tolerance would let a differently-priced purchase
        stand in as evidence, and the evidence is weak enough already."""
        other = Candidate("pay_other", AMOUNT + 1, NOW - timedelta(hours=2))
        assert _reconcile(_Lookup(other)).should_halt is False

    def test_a_payment_older_than_the_window_is_not_this_episode(self):
        stale = Candidate("pay_old", AMOUNT, FAILED_AT - DEFAULT_WINDOW - timedelta(days=1))
        assert _reconcile(_Lookup(stale)).should_halt is False

    def test_the_window_opens_before_the_failure_not_after(self):
        """A customer who pays another way often does it within minutes of the
        charge failing — sometimes before the webhook arrives. A window that
        started at the failure would miss exactly those."""
        lookup = _Lookup()
        _reconcile(lookup)
        _, since = lookup.asked[0]
        assert since == FAILED_AT - DEFAULT_WINDOW

    def test_the_first_match_is_the_one_reported(self):
        first = Candidate("pay_a", AMOUNT, NOW - timedelta(days=1))
        second = Candidate("pay_b", AMOUNT, NOW - timedelta(hours=1))
        assert _reconcile(_Lookup(first, second)).candidate == first


class TestWhatItRefusesToConclude:
    def test_it_never_says_the_episode_was_recovered(self):
        """The whole design. `Reconciliation` exposes `should_halt` and a
        detail string, and no attribute anywhere on it says money arrived —
        because an amount match is not a join key, and a second purchase at the
        same price collides with it exactly."""
        result = _reconcile(_Lookup(Candidate("pay_x", AMOUNT, NOW)))
        assert not any(
            "recover" in name.lower() for name in dir(result) if not name.startswith("_")
        )
        assert "not recorded as recovered" in result.detail

    def test_the_detail_names_the_payment_and_the_reason_to_stop(self):
        """A receipt that said only "halted" would leave an operator to guess.
        The clause a person acts on has to name the evidence."""
        paid = Candidate("pay_evidence", AMOUNT, NOW - timedelta(days=1))
        detail = _reconcile(_Lookup(paid)).detail
        assert "pay_evidence" in detail
        assert "a person decides" in detail

    def test_finding_nothing_says_so_rather_than_saying_nothing(self):
        assert "no payment" in _reconcile(_Lookup()).detail
