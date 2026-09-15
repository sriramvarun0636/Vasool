"""The e-mandate lifecycle: reachable, terminal where it says so, and cited.

The design this implements (docs/EVALUATION.md §10, 2026-09-15) names its
central risk — a lifecycle built from documents cannot be checked against a
live account — and its mitigation: every transition cites the clause that
permits it, so a wrong transition is a wrong citation a reader can find. These
tests are what make that mitigation more than a convention: a transition
without a citation, a citation to nothing, and a quotation that has drifted
from its source all fail here.

The last class runs the debit path end to end through the adversary arena —
the real receiver, the real `PolicyMachine`, all fifteen guards, the real
ledger — for the three rules the lifecycle adds to a debit's path.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vasool.diagnosis.rules import IST
from vasool.mandate.citations import CLAUSES, DOCUMENTS, Authority, authorities, cite
from vasool.mandate.machine import (
    TRANSITIONS,
    IllegalTransition,
    MandateMachine,
    Transition,
    Trigger,
)
from vasool.mandate.record import MandateRail, MandateRecord
from vasool.mandate.states import TERMINAL, MandateState
from vasool.policy.episode import State
from vasool.policy.guards.autopay_peak_hours import PEAK_HOURS_IST
from vasool.policy.guards.pre_debit_notice import PRE_DEBIT_NOTICE_LEAD
from vasool.policy.verdict import Decision
from windtunnel.adversary.arena import Arena
from windtunnel.adversary.criterion import NoDebitBeforeNoticeMatures
from tests.policy.strategies import card_mandate, upi_mandate

S = MandateState
T = Trigger
ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)


def _record(state: MandateState = S.ACTIVE, **overrides) -> MandateRecord:
    return upi_mandate(**({"state": state, "valid_until": NOW + timedelta(days=365)} | overrides))


# ---------------------------------------------------------------------------
# the states
# ---------------------------------------------------------------------------
class TestTheStates:
    def test_every_state_is_reachable_from_created(self):
        seen, frontier = {S.CREATED}, deque([S.CREATED])
        while frontier:
            state = frontier.popleft()
            for transition in TRANSITIONS:
                if transition.frm is state and transition.to not in seen:
                    seen.add(transition.to)
                    frontier.append(transition.to)
        assert seen == set(MandateState)

    def test_every_terminal_state_is_terminal(self):
        assert not [t for t in TRANSITIONS if t.frm in TERMINAL]

    def test_a_transition_out_of_a_terminal_state_cannot_be_built(self):
        with pytest.raises(ValueError, match="terminal"):
            Transition(S.REVOKED, S.ACTIVE, T.UNPAUSED_BY_PAYER, cite("RBI-EMF-2026 §4(b)"))

    def test_the_terminal_states_are_revoked_and_expired(self):
        assert TERMINAL == {S.REVOKED, S.EXPIRED}

    def test_every_other_state_has_a_way_out(self):
        exits = {t.frm for t in TRANSITIONS if t.to is not t.frm}
        assert exits == set(MandateState) - TERMINAL

    def test_one_transition_per_state_and_trigger(self):
        keys = [(t.frm, t.trigger) for t in TRANSITIONS]
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# every transition points at the clause that permits it
# ---------------------------------------------------------------------------
class TestEveryTransitionIsCited:
    def test_no_transition_has_an_empty_citation(self):
        assert all(t.citation for t in TRANSITIONS)

    def test_a_transition_without_a_citation_cannot_be_built(self):
        with pytest.raises(ValueError, match="cite"):
            Transition(S.ACTIVE, S.REVOKED, T.REVOKED_BY_PAYER, ())

    def test_every_citation_resolves_to_a_quoted_clause_in_a_known_document(self):
        for transition in TRANSITIONS:
            for key in transition.citation:
                assert CLAUSES[key].document in DOCUMENTS, (transition.trigger, key)

    def test_every_refusal_is_cited_too(self):
        for transition in TRANSITIONS:
            for condition in transition.conditions:
                assert condition.citation, transition.trigger
                assert all(key in CLAUSES for key in condition.citation)

    def test_citing_a_clause_that_is_not_quoted_fails_at_once(self):
        with pytest.raises(KeyError):
            cite("RBI-EMF-2026 §99")

    def test_exactly_one_edge_rests_on_platform_documentation_alone(self):
        """Resuming a paused mandate is documented by Razorpay's API
        reference and by no rule document found. That is recorded rather than
        hidden, and held at one edge — PAUSED to ACTIVE, by the payer or at the
        pause's end — so a second such edge cannot arrive unnoticed."""
        platform_only = {
            (t.frm, t.to)
            for t in TRANSITIONS
            if authorities(t.citation) == {Authority.PLATFORM}
        }
        assert platform_only == {(S.PAUSED, S.ACTIVE)}

    def test_platform_documentation_is_cited_by_the_pause_edge_and_the_rails_three_reports(self):
        """Four kinds of platform evidence and no more: resuming a pause, and
        the rail reporting a mandate revoked, paused or expired when a debit
        fails (docs/EVALUATION.md §10, 2026-09-15). A fifth fails here."""
        citing_platform = {
            t.trigger for t in TRANSITIONS if Authority.PLATFORM in authorities(t.citation)
        }
        assert citing_platform == {
            T.UNPAUSED_BY_PAYER, T.PAUSE_ENDED,
            T.RAIL_REPORTED_REVOKED, T.RAIL_REPORTED_PAUSED, T.RAIL_REPORTED_EXPIRED,
        }

    def test_the_rails_reports_cite_the_rail_beside_the_platform(self):
        """Razorpay's reason is what a merchant receives; NPCI's code is what
        the state means. A report citing only the first would rest a state
        change on a platform's description."""
        for transition in TRANSITIONS:
            if transition.trigger.value.startswith("rail_reported_"):
                assert authorities(transition.citation) == {Authority.PLATFORM, Authority.RAIL}

    def test_every_other_transition_has_a_regulator_or_the_rail_behind_it(self):
        for transition in TRANSITIONS:
            if (transition.frm, transition.to) == (S.PAUSED, S.ACTIVE):
                continue
            assert authorities(transition.citation) & {Authority.REGULATOR, Authority.RAIL}


