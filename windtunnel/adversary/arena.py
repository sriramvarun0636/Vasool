"""The world an attack manipulates. Nothing here decides what the agent does.

The boundary is windtunnel/runner.py's, kept deliberately: this module decides
what happens TO the agent — which webhooks arrive and when, who the customer
really is, what the merchant's configuration says — and `vasool/` decides what
the agent does about it. No guard, no classification, no state transition and
no receipt is reimplemented here.

**Every webhook goes through the real front door.** An attack calls
`fail()` or `pay_link()` and the arena POSTs a signed body to the real
FastAPI receiver, in process, over ASGI — no socket is opened. That matters
for the whole dedupe family of attacks: the signature check, the
`x-razorpay-event-id` dedupe and the "only the first delivery may settle
anything" gate are production's own code, and an attack that reached around
them into `EventStore` or `PolicyMachine.settled` would be attacking a
reimplementation of the receiver rather than the receiver.

**The one hop production has not wired.** Nothing today consumes the event
store and feeds `PolicyMachine.observe` — the receiver stores a `FailureEvent`
and stops. So the arena plays that consumer: after a delivery the receiver
reports as newly inserted, it hands the stored event to the machine. That is
the same hop `windtunnel/runner.py` makes by calling `observe` directly, and
it is stated here rather than hidden because it is the one place the arena is
not exercising a wire that exists.

**Retries do not fail on their own.** There is no outcome model in this
package. The adversary is about what the agent is permitted to do, not about
whether money comes back, so a retry has no probability attached: an attack
that wants the ladder to advance says so by calling `fail_last_retry`. That
keeps every attack's script legible as a sequence of things the world did.

**Virtual time, like everything else.** architectural invariant 2 and 5 hold here
without exception; `tests/test_no_wallclock.py` already scans this package and
`tests/adversary/test_attacks.py` replays every attack for a byte-identical
ledger.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from vasool.actions.comms import CommsSender
from vasool.actions.executor import RazorpayExecutor
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import Proposal, template_ids
from vasool.diagnosis.rules import IST
from vasool.events.receiver import create_app
from vasool.events.schemas import derive_customer_id
from vasool.events.store import EventStore
from vasool.ledger.receipts import Receipt, build_from_transitions
from vasool.ledger.tracing import trace_id_for
from vasool.policy.episode import State
from vasool.policy.facts import (
    CONSENT_PURPOSE_RECOVERY,
    ConsentRecord,
    MerchantPolicy,
    PolicyFacts,
)
from vasool.policy.machine import ExecutionResult, PolicyMachine
from vasool.policy.transitions import Transition
from windtunnel import payloads
from windtunnel.adversary.criterion import Dispatch
from windtunnel.universe import EPOCH
from windtunnel.world import MERCHANT_ID

ADVERSARY_PEPPER = "adversary-pepper-not-a-secret"
"""Keys the customer_id HMAC for the attacks.

