"""§2.5's failure path: a UPI Autopay debit fails with a reason Razorpay
documents, is classified into one of the five classes or refused, and nothing
that may be in flight is ever retried (docs/EVALUATION.md §10, 2026-09-15).

Four parts: the mapping against the stubs on disk; the rules that must never
retry; the rail deciding which table a failure is read against; and the
registered "done when", end to end, in the adversary's arena.
"""
from __future__ import annotations

import json
import pathlib
from datetime import timedelta

import pytest

from vasool.clock import VirtualClock
from vasool.diagnosis import npci, razorpay_upi, upi
from vasool.diagnosis.npci import Unmapped
from vasool.diagnosis.rules import classify
from vasool.diagnosis.taxonomy import FailureClass, InterventionType
from vasool.events.provenance import Provenance, tier_of
from vasool.events.rail_codes import NullRailCodeSource
from vasool.events.schemas import from_webhook
from vasool.ledger.receipts import Outcome
from vasool.mandate.citations import CLAUSES
from vasool.mandate.machine import MandateMachine, Trigger
from vasool.mandate.record import MandateCategory, MandateRail, MandateRecord
from vasool.mandate.states import MandateState
from vasool.policy.episode import State
from windtunnel import payloads
from windtunnel.adversary.arena import Arena
from tests.payloads import TEST_PEPPER, event_for

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
STUBS = sorted((REPO_ROOT / "data" / "stubbed_payloads").glob(f"{payloads.UPI_PREFIX}*.json"))


def _entity(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())["body"]["payload"]["payment"]["entity"]


def _upi_event(reason: str, *, rail_codes=None):
    path = REPO_ROOT / "data" / "stubbed_payloads" / f"{payloads.UPI_PREFIX}{reason}.json"
    fixture = json.loads(path.read_text())
    return from_webhook(
        event_id=fixture["headers"]["x-razorpay-event-id"], body=fixture["body"],
        pepper=TEST_PEPPER, rail_codes=rail_codes,
    )


def _classify(event):
    return classify(event, clock=VirtualClock(event.occurred_at))


# ---------------------------------------------------------------------------
# the mapping, and the stubs it rests on
# ---------------------------------------------------------------------------
class TestTheSixtyOneReasons:
    def test_every_documented_reason_is_mapped_once_and_has_one_stub(self):
        assert len(razorpay_upi.MAPPINGS) == 61
        assert len(razorpay_upi.BY_REASON) == 61
        assert set(razorpay_upi.BY_REASON) == payloads.upi_reasons()
        assert len(STUBS) == 61

    def test_every_stub_is_a_simulated_upi_failure_carrying_only_what_is_documented(self):
        for path in STUBS:
            entity = _entity(path)
            assert tier_of(path) is Provenance.SIMULATED
            assert entity["method"] == "upi"
            assert entity["error_source"] is None and entity["error_step"] is None, path.name
            assert entity["error_description"]
            assert razorpay_upi.DOCUMENT["sha256"][:12] in json.loads(path.read_text())["_source_note"]

    def test_a_code_appears_only_where_razorpay_files_the_reason_under_one(self):
        coded = [p for p in STUBS if _entity(p)["error_code"] is not None]
        assert len(coded) == 19
        assert {_entity(p)["error_code"] for p in coded} <= {"BAD_REQUEST_ERROR", "GATEWAY_ERROR"}

    def test_no_card_reader_can_see_a_upi_stub(self):
        """The card index globs `SIMULATED__payment_failed__*`; a UPI stub in it
        would change which envelope a registered universe draws from. Every UPI
        stub carries a null source and no card envelope does, so one null in
        the card index would be a UPI stub that got in."""
        assert all(source is not None for _, source in payloads.available_pairs())
        assert not any(p.name.startswith(payloads.UPI_PREFIX) for p in payloads._envelope_paths())

    def test_a_description_that_says_money_may_have_moved_is_never_a_class(self):
        for path in STUBS:
            entity = _entity(path)
            text = entity["error_description"].lower()
            if "deducted" in text or "pending" in text or "already honoured" in text:
                assert razorpay_upi.outcome(entity["error_reason"]) is Unmapped.RECONCILE, entity["error_reason"]

    def test_the_three_mandate_states_are_quoted_as_the_stubs_describe_them(self):
        for reason in ("mandate_cancelled", "mandate_paused", "mandate_expired"):
            clause = CLAUSES[f"RAZORPAY-UPI-SUBSEQUENT {reason}"]
            stub = REPO_ROOT / "data" / "stubbed_payloads" / f"{payloads.UPI_PREFIX}{reason}.json"
            assert clause.text == _entity(stub)["error_description"]