# ---------------------------------------------------------------------------
# the quotations are the sources
# ---------------------------------------------------------------------------
def _cited_payload_codes() -> dict[str, str]:
    """Every NPCI code transcribed by tools/cite_npci.py, by '<section> <code>'."""
    out = {}
    for path in sorted((ROOT / "data" / "cited_payloads").glob("npci_upi_v2.9__s*.json")):
        section = path.stem.split("__s")[1].replace("_", ".")
        for row in json.loads(path.read_text())["codes"]:
            out[f"§{section} {row['code']}"] = row["description"]
    return out


class TestTheQuotationsAreTheSources:
    def test_a_quoted_npci_code_says_what_the_transcribed_specification_says(self):
        """The same four codes are quoted here and transcribed in
        data/cited_payloads/. Two copies of one fact are a drift waiting to
        happen, so the copy in the lifecycle is checked against the other."""
        codes = _cited_payload_codes()
        quoted = [c for c in CLAUSES.values() if c.document == "NPCI-UPI-CODES-2.9"]
        assert len(quoted) == 4
        for clause in quoted:
            section_and_code = clause.key.removeprefix("NPCI-UPI-CODES-2.9 ")
            assert codes[section_and_code] == clause.text, clause.key

    def test_the_specification_cited_is_the_one_pinned_beside_its_transcription(self):
        path = ROOT / "data" / "cited_payloads" / "npci_upi_v2.9__s3_1.json"
        provenance = json.loads(path.read_text())["_PROVENANCE"]
        document = DOCUMENTS["NPCI-UPI-CODES-2.9"]
        assert document.sha256 == provenance["sha256"]
        assert document.retrieved_from == provenance["retrieved_from"]

    def test_only_platform_documentation_goes_unpinned(self):
        for document in DOCUMENTS.values():
            if document.authority is Authority.PLATFORM:
                continue
            assert document.sha256 is not None and len(document.sha256) == 64, document.key

    def test_every_clause_is_keyed_by_its_document(self):
        for clause in CLAUSES.values():
            assert clause.key.startswith(clause.document + " ")

    def test_the_framework_is_quoted_as_printed_errors_and_all(self):
        """Verbatim means verbatim: §6(c) prints "shall provider", and a quote
        that silently corrected it would no longer be the Directions' text."""
        assert "shall provider a customer" in CLAUSES["RBI-EMF-2026 §6(c)"].text

    def test_the_thresholds_the_afa_guard_uses_are_the_framework_s(self):
        assert "up to ₹15,000/-" in CLAUSES["RBI-EMF-2026 §8(a)"].text
        assert "up to ₹1,00,000/-" in CLAUSES["RBI-EMF-2026 §8(b)"].text

    def test_the_peak_windows_the_guard_uses_are_the_circular_s(self):
        text = CLAUSES["NPCI-OC-215A ¶3"].text
        for opens, closes in PEAK_HOURS_IST:
            assert f"{opens:%H:%M} hrs to {closes:%H:%M} hrs" in text

    def test_the_notice_lead_is_the_framework_s(self):
        assert PRE_DEBIT_NOTICE_LEAD == timedelta(hours=24)
        assert "at least 24 hours prior" in CLAUSES["RBI-EMF-2026 §6(a)"].text

    def test_a_clause_is_immutable(self):
        clause = next(iter(CLAUSES.values()))
        with pytest.raises(FrozenInstanceError):
            clause.text = "anything"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# the machine