A literal, not a secret, and deliberately not read from the environment: no
module in `windtunnel/` may touch a secret or the environment, and the
adversary has no reason to want the production pepper — its customer ids only
have to be stable within a run so the frequency cap can count them.
"""

ADVERSARY_WEBHOOK_SECRET = "adversary-webhook-secret-not-real"
"""What the arena signs with and the receiver verifies against. Both sides are
this process, so this is a shared constant rather than a credential."""

MAX_STEPS = 10_000
"""Upper bound on the tick loop, so a scheduling bug fails loudly rather than
hanging the suite. Deferral is bounded three ways inside PolicyMachine, so
reaching this means something upstream changed."""

DEFAULT_AMOUNT_PAISE = 50_000
"""₹500 — what every envelope in data/ actually carries. An attack that cares
about the amount says so."""


def _ist_day(when: datetime) -> date:
    """The IST calendar day a moment falls in.

    IST because `SpendCapGuard`'s own reset is IST midnight — a UTC day here
    would put the arena's accounting boundary five and a half hours away from
    the guard's. The same reasoning, and the same three lines, as
    windtunnel/world.py.
    """
    return when.astimezone(IST).date()


@dataclass(frozen=True, slots=True)
class Person:
    """A human, as the world knows them.

    `customer_id` is the only field the agent ever sees. `human_id` is the
    world's own identity and is what makes attack A07 expressible at all: one
    person with two email addresses is two customers to every guard in the
    chain, because `derive_customer_id` keys on contact+email.
    """

    human_id: str
    contact: str
    email: str
    customer_id: str
    zone: timezone | None = None
    """The customer's real timezone, where an attack has given them one.
    `ContactWindowGuard` evaluates its window in IST and has no fact for
    this — docs/taxonomy.md §9.3."""

    dnd_listed: bool = False
    is_mandate: bool = False


@dataclass(frozen=True, slots=True)
class Script:
    """What the world decided about one payment before the agent saw it."""

    entity_id: str
    person: Person
    reason: str
    source: str
    amount_paise: int


@dataclass
class ArenaFacts:
    """The FactStore the guards read. A dict, exactly as
    `vasool/policy/facts.py` says the simulator's should be.

    Everything here is a fact about the world, never a decision about the
    agent — the same split `windtunnel/world.py` holds. Nothing in this class
    knows what a verdict is.
    """

    merchant: MerchantPolicy
    people: dict[str, Person] = field(default_factory=dict)
    scripts: dict[str, Script] = field(default_factory=dict)
    consent: dict[str, ConsentRecord | None] = field(default_factory=dict)
    promises: dict[str, date] = field(default_factory=dict)
    notices: dict[str, datetime] = field(default_factory=dict)
    contacts: dict[str, list[datetime]] = field(default_factory=dict)
    spent: dict[tuple[str, date], int] = field(default_factory=dict)
    registered_templates: frozenset[str] = field(default_factory=template_ids)

    def snapshot(self, *, event, proposal: Proposal, now: datetime) -> PolicyFacts:
        person = self.people[event.customer_id]
        return PolicyFacts(
            merchant=self.merchant,
            # Left empty on purpose: PolicyMachine merges the episode's own
            # executed keys in, and an idempotency key is scoped to one entity
            # anyway, so a store-level set would be a second copy of a record
            # the episode already holds (windtunnel/world.py says the same).
            executed_keys=frozenset(),
            spent_today_paise=self.spent.get((self.merchant.merchant_id, _ist_day(now)), 0),
            contact_history=tuple(sorted(self.contacts.get(person.customer_id, ()))),
            consent=self.consent.get(person.customer_id),
            dnd_listed=person.dnd_listed,
            dnd_checked_at=now,
            promise_to_pay=self.promises.get(event.entity_id),
            is_mandate=person.is_mandate,
            pre_debit_notice_sent_at=self.notices.get(event.entity_id),
            registered_templates=self.registered_templates,
            # The world has always known where these people are; until A08 was
            # fixed, nothing handed that fact to the guards. None still means
            # unknown, and unknown still resolves to IST.
            customer_zone=person.zone,
        )

    def record(self, proposal: Proposal, *, at: datetime) -> None:
        """Fold one executed action back into the world, synchronously.

        From inside the executor seam rather than at the end of a tick: two
        contacts gated in the same tick must not both pass the frequency cap
        on a snapshot neither of them appears in.
        """
        if proposal.is_contact:
            self.contacts.setdefault(proposal.customer_id, []).append(at)
        if proposal.role.value == "PRE_DEBIT_NOTICE":
            self.notices[proposal.entity_id] = at
        if proposal.is_retry:
            key = (proposal.merchant_id, _ist_day(at))
            self.spent[key] = self.spent.get(key, 0) + proposal.amount_paise


@dataclass
class WatchedExecutor:
    """The real `RazorpayExecutor`, with the world watching.

    Records what was dispatched *from inside the seam*, so that the ledger
    saying what happened is a claim the criterion tests rather than an
    assumption it inherits (`windtunnel/metrics.py` makes the same move for
    the same reason).
    """

    inner: RazorpayExecutor
    facts: ArenaFacts
    clock: VirtualClock
    calls: list[Dispatch] = field(default_factory=list)

    def execute(self, proposal: Proposal) -> ExecutionResult:
        at = self.clock.now()
        result = self.inner.execute(proposal)
        self.facts.record(proposal, at=at)
        self.calls.append(
            Dispatch(
                entity_id=proposal.entity_id,
                customer_id=proposal.customer_id,
                proposal_id=proposal.proposal_id,
                intervention=proposal.intervention.value,
                role=proposal.role.value,
                amount_paise=proposal.amount_paise,
                at=at,
                is_contact=proposal.is_contact,
                is_retry=proposal.is_retry,
                ok=result.ok,
            )
        )
        return result


class SimulatedRazorpay:
    """A Razorpay that never leaves the process.

    Ids are derived from the idempotency key rather than counted, so they do
    not depend on call order — a retry's id is the join key `RetryIndex`
    correlates a later capture through, so an order-dependent id would make
    settlement order-dependent too. Identical in shape and reasoning to
    windtunnel/runner.py's; kept separate because that one belongs to the
    evaluation's wiring and this one to the adversary's.
    """

    @staticmethod
    def _id(prefix: str, basis: str) -> str:
        return prefix + hashlib.sha256(basis.encode()).hexdigest()[:14]

    def create_payment_link(self, *, idempotency_key: str, **kwargs) -> dict:
        link_id = self._id("plink_", idempotency_key)
        return {"id": link_id, "short_url": f"https://rzp.io/l/{link_id[-8:]}"}

    def notify_payment_link(self, **kwargs) -> dict:
        return {"success": True}

    def retry_payment(self, *, idempotency_key: str, **kwargs) -> dict:
        return {"id": self._id("pay_", idempotency_key)}


class Arena:
    """One attack's world. Implements `criterion.Scene`."""

    EPOCH = EPOCH
    """The same calendar anchor the wind tunnel uses, so an attack's
    timestamps read against the same September as every other artefact."""

    def __init__(self) -> None:
        self.clock = VirtualClock(self.EPOCH)
        self.facts = ArenaFacts(merchant=MerchantPolicy(merchant_id=MERCHANT_ID))
        self._razorpay = SimulatedRazorpay()
        self._inner = RazorpayExecutor(
            client=self._razorpay,
            # Delivery always succeeds. comms.py still enforces the DLT
            # template and the channel, which is the half that can refuse, and
            # no transport-failure rate is anything this package models.
            comms=CommsSender(deliver=lambda proposal, params: {"delivered": True}),
            registered_templates=template_ids(),
        )
        self.executor = WatchedExecutor(inner=self._inner, facts=self.facts, clock=self.clock)
        self.machine = PolicyMachine(
            clock=self.clock, facts=self.facts, executor=self.executor
        )
        self.store = EventStore(":memory:")
        self._client = TestClient(
            create_app(
                store=self.store,
                webhook_secret=ADVERSARY_WEBHOOK_SECRET,
                pepper=ADVERSARY_PEPPER,
                clock=self.clock,
                machine=self.machine,
                retry_index=self._inner.retry_index,
            )
        )
        self._marks: dict[str, datetime] = {}
        self._deliveries: dict[str, int] = {}
        self._episodes_per_person: dict[str, int] = {}

    # -- time -------------------------------------------------------------
    def now(self) -> datetime:
        return self.clock.now()

    def ist(self, *, day: int = 1, hour: int = 0, minute: int = 0) -> datetime:
        """An instant in the epoch's own month, named in IST.

        Attacks are written against wall-clock IST because that is the
        timezone every rule in the system is stated in — an attack that says
        "18:58" should not have to say it in UTC.
        """
        return datetime(EPOCH.astimezone(IST).year, 9, day, hour, minute, tzinfo=IST).astimezone(
            timezone.utc
        )

    def ist_date(self, day: int) -> date:
        """A calendar day in the epoch's month, as IST reckons it.

        `ist()` returns a UTC instant, so `ist(day=2).date()` is the *first* of
        September — five and a half hours earlier. Every date-shaped rule in
        this system (a promise to pay, `SpendCapGuard`'s reset) is stated in
        IST, so an attack says which IST day it means and this converts.
        """
        return self.ist(day=day).astimezone(IST).date()

    def advance_to(self, target: datetime) -> None:
        """Move time forward, ticking at every instant something falls due.

        A single jump must not skip the rungs of a ladder: the machine only
        acts inside `tick()`, so advancing straight to the target would let a
        retry scheduled in between never happen at all — and an attack whose
        setup silently did not run is the worst possible kind of pass.
        """
        previous: tuple | None = None
        for _ in range(MAX_STEPS):
            pending = self.machine.pending()
            due = min((item.proposal.execute_at for item in pending), default=None)
            if due is None or due > target:
                break
            state = (due, tuple(sorted(item.proposal.proposal_id for item in pending)))
            if state == previous:
                # A tick that changed nothing: the work is held rather than
                # consumed (the merchant kill switch does exactly this). Stop
                # stepping and let the clock run to the target.
                break
            previous = state
            self.clock.advance_to(due)
            self.machine.tick()
        else:
            raise RuntimeError(
                f"hit the {MAX_STEPS}-step cap with work still pending — deferral is "
                "bounded three ways inside PolicyMachine, so this means something "
                "upstream changed"
            )
        self.clock.advance_to(target)
        self.machine.tick()

    def advance_by(self, delta: timedelta) -> None:
        self.advance_to(self.clock.now() + delta)

    def jump_to(self, target: datetime) -> None:
        """Move the clock without letting the agent act.

        For the attacks that turn on two things landing at the same instant.
        `advance_to` ticks at every due moment, so it can never place a webhook
        *at* the moment an action falls due — it would always have executed
        that action first. The world does not owe the agent a tick between two
        webhooks arriving in the same millisecond (docs/VERIFIED.md observed
        exactly that, twice, from two Razorpay IPs), and this is that.
        """
        self.clock.advance_to(target)

    def mark(self, label: str) -> datetime:
        """Stamp this instant so evidence can refer to it afterwards."""
        self._marks[label] = self.clock.now()
        return self._marks[label]

    # -- the world's people and payments ----------------------------------
    def person(
        self,
        human_id: str,
        *,
        contact: str | None = None,
        email: str | None = None,
        zone: timezone | None = None,
        dnd_listed: bool = False,
        is_mandate: bool = False,
        consent: ConsentRecord | None = "default",  # type: ignore[assignment]
    ) -> Person:
        """Register a human. Calling this twice with one `human_id` and two
        emails is attack A07's whole mechanism, not a mistake."""
        contact = contact or self._contact_for(human_id)
        email = email or f"{human_id}@example.invalid"
        customer_id = derive_customer_id(contact, email, pepper=ADVERSARY_PEPPER)
        record = (
            ConsentRecord(
                granted_at=self.EPOCH - timedelta(days=365),
                purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
            )
            if consent == "default"
            else consent
        )
        subject = Person(
            human_id=human_id,
            contact=contact,
            email=email,
            customer_id=customer_id,
            zone=zone,
            dnd_listed=dnd_listed,
            is_mandate=is_mandate,
        )
        self.facts.people[customer_id] = subject
        self.facts.consent[customer_id] = record
        return subject

    @staticmethod
    def _contact_for(human_id: str) -> str:
        digits = int(hashlib.sha256(human_id.encode()).hexdigest()[:8], 16) % 900_000_000
        return f"+91{9_000_000_000 + digits}"

    def fail(
        self,
        person: Person,
        reason: str,
        *,
        source: str | None = None,
        amount_paise: int = DEFAULT_AMOUNT_PAISE,
        entity_id: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> str:
        """A `payment.failed` webhook arrives. Returns the entity_id.

        The envelope comes off disk with only identity stamped on it
        (`windtunnel/payloads.py`), so no attack can author an error string —
        adversarial framing is not an exception to the project's rule.
        """
        entity_id = entity_id or self._entity_id_for(person, reason)
        # Overwritten rather than kept: a second failure on one payment may
        # carry a *different* reason, which is the whole of adversary attacks
        # A15 and A16 (spec §9's A06). The script is the world's latest word
        # about this payment, so a later `fail_last_retry` continues the
        # failure that is current rather than the one it opened on.
        script = Script(
            entity_id=entity_id,
            person=person,
            reason=reason,
            source=source or payloads.source_on_disk(reason),
            amount_paise=amount_paise,
        )
        self.facts.scripts[entity_id] = script
        body = payloads.failure_body(
            reason=script.reason,
            source=script.source,
            entity_id=entity_id,
            contact=person.contact,
            email=person.email,
            amount_paise=amount_paise,
            occurred_at=occurred_at or self.clock.now(),
        )
        self.deliver(body, event_id=event_id or self._event_id_for(entity_id))
        return entity_id

    def fail_last_retry(self, entity_id: str) -> None:
        """The re-presentation the agent just made did not authorise.

        Razorpay fires `payment.failed` for the *new* payment `createRecurring`
        created, so the webhook names an id the policy plane has never seen;
        resolving it back to this episode is production's own job, done by the
        same `from_webhook` the receiver calls through the same `RetryIndex`
        the executor filled. The id is read off the executor's journal, never
        recomputed here — the whole correlation rests on it being Razorpay's
        own id rather than a guess.
        """
        retries = [d for d in self.executor.calls if d.entity_id == entity_id and d.is_retry and d.ok]
        if not retries:
            raise LookupError(f"no dispatched retry on {entity_id} to fail")
        record = self._inner.journal.get(retries[-1].proposal_id)
        if record is None or record.razorpay_request_id is None:
            raise LookupError(f"no Razorpay id recorded for {retries[-1].proposal_id}")
        script = self.facts.scripts[entity_id]
        body = payloads.failure_body(
            reason=script.reason,
            source=script.source,
            entity_id=record.razorpay_request_id,
            contact=script.person.contact,
            email=script.person.email,
            amount_paise=script.amount_paise,
            occurred_at=self.clock.now(),
        )
        self.deliver(body, event_id=self._event_id_for(record.razorpay_request_id))

    def pay_link(self, entity_id: str, *, event_id: str | None = None) -> bool:
        """The customer paid through a link this agent sent.

        The one settlement path with a join key that was not guessed: `_link`
        tags every link it creates with `notes.vasool_entity_id`, and the
        webhook carries it back (vasool/events/settlement.py).
        """
        script = self.facts.scripts[entity_id]
        self.deliver(
            payloads.link_paid_body(entity_id=entity_id, amount_paise=script.amount_paise),
            event_id=event_id or self._event_id_for(f"{entity_id}|link_paid"),
        )
        return self.state_of(entity_id) is State.RECOVERED

    def capture_last_retry(self, entity_id: str) -> bool:
        """The re-presentation the agent just made authorised.

        `payment.captured` for the payment `createRecurring` created, read off
        the executor's journal — the second wired settlement path, correlated
        through the executor's own `RetryIndex` rather than through a guessed
        join key (vasool/events/settlement.py).
        """
        retries = [
            d for d in self.executor.calls if d.entity_id == entity_id and d.is_retry and d.ok
        ]
        if not retries:
            raise LookupError(f"no dispatched retry on {entity_id} to capture")
        record = self._inner.journal.get(retries[-1].proposal_id)
        if record is None or record.razorpay_request_id is None:
            raise LookupError(f"no Razorpay id recorded for {retries[-1].proposal_id}")
        script = self.facts.scripts[entity_id]
        self.deliver(
            payloads.capture_body(
                payment_id=record.razorpay_request_id, amount_paise=script.amount_paise
            ),
            event_id=self._event_id_for(f"{entity_id}|captured"),
        )
        return self.state_of(entity_id) is State.RECOVERED

    def pay_out_of_band(self, entity_id: str) -> bool:
        """The customer paid through a channel this agent cannot see.

        An ordinary `payment.captured` carrying a payment id no `RetryIndex`
        knows and no `vasool_entity_id` anywhere — indistinguishable from any
        other payment on the account (docs/taxonomy.md §9.9). Returns whether
        the settlement correlated, which is the fact A01 rests on.
        """
        script = self.facts.scripts[entity_id]
        self.deliver(
            payloads.capture_body(
                payment_id=SimulatedRazorpay._id("pay_oob_", entity_id),
                amount_paise=script.amount_paise,
            ),
            event_id=self._event_id_for(f"{entity_id}|out_of_band"),
        )
        return self.state_of(entity_id) is State.RECOVERED

    # -- what the world can change about itself ---------------------------
    def withdraw_consent(self, person: Person) -> None:
        """DPDP. Purges queued work and closes the customer's open episodes;
        the ConsentRecord carries the withdrawal too, so `ConsentGuard`
        independently refuses anything arriving later."""
        held = self.facts.consent.get(person.customer_id)
        self.facts.consent[person.customer_id] = (
            dataclasses.replace(held, withdrawn_at=self.clock.now())
            if held is not None
            else ConsentRecord(
                granted_at=self.EPOCH - timedelta(days=365),
                purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
                withdrawn_at=self.clock.now(),
            )
        )
        self.machine.consent_withdrawn(person.customer_id)

    def promise(self, entity_id: str, day: date) -> None:
        self.facts.promises[entity_id] = day

    def set_merchant(self, **changes) -> None:
        self.facts.merchant = dataclasses.replace(self.facts.merchant, **changes)

    def set_registered_templates(self, templates: frozenset[str]) -> None:
        """A DLT registration lapses. Both the guard's view and the executor's
        are updated, because in production both read one config source."""
        self.facts.registered_templates = templates
        self._inner.registered_templates = templates

    def poison_dedupe_oracle(self) -> None:
        """Make the store's read path answer "never seen it", always.

        This is the check-then-act window, won. A dedupe implemented as
        `has_event()` then `append()` consults exactly this oracle, and two
        near-simultaneous deliveries — which docs/VERIFIED.md records as
        normal operation, from two Razorpay IPs inside the same millisecond —
        can both pass it before either has written. Poisoning it deterministic-
        ally is the same test without threads: if the receiver still processes
        one delivery, its dedupe cannot be a check followed by an act.
        """
        self.store.has_event = lambda event_id: False  # type: ignore[method-assign]

    # -- delivery ---------------------------------------------------------
    def deliver(self, body: dict[str, Any], *, event_id: str) -> bool:
        """POST a signed webhook to the real receiver. Returns whether it was
        newly inserted.

        Razorpay signs the compact JSON body — `separators=(",", ":")`
        reproduces every captured signature (docs/VERIFIED.md), so that is
        what is signed and what is sent, byte for byte.
        """
        raw = json.dumps(body, separators=(",", ":")).encode()
        signature = hmac.new(
            ADVERSARY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256
        ).hexdigest()
        response = self._client.post(
            "/webhook",
            content=raw,
            headers={
                "content-type": "application/json",
                "x-razorpay-signature": signature,
                "x-razorpay-event-id": event_id,
            },
        )
        if response.status_code != 200:
            return False
        inserted = not response.json()["duplicate"]
        if inserted and body.get("event") == "payment.failed":
            # The consumer production has not wired — see the module
            # docstring. Only a delivery the receiver accepted as new is
            # observed, which is where webhook-level idempotency actually
            # bites.
            stored = self.store.get(event_id)
            self.machine.observe(stored["failure_event"])
        return inserted

    def _entity_id_for(self, person: Person, reason: str) -> str:
        ordinal = self._episodes_per_person.get(person.customer_id, 0)
        self._episodes_per_person[person.customer_id] = ordinal + 1
        basis = f"{person.customer_id}|{reason}|{ordinal}"
        return "pay_" + hashlib.sha256(basis.encode()).hexdigest()[:14]

    def _event_id_for(self, basis: str) -> str:
        ordinal = self._deliveries.get(basis, 0)
        self._deliveries[basis] = ordinal + 1
        return "evt_" + hashlib.sha256(f"{basis}|{ordinal}".encode()).hexdigest()[:14]

    # -- the Scene protocol -----------------------------------------------
    def state_of(self, entity_id: str) -> State | None:
        return self.machine.state_of(entity_id)

    def ledger(self) -> tuple[Receipt, ...]:
        """The hash-chained receipt ledger — the artefact every §2a claim, and
        every clause of the survival criterion, is scanned from."""
        return tuple(
            build_from_transitions(
                self.machine.transitions,
                call_journal=self._inner.journal,
                trace_id_of=trace_id_for,
            )
        )

    def transitions(self) -> tuple[Transition, ...]:
        return tuple(self.machine.transitions)

    def dispatched(self) -> tuple[Dispatch, ...]:
        return tuple(self.executor.calls)

    def mark_at(self, label: str) -> datetime:
        from windtunnel.adversary.criterion import UnknownMark

        try:
            return self._marks[label]
        except KeyError:
            raise UnknownMark(
                f"{label!r} was never stamped — evidence pointing at a moment the "
                "attack did not mark would otherwise read as a check that passed"
            ) from None

    def subject_for(self, customer_id: str) -> Person | None:
        return self.facts.people.get(customer_id)

    def ledger_digest(self) -> str:
        """SHA-256 over the receipt chain. architectural invariant 5, per attack."""
        basis = [(r.receipt_id, r.prev_hash, r.hash) for r in self.ledger()]
        return hashlib.sha256(
            json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