# ---------------------------------------------------------------------------
# the rules: nothing unmapped is retried
# ---------------------------------------------------------------------------
class TestNothingInFlightIsRetried:
    @pytest.mark.parametrize("mapping", razorpay_upi.MAPPINGS, ids=lambda m: m.reason)
    def test_an_unmapped_reason_never_takes_a_retry(self, mapping):
        _, rule = upi.rule_for_reason(mapping.reason)
        if isinstance(mapping.outcome, FailureClass):
            return
        assert rule.retry_budget == 0
        expected = InterventionType.STATUS_CHECK if mapping.outcome is Unmapped.RECONCILE else InterventionType.HUMAN_QUEUE
        assert rule.post_retry is expected
        assert rule.rationale.startswith(f"Unmapped ({mapping.outcome.value})")

    @pytest.mark.parametrize("mapping", npci.MAPPINGS, ids=lambda m: f"{m.section}-{m.code}")
    def test_an_unmapped_npci_code_never_takes_a_retry_either(self, mapping):
        _, rule = upi.rule_for_rail_code(mapping.section, mapping.code)
        if isinstance(mapping.outcome, FailureClass):
            return
        assert rule.retry_budget == 0
        assert rule.post_retry in (InterventionType.STATUS_CHECK, InterventionType.HUMAN_QUEUE)

    def test_money_in_flight_is_checked_after_ninety_seconds(self):
        _, rule = upi.rule_for_reason("payment_pending")
        assert rule.post_retry is InterventionType.STATUS_CHECK
        assert rule.post_retry_delay == timedelta(seconds=90)

    def test_an_undocumented_upi_reason_goes_to_a_person_not_to_the_card_fail_safe(self):
        reason, rule = upi.rule_for_reason("a_reason_nobody_documented")
        assert reason == "unknown"
        assert rule.retry_budget == 0 and rule.post_retry is InterventionType.HUMAN_QUEUE


# ---------------------------------------------------------------------------
# the rail decides the table
# ---------------------------------------------------------------------------
class TestTheRailDecides:
    def test_a_timeout_is_one_retry_on_a_card_and_a_status_check_on_upi(self):
        card = _classify(event_for("payment_timed_out"))
        rail = _classify(_upi_event("payment_timed_out"))
        assert card.intervention is InterventionType.SILENT_RETRY
        assert rail.intervention is InterventionType.STATUS_CHECK

    @pytest.mark.parametrize("path", STUBS, ids=lambda p: p.stem.removeprefix(payloads.UPI_PREFIX))
    def test_every_stub_classifies_as_its_mapping_says(self, path):
        reason = _entity(path)["error_reason"]
        diagnosis = _classify(_upi_event(reason))
        _, rule = upi.rule_for_reason(reason)
        first = rule.retry_intervention if rule.retry_budget else rule.post_retry
        assert diagnosis.reason == reason
        assert diagnosis.intervention is first

    def test_a_rail_code_decides_when_a_provider_passes_one(self):
        class PassesVA:
            def code_for(self, body):
                return ("3.1", "VA")

        assert NullRailCodeSource().code_for({}) is None
        event = _upi_event("payment_failed", rail_codes=PassesVA())
        assert event.rail_code == ("3.1", "VA")
        diagnosis = _classify(event)
        assert diagnosis.reason == "npci:3.1:VA"
        assert diagnosis.failure_class is FailureClass.INSTRUMENT_DEAD
        assert diagnosis.intervention is InterventionType.REAUTH_LINK

    def test_razorpay_sends_no_rail_code(self):
        assert _upi_event("payment_failed").rail_code is None


# ---------------------------------------------------------------------------
# the registered "done when", end to end
# ---------------------------------------------------------------------------
def _registered_upi_mandate(arena: Arena) -> tuple[MandateRecord, list]:
    """Created and authenticated through the cited lifecycle — every step a
    transition that names its clause."""
    machine = MandateMachine()
    record = MandateRecord(
        mandate_id="umn_done_when", rail=MandateRail.UPI_AUTOPAY, category=MandateCategory.GENERAL,
        state=MandateState.CREATED, valid_until=arena.EPOCH + timedelta(days=365),
    )
    taken = []
    for trigger in (Trigger.REGISTRATION_REQUESTED, Trigger.AUTHENTICATED):
        record, transition = machine.apply(record, trigger, at=arena.now())
        taken.append(transition)
    return record, taken


def _last_retry_payment_id(arena: Arena, entity_id: str) -> str:
    retries = [d for d in arena.dispatched() if d.entity_id == entity_id and d.is_retry and d.ok]
    return arena._inner.journal.get(retries[-1].proposal_id).razorpay_request_id