# ---------------------------------------------------------------------------
class TestTheMachine:
    machine = MandateMachine()

    def test_a_mandate_is_created_and_authenticated(self):
        record = _record(S.CREATED)
        record, first = self.machine.apply(record, T.REGISTRATION_REQUESTED, at=NOW)
        record, second = self.machine.apply(record, T.AUTHENTICATED, at=NOW)
        assert record.state is S.ACTIVE
        assert "RBI-EMF-2026 §4(a)" in first.citation and "RBI-EMF-2026 §4(a)" in second.citation

    def test_a_transition_returns_a_new_record_and_leaves_the_old_one_alone(self):
        before = _record(S.ACTIVE)
        after, _ = self.machine.apply(before, T.REVOKED_BY_PAYER, at=NOW)
        assert (before.state, after.state) == (S.ACTIVE, S.REVOKED)

    def test_an_undeclared_transition_names_the_state_it_was_tried_from(self):
        with pytest.raises(IllegalTransition, match="PENDING_AUTH"):
            self.machine.apply(_record(S.PENDING_AUTH), T.PAUSED_BY_PAYER, at=NOW)

    @pytest.mark.parametrize("state", sorted(TERMINAL))
    @pytest.mark.parametrize("trigger", list(Trigger))
    def test_nothing_leaves_a_terminal_state(self, state, trigger):
        with pytest.raises(IllegalTransition):
            self.machine.apply(_record(state), trigger, at=NOW)

    def test_a_upi_mandate_pauses_and_resumes(self):
        paused, _ = self.machine.apply(
            _record(), T.PAUSED_BY_PAYER, at=NOW, paused_until=NOW + timedelta(days=3)
        )
        resumed, _ = self.machine.apply(paused, T.UNPAUSED_BY_PAYER, at=NOW)
        assert (paused.state, resumed.state, resumed.paused_until) == (S.PAUSED, S.ACTIVE, None)

    def test_a_card_mandate_cannot_be_paused_and_the_refusal_says_why(self):
        with pytest.raises(IllegalTransition, match="NPCI-OC-223"):
            self.machine.apply(card_mandate(), T.PAUSED_BY_PAYER, at=NOW)

    def test_a_non_revocable_mandate_is_neither_paused_nor_revoked_by_its_payer(self):
        """NPCI OC-125A: loan and EMI mandates created with "revokeable" set to
        "N" offer the payer no revoke and no pause."""
        loan = _record(revocable_by_payer=False)
        for trigger in (T.PAUSED_BY_PAYER, T.REVOKED_BY_PAYER):
            with pytest.raises(IllegalTransition, match="NPCI-OC-125A"):
                self.machine.apply(loan, trigger, at=NOW)

    def test_a_non_revocable_mandate_is_revoked_through_the_merchant(self):
        loan = _record(revocable_by_payer=False)
        revoked, transition = self.machine.apply(loan, T.REVOKED_BY_PAYEE, at=NOW)
        assert revoked.state is S.REVOKED
        assert "NPCI-OC-125A ¶4" in transition.citation

    def test_validity_ends_at_its_date_and_not_before(self):
        record = _record(valid_until=NOW + timedelta(days=1))
        with pytest.raises(IllegalTransition, match="validity"):
            self.machine.apply(record, T.VALIDITY_ENDED, at=NOW)
        expired, _ = self.machine.apply(record, T.VALIDITY_ENDED, at=NOW + timedelta(days=1))
        assert expired.state is S.EXPIRED

    def test_a_pause_ends_at_its_end_and_not_before(self):
        paused = _record(S.PAUSED, paused_until=NOW + timedelta(days=2))
        with pytest.raises(IllegalTransition):
            self.machine.apply(paused, T.PAUSE_ENDED, at=NOW)
        resumed, _ = self.machine.apply(paused, T.PAUSE_ENDED, at=NOW + timedelta(days=2))
        assert resumed.state is S.ACTIVE

    def test_a_pause_with_no_end_does_not_end_by_itself(self):
        with pytest.raises(IllegalTransition):
            self.machine.apply(_record(S.PAUSED), T.PAUSE_ENDED, at=NOW + timedelta(days=400))

    def test_a_modification_changes_the_validity_period_and_nothing_else(self):
        later = NOW + timedelta(days=700)
        modified, _ = self.machine.apply(_record(), T.MODIFIED, at=NOW, valid_until=later)
        assert modified.valid_until == later
        with pytest.raises(IllegalTransition, match="cannot change"):
            self.machine.apply(_record(), T.MODIFIED, at=NOW, rail=MandateRail.CARD)

    def test_a_lapsed_registration_expires_and_a_declined_one_is_refused_authority(self):
        lapsed, lapse = self.machine.apply(_record(S.PENDING_AUTH), T.REGISTRATION_LAPSED, at=NOW)
        declined, _ = self.machine.apply(_record(S.PENDING_AUTH), T.REGISTRATION_DECLINED, at=NOW)
        assert (lapsed.state, declined.state) == (S.EXPIRED, S.REVOKED)
        assert "NPCI-UPI-CODES-2.9 §4.4 U69" in lapse.citation


