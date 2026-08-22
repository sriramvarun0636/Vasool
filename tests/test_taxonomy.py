"""docs/taxonomy.md §2-§4, encoded as a test.

The point of this file is that the taxonomy can be defended row by row. So
EXPECTED_ROWS below is a transcription of the §4 markdown table, and any drift
between the document and vasool/diagnosis/taxonomy.py fails here rather than
in a demo.

Reason strings are loaded from data/observed_payloads/ and
data/stubbed_payloads/ — never typed in. CLAUDE.md: a reason in neither
directory does not exist.
"""
from __future__ import annotations

import json
import logging
import pathlib
from datetime import timedelta

import pytest

from vasool.diagnosis.taxonomy import (
    RETRY_INTERVENTIONS,
    RULES,
    SOURCE_ANY,
    UNKNOWN_REASON,
    UNMAPPED_RULE,
    FailureClass,
    InterventionType,
    known_reasons,
    lookup,
    normalise,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED = REPO_ROOT / "data" / "observed_payloads"
STUBBED = REPO_ROOT / "data" / "stubbed_payloads"

TAXONOMY_LOGGER = "vasool.diagnosis.taxonomy"


def _payment_failed_fixtures() -> list[pathlib.Path]:
    return sorted(OBSERVED.glob("payment_failed__*.json")) + sorted(
        STUBBED.glob("SIMULATED__payment_failed__*.json")
    )


def _entity(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())["body"]["payload"]["payment"]["entity"]


def fixture_reasons() -> set[str]:
    """Every error_reason that has an actual payload on disk."""
    return {_entity(p)["error_reason"] for p in _payment_failed_fixtures()}


def fixture_sources() -> set[str]:
    """Every error_source that has an actual payload on disk."""
    return {_entity(p)["error_source"] for p in _payment_failed_fixtures()}


# ---------------------------------------------------------------------------
# §4, transcribed. (reason, source) -> (class, retry budget, retry action,
# post-retry action). Timing is asserted in tests/test_rules.py.
# ---------------------------------------------------------------------------
C = FailureClass
I = InterventionType

EXPECTED_ROWS: dict[tuple[str, str], tuple[FailureClass, int, InterventionType | None, InterventionType | None]] = {
    ("payment_failed", "gateway"): (C.TRANSIENT, 1, I.SILENT_RETRY, I.REATTEMPT_LINK),
    ("payment_failed", "bank"): (C.INSTRUMENT_DEAD, 1, I.SILENT_RETRY, I.REAUTH_LINK),
    ("payment_failed", "business"): (C.RISK_BLOCK, 0, None, I.HUMAN_QUEUE),
    ("payment_failed", SOURCE_ANY): (C.TRANSIENT, 1, I.SILENT_RETRY, I.HUMAN_QUEUE),
    ("gateway_technical_error", SOURCE_ANY): (C.TRANSIENT, 3, I.SILENT_RETRY, I.REATTEMPT_LINK),
    ("payment_timed_out", SOURCE_ANY): (C.TRANSIENT, 1, I.SILENT_RETRY, I.REATTEMPT_LINK),
    ("insufficient_fund", SOURCE_ANY): (C.LIQUIDITY, 3, I.TIMED_RETRY, I.REATTEMPT_LINK),
    ("payment_cancelled", SOURCE_ANY): (C.CUSTOMER_ACTION, 0, None, I.REATTEMPT_LINK),
    ("card_declined", SOURCE_ANY): (C.INSTRUMENT_DEAD, 1, I.SILENT_RETRY, I.REAUTH_LINK),
    ("card_disabled_for_online_payments", SOURCE_ANY): (C.INSTRUMENT_DEAD, 0, None, I.REAUTH_LINK),
    ("card_number_invalid", SOURCE_ANY): (C.CUSTOMER_ACTION, 0, None, I.REATTEMPT_LINK),
    ("card_expired", SOURCE_ANY): (C.INSTRUMENT_DEAD, 0, None, I.REAUTH_LINK),
    ("payment_risk_check_failed", SOURCE_ANY): (C.RISK_BLOCK, 0, None, I.HUMAN_QUEUE),
}


class TestClosedEnums:
    """A closed enum is what stops a Session-7 LLM emitting an action that does
    not exist. If either of these grows a member, that is a taxonomy change and
    it has to be argued in docs/taxonomy.md first."""

    def test_exactly_the_five_classes_of_section_2(self):
        assert {c.value for c in FailureClass} == {
            "TRANSIENT",
            "LIQUIDITY",
            "INSTRUMENT_DEAD",
            "CUSTOMER_ACTION",
            "RISK_BLOCK",
        }

    def test_exactly_the_interventions_used_in_section_4(self):
        assert {i.value for i in InterventionType} == {
            "SILENT_RETRY",
            "TIMED_RETRY",
            "REATTEMPT_LINK",
            "REAUTH_LINK",
            "HUMAN_QUEUE",
        }

    def test_an_invented_intervention_is_rejected_at_the_boundary(self):
        with pytest.raises(ValueError):
            InterventionType("AUTO_REFUND")

    def test_an_invented_class_is_rejected_at_the_boundary(self):
        with pytest.raises(ValueError):
            FailureClass("PROBABLY_FINE")

    def test_retry_interventions_are_the_two_that_touch_the_instrument(self):
        assert RETRY_INTERVENTIONS == frozenset(
            {InterventionType.SILENT_RETRY, InterventionType.TIMED_RETRY}
        )


class TestTableMatchesTheDocument:
    def test_no_extra_or_missing_rows(self):
        assert set(RULES) == set(EXPECTED_ROWS)

    @pytest.mark.parametrize("key", sorted(EXPECTED_ROWS), ids=lambda k: f"{k[0]}|{k[1]}")
    def test_row(self, key):
        failure_class, budget, retry, post = EXPECTED_ROWS[key]
        rule = RULES[key]
        assert rule.failure_class is failure_class
        assert rule.retry_budget == budget
        assert rule.retry_intervention is retry
        assert rule.post_retry is post

    def test_unmapped_row(self):
        """§4's last row: TRANSIENT fail-safe, one silent retry, then a human."""
        assert UNMAPPED_RULE.failure_class is FailureClass.TRANSIENT
        assert UNMAPPED_RULE.retry_budget == 1
        assert UNMAPPED_RULE.retry_intervention is InterventionType.SILENT_RETRY
        assert UNMAPPED_RULE.post_retry is InterventionType.HUMAN_QUEUE
        assert UNMAPPED_RULE.retry_delays == (timedelta(minutes=30),)


class TestProvenance:
    """CLAUDE.md: never invent a Razorpay error string. Enforced mechanically,
    not by discipline."""

    def test_fixtures_are_not_silently_empty(self):
        assert len(_payment_failed_fixtures()) >= 10
        # 9 stubs + payment_failed (live). A new reason on disk must get a §4
        # row and an EXPECTED_ROWS entry; this number changing is the prompt.
        assert len(fixture_reasons()) == 10

    @pytest.mark.parametrize("reason", sorted(fixture_reasons()))
    def test_every_payload_on_disk_has_a_mapped_reason(self, reason):
        """Nothing we have actually captured may fall through to the unknown
        path — that path exists for API drift, not for our own fixtures."""
        assert reason in known_reasons()

    @pytest.mark.parametrize("reason", sorted(known_reasons()))
    def test_every_mapped_reason_has_a_payload(self, reason):
        assert reason in fixture_reasons(), (
            f"{reason!r} is mapped in taxonomy.py but has no payload in "
            "data/observed_payloads/ or data/stubbed_payloads/"
        )

    def test_unknown_sentinel_is_not_a_wire_value(self):
        assert UNKNOWN_REASON not in fixture_reasons()


class TestSourceBranching:
    """§3: the lookup is keyed on the pair, but ONLY payment_failed branches on
    source. Everything else must ignore it structurally, not by convention."""

    def test_only_payment_failed_has_source_specific_keys(self):
        branching = {reason for reason, source in RULES if source != SOURCE_ANY}
        assert branching == {"payment_failed"}

    def test_payment_failed_branches_are_the_observed_sources_plus_business(self):
        """VERIFIED.md: cards return gateway, one netbanking failure returned
        bank. Those are the only two source values ever seen on a live
        payment_failed, and both get their own row.

        `business` is the deliberate third. §5 argues it on cost asymmetry
        rather than on evidence — the value was hand-set in tools/make_stubs.py
        and has never been observed — and §9.2 records it as inferred. This test
        pins the exception at exactly one: any OTHER unobserved source growing a
        row would be a claim about evidence we do not have."""
        observed_on_payment_failed = {
            _entity(p)["error_source"] for p in sorted(OBSERVED.glob("payment_failed__*.json"))
        }
        branches = {source for reason, source in RULES if reason == "payment_failed"}
        assert observed_on_payment_failed <= branches
        assert branches == observed_on_payment_failed | {"business", SOURCE_ANY}

    def test_the_business_branch_is_not_evidence_backed(self):
        """The honesty check behind §9.2: if a live payment_failed/business
        payload is ever captured, this test fails and the §5 reasoning should be
        rewritten from cost asymmetry to evidence."""
        observed_on_payment_failed = {
            _entity(p)["error_source"] for p in sorted(OBSERVED.glob("payment_failed__*.json"))
        }
        assert "business" not in observed_on_payment_failed

    @pytest.mark.parametrize("source", sorted(fixture_sources()))
    def test_source_is_ignored_for_every_specific_reason(self, source):
        """card_expired is card_expired whichever layer reported it."""
        for reason in sorted(known_reasons() - {"payment_failed"}):
            _, rule = lookup(reason, source)
            assert rule is RULES[(reason, SOURCE_ANY)]

    def test_unfamiliar_source_on_payment_failed_falls_to_the_other_branch(self, caplog):
        # "customer" is a real observed source string — it appears on stub
        # payloads — but has never been seen on payment_failed. An unobserved
        # COMBINATION, not an invented string.
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            _, rule = lookup("payment_failed", "customer")
        assert rule is RULES[("payment_failed", SOURCE_ANY)]

    def test_unfamiliar_source_is_logged_because_it_is_operational_signal(self, caplog):
        """§5: 'Log the source value; an unfamiliar source is itself
        operational signal.'"""
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            lookup("payment_failed", "wallet_provider")
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert "wallet_provider" in records[0].getMessage()

    def test_known_source_on_payment_failed_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            lookup("payment_failed", "gateway")
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestNormalise:
    def test_singular_insufficient_fund_is_the_canonical_key(self):
        """§5: Razorpay emits the singular. The stub payload on disk is the
        authority; the plural is what the docs and the design spec write."""
        assert "insufficient_fund" in fixture_reasons()
        assert normalise("insufficient_fund") == "insufficient_fund"

    def test_plural_is_aliased_not_dropped(self, caplog):
        """The whole point: trusting the plural would route the single most
        recoverable class to the unknown path."""
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            assert normalise("insufficient_funds") == "insufficient_fund"
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_plural_resolves_to_liquidity_not_unknown(self):
        _, rule = lookup("insufficient_funds", "bank")
        assert rule.failure_class is FailureClass.LIQUIDITY
        assert rule.retry_intervention is InterventionType.TIMED_RETRY

    @pytest.mark.parametrize("reason", sorted(fixture_reasons()))
    def test_every_real_reason_survives_normalisation_unchanged(self, reason):
        assert normalise(reason) == reason

    def test_whitespace_and_case_are_normalised(self):
        assert normalise("  CARD_EXPIRED \n") == "card_expired"

    def test_unknown_reason_returns_the_sentinel(self, caplog):
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            assert normalise("a_reason_razorpay_has_never_sent") == UNKNOWN_REASON

    def test_unknown_reason_logs_at_warn_with_the_full_string(self, caplog):
        """§5: 'Log at WARN with the full reason string. The unknown bucket
        filling up is how we learn the API changed.'"""
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            normalise("some_reason_from_a_future_razorpay_release")
        records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(records) == 1
        assert records[0].levelname == "WARNING"
        assert "some_reason_from_a_future_razorpay_release" in records[0].getMessage()

    def test_unknown_reason_logs_the_string_it_was_given_not_the_folded_one(self, caplog):
        """An API change that arrives mis-cased should surface verbatim, or the
        log stops being evidence of what actually came off the wire."""
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            normalise("  SomeNewReason  ")
        message = caplog.records[0].getMessage()
        assert "SomeNewReason" in message

    def test_unknown_reason_routes_to_the_fail_safe_rule(self, caplog):
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            reason, rule = lookup("a_reason_razorpay_has_never_sent", "gateway")
        assert reason == UNKNOWN_REASON
        assert rule is UNMAPPED_RULE

    def test_unknown_reason_warns_exactly_once_per_lookup(self, caplog):
        """Double-logging turns the unknown bucket into a bad counter, and the
        bucket's size is the signal."""
        with caplog.at_level(logging.WARNING, logger=TAXONOMY_LOGGER):
            lookup("a_reason_razorpay_has_never_sent", "gateway")
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


class TestTableIsInternallyConsistent:
    """Structural invariants the policy plane will rely on. These are what let
    rules.py index retry_delays without bounds-checking."""

    @pytest.mark.parametrize("key", sorted(RULES), ids=lambda k: f"{k[0]}|{k[1]}")
    def test_rule(self, key):
        self._check(RULES[key])

    def test_unmapped_rule_is_consistent_too(self):
        self._check(UNMAPPED_RULE)

    @staticmethod
    def _check(rule):
        if rule.salary_aware:
            # Timing comes from §6, not from a fixed ladder.
            assert rule.retry_delays == ()
            assert rule.failure_class is FailureClass.LIQUIDITY
        else:
            assert len(rule.retry_delays) == rule.retry_budget

        if rule.retry_budget > 0:
            assert rule.retry_intervention in RETRY_INTERVENTIONS
        else:
            assert rule.retry_intervention is None

        # §5, on gateway_technical_error: "three retries and then nothing is a
        # broken product". Every row in §4 as it now stands names an escalation,
        # including the two that used to trail off. The Rule type still permits
        # None — a row that deliberately ends in silence is a coherent thing to
        # want — so this asserts a property of the current table, not of the
        # type. Changing it means arguing a silent terminal into the document.
        assert rule.post_retry is not None

        assert rule.retry_budget >= 0
        assert rule.post_retry_delay >= timedelta(0)
        assert rule.rationale


class TestRiskBlockIsStructurallyInert:
    """§2: 'Hard stop. Human queue. Zero outbound.' Checked at the table level
    here; checked over generated input in tests/test_rules.py."""

    def test_risk_block_never_retries(self):
        _, rule = lookup("payment_risk_check_failed", "business")
        assert rule.failure_class is FailureClass.RISK_BLOCK
        assert rule.retry_budget == 0
        assert rule.retry_intervention is None
        assert rule.post_retry is InterventionType.HUMAN_QUEUE

    def test_risk_block_sends_nothing_outbound(self):
        _, rule = lookup("payment_risk_check_failed", "business")
        assert not rule.soft_nudge
        assert not rule.explain

    def test_the_business_branch_is_inert_in_exactly_the_same_way(self):
        """§5's precautionary row. Same class, same action, same zero outbound
        as the explicit risk reason — the whole point is that it behaves
        identically without claiming to know what `business` means."""
        _, risk = lookup("payment_risk_check_failed", "business")
        _, business = lookup("payment_failed", "business")
        assert business.failure_class is risk.failure_class
        assert business.retry_budget == risk.retry_budget == 0
        assert business.retry_intervention is risk.retry_intervention is None
        assert business.post_retry is risk.post_retry is InterventionType.HUMAN_QUEUE
        assert not business.soft_nudge
        assert not business.explain

    def test_only_risk_rows_reach_the_human_queue_without_trying_anything(self):
        """HUMAN_QUEUE-as-first-action is rare and deliberate: two rows, both
        RISK_BLOCK. Every other route to a human costs a spent budget first."""
        immediate_queue = {
            key
            for key, rule in RULES.items()
            if rule.retry_budget == 0 and rule.post_retry is InterventionType.HUMAN_QUEUE
        }
        assert immediate_queue == {
            ("payment_risk_check_failed", SOURCE_ANY),
            ("payment_failed", "business"),
        }
        assert all(RULES[key].failure_class is FailureClass.RISK_BLOCK for key in immediate_queue)


class TestInstrumentDeadBudget:
    def test_no_instrument_dead_row_retries_more_than_the_soft_decline_probe(self):
        """§5 (card_declined): 'One retry after six hours, then treat as
        INSTRUMENT_DEAD.' One probe, never a ladder."""
        for key, rule in RULES.items():
            if rule.failure_class is FailureClass.INSTRUMENT_DEAD:
                assert rule.retry_budget <= 1, key

    def test_instrument_dead_always_ends_at_a_new_instrument(self):
        for key, rule in RULES.items():
            if rule.failure_class is FailureClass.INSTRUMENT_DEAD:
                assert rule.post_retry is InterventionType.REAUTH_LINK, key