class TestTheDoneWhenSentence:
    """"A UPI Autopay mandate can be created, authenticated, debited, fail with
    a reason Razorpay documents, be classified into one of the five classes or
    refused with its reason — with every mandate transition citing the clause
    that permits it." (docs/EVALUATION.md §10, 2026-09-15)"""

    def test_created_authenticated_debited_failed_classified_refused_and_cited(self):
        arena = Arena()
        record, lifecycle = _registered_upi_mandate(arena)
        assert record.state is MandateState.ACTIVE
        person = arena.person("dora", mandate=record)
        arena.jump_to(arena.ist(day=2, hour=7))

        # Debited: a transient failure, a pre-debit notice, then the debit.
        entity = arena.fail(person, "issuer_dispatch_failed", entity_id="pay_done_when", upi=True)
        arena.advance_by(timedelta(days=2))
        debits = [d for d in arena.dispatched() if d.entity_id == entity and d.is_retry]
        assert len(debits) == 1 and debits[0].ok

        # It fails with a documented reason: the customer cancelled the mandate.
        arena.fail(person, "mandate_cancelled", entity_id=_last_retry_payment_id(arena, entity), upi=True)
        arena.advance_by(timedelta(days=3))

        # Classified into one of the five, and the rail moved the mandate.
        assert arena.facts.people[person.customer_id].mandate.state is MandateState.REVOKED
        observation = arena.facts.mandate_log[-1]
        assert observation.transition.trigger is Trigger.RAIL_REPORTED_REVOKED
        assert set(observation.transition.citation) == {
            "RAZORPAY-UPI-SUBSEQUENT mandate_cancelled", "NPCI-UPI-CODES-2.9 §3.1 VA",
        }
        classified = [t for t in arena.transitions() if t.to_state is State.DIAGNOSED and "revoked" in t.note]
        assert classified, "the second failure was classified from its own reason"
        links = [d for d in arena.dispatched() if d.entity_id == entity and d.intervention == "REAUTH_LINK"]
        assert len(links) == 1, "a revoked mandate is answered by asking for a new one"
        assert len([d for d in arena.dispatched() if d.entity_id == entity and d.is_retry]) == 1

        # Every mandate transition taken cites the clause that permits it.
        for transition in (*lifecycle, observation.transition):
            assert transition.citation and all(key in CLAUSES for key in transition.citation)

    def test_a_pending_debit_is_checked_and_never_retried(self):
        arena = Arena()
        person = arena.person("pat", mandate=_registered_upi_mandate(arena)[0])
        arena.jump_to(arena.ist(day=2, hour=7))
        entity = arena.fail(person, "payment_pending", entity_id="pay_pending", upi=True)
        arena.advance_by(timedelta(days=3))

        checks = [r for r in arena.ledger() if r.entity_id == entity and r.proposal
                  and r.proposal.intervention is InterventionType.STATUS_CHECK]
        assert checks, "the rail was asked"
        assert not [d for d in arena.dispatched() if d.entity_id == entity and d.is_retry]
        # No status call is wired for a mandate without a Razorpay token, so a person decides.
        assert arena.state_of(entity) is State.ESCALATED

    @pytest.mark.parametrize(
        "reason",
        sorted(m.reason for m in razorpay_upi.MAPPINGS if not isinstance(m.outcome, FailureClass)),
    )
    def test_no_unmapped_reason_is_ever_retried_in_the_arena(self, reason):
        arena = Arena()
        person = arena.person("una", mandate=_registered_upi_mandate(arena)[0])
        arena.jump_to(arena.ist(day=2, hour=7))
        entity = arena.fail(person, reason, entity_id="pay_unmapped", upi=True)
        arena.advance_by(timedelta(days=10))
        assert not [d for d in arena.dispatched() if d.entity_id == entity and d.is_retry]
        assert arena.state_of(entity) is State.ESCALATED

    def test_the_rail_wins_a_disagreement_and_the_disagreement_is_kept(self):
        arena = Arena()
        person = arena.person("evi", mandate=_registered_upi_mandate(arena)[0])
        arena.jump_to(arena.ist(day=2, hour=7))
        arena.fail(person, "mandate_expired", entity_id="pay_early_expiry", upi=True)
        assert arena.facts.people[person.customer_id].mandate.state is MandateState.EXPIRED
        assert "before the record's valid_until" in arena.facts.mandate_log[-1].disagreement

    def test_a_lost_debit_response_is_the_one_outcome_receipt_that_says_so(self):
        from windtunnel.adversary.attacks import a26_lost_debit_response

        arena = Arena()
        a26_lost_debit_response(arena)
        outcomes = [r.outcome for r in arena.ledger() if r.entity_id == "pay_a26"]
        assert outcomes.count(Outcome.OUTCOME_UNKNOWN) == 1
        assert arena.rail_debits("pay_a26") == 1