# ---------------------------------------------------------------------------
# what a debit would meet
# ---------------------------------------------------------------------------
class TestStateAt:
    def test_a_mandate_is_live_until_its_validity_ends(self):
        record = _record(valid_until=NOW + timedelta(days=1))
        assert record.state_at(NOW) is S.ACTIVE
        assert record.state_at(NOW + timedelta(days=1)) is S.EXPIRED

    def test_a_pause_lapses_at_its_end(self):
        record = _record(S.PAUSED, paused_until=NOW + timedelta(days=2))
        assert record.state_at(NOW + timedelta(days=1)) is S.PAUSED
        assert record.state_at(NOW + timedelta(days=2)) is S.ACTIVE

    def test_a_pause_that_outlasts_the_validity_ends_in_expiry(self):
        record = _record(
            S.PAUSED, valid_until=NOW + timedelta(days=1), paused_until=NOW + timedelta(days=5)
        )
        assert record.state_at(NOW + timedelta(days=5)) is S.EXPIRED

    @pytest.mark.parametrize("state", sorted(TERMINAL))
    def test_a_terminal_state_is_what_it_is_at_any_time(self, state):
        assert _record(state).state_at(NOW + timedelta(days=9999)) is state

    def test_an_unregistered_mandate_is_not_made_live_by_the_calendar(self):
        assert _record(S.PENDING_AUTH).state_at(NOW) is S.PENDING_AUTH


# ---------------------------------------------------------------------------
# the debit path, end to end
# ---------------------------------------------------------------------------
def _executed_debits(arena: Arena, entity_id: str):
    return [
        r
        for r in arena.ledger()
        if r.executed and r.entity_id == entity_id and r.proposal is not None and r.proposal.is_retry
    ]


def _verdicts(arena: Arena, entity_id: str, guard: str):
    """Every ruling `guard` gave on this episode, read from the transition log
    rather than the ledger: a deferral is a transition, not a receipt, and the
    peak-hours save lives only there."""
    return [
        v
        for t in arena.transitions()
        if t.entity_id == entity_id and t.chain is not None
        for v in t.chain.verdicts
        if v.guard == guard
    ]


def _in_peak(when: datetime) -> bool:
    local = when.astimezone(IST).time()
    return any(opens <= local <= closes for opens, closes in PEAK_HOURS_IST)