# ---------------------------------------------------------------------------
# docs/taxonomy.md §12 is the mapping, in prose
# ---------------------------------------------------------------------------
def _make_upi_stubs():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_make_upi_stubs", REPO_ROOT / "tools" / "make_upi_stubs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSectionTwelveIsTheMapping:
    TAXONOMY = REPO_ROOT / "docs" / "taxonomy.md"

    def test_the_table_is_the_rendered_mapping(self):
        tool = _make_upi_stubs()
        text = self.TAXONOMY.read_text()
        start = text.index(tool.TABLE_START)
        end = text.index(tool.TABLE_END) + len(tool.TABLE_END)
        assert text[start:end] == tool.render_table(), (
            "docs/taxonomy.md §12's table has drifted from vasool/diagnosis/razorpay_upi.py — "
            "run `python tools/make_upi_stubs.py table`, never edit the table by hand."
        )

    def test_the_counts_section_12_quotes_are_the_mapping_s(self):
        import collections

        text = self.TAXONOMY.read_text().split("## 12. UPI Autopay", 1)[1]
        tally = collections.Counter(m.outcome for m in razorpay_upi.MAPPINGS)
        mapped = sum(tally[c] for c in FailureClass)
        assert f"**{mapped} of the 61 reasons fit the five classes. {61 - mapped} do not**" in text
        assert f"and fifteen of those\nsay money may already have moved" in text
        assert tally[Unmapped.RECONCILE] == 15
        for reason in (Unmapped.INTEGRATION, Unmapped.CAP, Unmapped.PAYEE_SIDE,
                       Unmapped.LEGAL_STOP, Unmapped.UNDESCRIBED):
            assert f"`{reason.value}` ({tally[reason]})" in text, reason
        coded = sum(1 for p in STUBS if _entity(p)["error_code"] is not None)
        assert f"The code** is set for the {coded}\nreasons" in text

    def test_the_next_steps_counts_are_the_stubs(self):
        """'Thirteen of the nineteen' and 'fourteen of the fifteen' are read off
        the documented next steps each stub carries, not remembered."""
        def steps(path):
            return json.loads(path.read_text())["_source_note"].split("Documented next steps: ")[1].lower()

        dead = [p for p in STUBS if razorpay_upi.outcome(_entity(p)["error_reason"]) is FailureClass.INSTRUMENT_DEAD]
        in_flight = [p for p in STUBS if razorpay_upi.outcome(_entity(p)["error_reason"]) is Unmapped.RECONCILE]
        assert (len(dead), sum("new mandate" in steps(p) for p in dead)) == (19, 13)
        assert (len(in_flight), sum(("retry" in steps(p)) or ("try again" in steps(p)) for p in in_flight)) == (15, 14)


# ---------------------------------------------------------------------------
# the NPCI port, through the receiver's front door
# ---------------------------------------------------------------------------
class TestARailCodeThroughTheReceiver:
    """A provider that passes NPCI's own code on supplies a `RailCodeSource`;
    the receiver threads it into `from_webhook`, signature and dedupe
    untouched. Exercised here at the real route rather than in an arena scene:
    the arena's wiring is inside the agent fingerprint and passes none, since
    no Razorpay webhook carries one (docs/EVALUATION.md §10, 2026-09-16)."""

    def _post(self, rail_codes):
        import hashlib
        import hmac as _hmac
        from datetime import datetime, timezone

        from fastapi.testclient import TestClient

        from vasool.events.receiver import create_app
        from vasool.events.store import EventStore

        secret = "rail-code-test-secret"
        store = EventStore(":memory:")
        app = create_app(store=store, webhook_secret=secret, pepper=TEST_PEPPER,
                         clock=VirtualClock(datetime(2026, 9, 1, tzinfo=timezone.utc)), rail_codes=rail_codes)
        fixture = json.loads((REPO_ROOT / "data" / "stubbed_payloads" / f"{payloads.UPI_PREFIX}payment_failed.json").read_text())
        raw = json.dumps(fixture["body"], separators=(",", ":")).encode()
        response = TestClient(app).post("/webhook", content=raw, headers={
            "content-type": "application/json",
            "x-razorpay-signature": _hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
            "x-razorpay-event-id": "evt_rail_code_1",
        })
        assert response.status_code == 200
        return store.get("evt_rail_code_1")["failure_event"]

    def test_a_passed_code_decides_the_classification(self):
        class PassesVU:
            def code_for(self, body):
                return ("3.1", "VU")

        event = self._post(PassesVU())
        assert event.rail_code == ("3.1", "VU")
        diagnosis = _classify(event)
        assert diagnosis.reason == "npci:3.1:VU" and diagnosis.intervention is InterventionType.REAUTH_LINK

    def test_without_a_source_razorpay_s_reason_decides(self):
        event = self._post(None)
        assert event.rail_code is None
        assert _classify(event).intervention is InterventionType.STATUS_CHECK