class TestTheDebitPath:
    def test_a_upi_debit_waits_for_a_matured_notice_and_for_the_peak_to_pass(self):
        """A failure at 10:20 IST: the retry is due at 10:25, inside NPCI's
        morning peak and with no notice served. The notice request is a
        non-customer-initiated API call, so the peak holds it too (OC-215A ¶3);
        it goes out just after 13:00, and the debit runs 24 hours after that,
        outside both peaks."""
        arena = Arena()
        mandy = arena.person("mandy", mandate=upi_mandate())
        arena.advance_to(arena.ist(hour=10, minute=20))
        entity_id = arena.fail(mandy, "gateway_technical_error", entity_id="pay_upi_notice")
        arena.advance_to(arena.ist(day=4, hour=12))

        debits = _executed_debits(arena, entity_id)
        assert debits, "the debit never executed — liveness, not safety"
        assert NoDebitBeforeNoticeMatures(entity_id, PRE_DEBIT_NOTICE_LEAD).check(arena).held
        assert not [d for d in debits if _in_peak(d.at)]
        assert any(
            v.decision is Decision.DEFER for v in _verdicts(arena, entity_id, "AutopayPeakHoursGuard")
        )

    def test_a_card_debit_is_not_held_to_npci_s_hours(self):
        arena = Arena()
        mandy = arena.person("mandy", mandate=card_mandate())
        arena.advance_to(arena.ist(hour=10, minute=20))
        entity_id = arena.fail(mandy, "gateway_technical_error", entity_id="pay_card_notice")
        arena.advance_to(arena.ist(day=4, hour=12))

        assert _executed_debits(arena, entity_id)
        assert {v.decision for v in _verdicts(arena, entity_id, "AutopayPeakHoursGuard")} == {
            Decision.NOT_APPLICABLE
        }

    def test_a_revoked_mandate_is_never_debited(self):
        """The retry is built while the mandate is live and held for its
        pre-debit notice. The customer revokes inside that day. When the retry
        comes due, MandateStateGuard refuses it — nothing touches the account,
        and the receipt names the revocation."""
        arena = Arena()
        mandy = arena.person("mandy", mandate=upi_mandate())
        arena.advance_to(arena.ist(hour=14))
        entity_id = arena.fail(mandy, "gateway_technical_error", entity_id="pay_revoked")
        arena.advance_to(arena.ist(hour=18))
        arena.mandate_event(mandy, Trigger.REVOKED_BY_PAYER)
        arena.advance_to(arena.ist(day=5, hour=12))

        assert not _executed_debits(arena, entity_id)
        refusals = [
            v for v in _verdicts(arena, entity_id, "MandateStateGuard") if v.decision is Decision.BLOCK
        ]
        assert refusals and "revoked" in refusals[0].reason
        assert arena.state_of(entity_id) is State.BLOCKED

    def test_a_paused_mandate_is_not_debited_while_paused(self):
        arena = Arena()
        mandy = arena.person("mandy", mandate=upi_mandate())
        arena.advance_to(arena.ist(hour=14))
        entity_id = arena.fail(mandy, "gateway_technical_error", entity_id="pay_paused")
        arena.advance_to(arena.ist(hour=18))
        arena.mandate_event(
            mandy, Trigger.PAUSED_BY_PAYER, paused_until=arena.ist(day=20, hour=0)
        )
        arena.advance_to(arena.ist(day=5, hour=12))

        assert not _executed_debits(arena, entity_id)
        assert any(
            v.decision is Decision.BLOCK and "paused" in (v.reason or "")
            for v in _verdicts(arena, entity_id, "MandateStateGuard")
        )

    def test_the_arena_cannot_do_to_a_mandate_what_the_lifecycle_forbids(self):
        arena = Arena()
        loan = arena.person("lena", mandate=upi_mandate(revocable_by_payer=False))
        with pytest.raises(IllegalTransition, match="NPCI-OC-125A"):
            arena.mandate_event(loan, Trigger.REVOKED_BY_PAYER)



class TestTheRailMovesTheMandate:
    """vasool/mandate/evidence.py: a failed debit's reason moves the record to
    the state the rail reports, and a disagreement is kept, never resolved in
    the record's favour."""

    def _upi(self, **changes):
        from tests.policy.strategies import upi_mandate

        return upi_mandate(**changes)

    def test_three_reasons_move_the_record_and_every_other_reason_does_not(self):
        from vasool.mandate.evidence import RAIL_EVIDENCE, apply_rail_evidence
        from windtunnel.payloads import upi_reasons

        record = self._upi()
        for reason in sorted(upi_reasons()):
            moved, observation = apply_rail_evidence(record, reason, at=NOW)
            if reason in RAIL_EVIDENCE:
                assert moved.state is RAIL_EVIDENCE[reason][1], reason
                assert observation.transition is not None
            else:
                assert moved is record and observation is None, reason

    def test_a_terminal_record_does_not_move_and_the_contradiction_is_kept(self):
        from vasool.mandate.evidence import apply_rail_evidence

        expired = self._upi(state=S.EXPIRED)
        moved, observation = apply_rail_evidence(expired, "mandate_paused", at=NOW)
        assert moved is expired and observation.transition is None
        assert "nothing leaves" in observation.disagreement

    def test_a_cancellation_by_the_user_of_a_non_revocable_mandate_moves_it_and_says_so(self):
        from vasool.mandate.evidence import apply_rail_evidence

        loan = self._upi(revocable_by_payer=False)
        moved, observation = apply_rail_evidence(loan, "mandate_cancelled", at=NOW)
        assert moved.state is S.REVOKED
        assert "non-revocable" in observation.disagreement

    def test_a_reported_state_the_record_already_holds_is_not_a_transition(self):
        from vasool.mandate.evidence import apply_rail_evidence

        paused = self._upi(state=S.PAUSED)
        moved, observation = apply_rail_evidence(paused, "mandate_paused", at=NOW)
        assert moved is paused and observation.transition is None and observation.disagreement is None
