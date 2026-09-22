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

import copy
import dataclasses
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from vasool.actions.comms import CommsSender
from vasool.actions.debit import DebitAttempt
from vasool.actions.executor import DocumentedAdapters, RazorpayExecutor, documented_adapters, lost_response
from vasool.actions.notice import NoticeRequest
from vasool.actions.status import NullStatusCheck, StatusReading
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import Proposal, template_ids
from vasool.diagnosis.rules import IST
from vasool.events.receiver import create_app
from vasool.events.schemas import derive_customer_id
from vasool.identity.resolver import UnionFindResolver
from vasool.events.store import EventStore
from vasool.mandate.evidence import Observation, apply_rail_evidence
from vasool.mandate.machine import MandateMachine, Trigger
from vasool.mandate.record import MandateCategory, MandateRail, MandateRecord
from vasool.mandate.states import MandateState
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
from vasool.actions.reconcile import NullSettlementLookup
from windtunnel.runner import SimulatedSettlementLookup
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
    mandate: MandateRecord | None = None
    """The mandate this person's debits are presented under, if any."""


@dataclass(frozen=True, slots=True)
class Script:
    """What the world decided about one payment before the agent saw it."""

    entity_id: str
    person: Person
    reason: str
    source: str | None
    amount_paise: int
    upi: bool = False
    """A UPI Autopay debit, whose failure arrives in the UPI envelope Razorpay
    documents (windtunnel/payloads.py) rather than a card one."""


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
    """Keyed by identity, not by customer id — the cap counts humans
    (docs/EVALUATION.md §10, 2026-09-17). A07 is the attack that made the
    difference visible and A11 is the control that says the fix did not simply
    move the failure."""

    identities: UnionFindResolver = field(
        default_factory=lambda: UnionFindResolver(ADVERSARY_PEPPER)
    )
    spent: dict[tuple[str, date], int] = field(default_factory=dict)
    registered_templates: frozenset[str] = field(default_factory=template_ids)
    mandate_log: list[Observation] = field(default_factory=list)
    """What each failed debit's rail evidence did to a mandate, disagreements
    included (vasool/mandate/evidence.py). The world holds the record, so the
    world keeps its log."""

    def mandate_for(self, entity_id: str) -> MandateRecord | None:
        """The mandate a payment is presented under, as the world holds it now —
        read through the person, because a mandate event replaces the person."""
        script = self.scripts.get(entity_id)
        if script is None:
            return None
        person = self.people.get(script.person.customer_id)
        return person.mandate if person is not None else None

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
            contact_history=tuple(sorted(self.contacts.get(self.identity_of(person), ()))),
            identity_id=self.identity_of(person),
            consent=self.consent.get(person.customer_id),
            dnd_listed=person.dnd_listed,
            dnd_checked_at=now,
            promise_to_pay=self.promises.get(event.entity_id),
            mandate=person.mandate,
            pre_debit_notice_sent_at=self.notices.get(event.entity_id),
            registered_templates=self.registered_templates,
            # The world has always known where these people are; until A08 was
            # fixed, nothing handed that fact to the guards. None still means
            # unknown, and unknown still resolves to IST.
            customer_zone=person.zone,
        )

    def identity_of(self, person: Person) -> str:
        """Which human this record belongs to, as the resolver answers now.

        Asked rather than cached: an arena builds its people one call at a
        time, and a second record can merge two sets that were separate when
        the first was added."""
        return self.identities.identity_for(person.contact, person.email)

    def record(self, proposal: Proposal, *, at: datetime) -> None:
        """Fold one executed action back into the world, synchronously.

        From inside the executor seam rather than at the end of a tick: two
        contacts gated in the same tick must not both pass the frequency cap
        on a snapshot neither of them appears in.
        """
        if proposal.is_contact:
            who = self.identity_of(self.people[proposal.customer_id])
            self.contacts.setdefault(who, []).append(at)
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


class SimulatedRail:
    """The rail a pre-debit notice request goes to, accepting every request.

    The notice itself is the issuer's to send (vasool/actions/notice.py); what
    the world needs to know is that it was asked for, which `WorldFactStore`
    records when the request executes. No refusal rate is registered in §4, so
    none is modelled. The reference is derived from the idempotency key, for
    the same replay reason as `SimulatedRazorpay`'s ids.
    """

    def request(self, proposal: Proposal) -> NoticeRequest:
        reference = "rvc_" + hashlib.sha256(proposal.idempotency_key.encode()).hexdigest()[:14]
        return NoticeRequest(ok=True, detail="the simulated rail accepted the request", reference=reference)


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


class SimulatedDebiter:
    """The rail a debit goes to when the arena is not playing Razorpay's
    documented surface: it takes the debit and answers with an id derived
    from the idempotency key — the id, and the response shape, the arena
    answered with before the debit became a port, so every attack's ledger
    replays byte for byte across that change (windtunnel/runner.py has the
    same class for the same reason)."""

    def debit(self, proposal: Proposal) -> DebitAttempt:
        payment_id = SimulatedRazorpay._id("pay_", proposal.idempotency_key)
        return DebitAttempt(ok=True, detail="debit requested", payment_id=payment_id, response={"id": payment_id})


class ArenaRail:
    """Razorpay's documented surface, played by the world, for a mandate that
    carries a Razorpay token.

    Shaped like the SDK the real `RazorpayClient` wraps — `order.create`,
    `order.fetch`, `customer.fetch`, `payment.createRecurring` — so that the
    client, its transport rules and the documented adapters all run for real
    (vasool/actions/executor.py, `documented_adapters`). The rail takes every
    debit it is sent and marks the order paid; an attack that wants a
    response lost says so, and the rail then takes the debit and loses the
    answer, exactly as a network does. It counts every debit it takes, which
    is what `RailDebitsAtMost` reads: a debit the client re-sent is in no
    record the agent keeps, and only the rail can count it."""

    def __init__(self, facts: ArenaFacts, clock: VirtualClock) -> None:
        self._facts = facts
        self._clock = clock
        self._orders: dict[str, dict] = {}
        self._lose: set[str] = set()
        self.debits: list[tuple[str, str, datetime]] = []
        self.order = _RailOrders(self)
        self.customer = _RailCustomers(self)
        self.payment = _RailPayments(self)

    def lose_next_response(self, entity_id: str) -> None:
        self._lose.add(entity_id)

    def debits_for(self, entity_id: str) -> int:
        return sum(1 for entity, _, _ in self.debits if entity == entity_id)

    @staticmethod
    def _key(headers: dict | None) -> str:
        return (headers or {}).get("X-Razorpay-Idempotency-Key", "")


class _RailOrders:
    def __init__(self, rail: ArenaRail) -> None:
        self._rail = rail

    def create(self, data: dict, **kwargs) -> dict:
        order_id = SimulatedRazorpay._id("order_", ArenaRail._key(kwargs.get("headers")))
        delivered = int(self._rail._clock.now().timestamp())
        self._rail._orders.setdefault(order_id, {
            "id": order_id,
            "entity": "order",
            "amount": data["amount"],
            "amount_paid": 0,
            "amount_due": data["amount"],
            "currency": data["currency"],
            "status": "created",
            "attempts": 0,
            "notification": {
                "token_id": data["notification"]["token_id"],
                "id": SimulatedRazorpay._id("notification_", order_id),
                "status": "delivered",
                "delivered_at": delivered,
            },
            "notes": data.get("notes", {}),
        })
        return copy.deepcopy(self._rail._orders[order_id])

    def fetch(self, order_id: str, **kwargs) -> dict:
        return copy.deepcopy(self._rail._orders[order_id])


class _RailCustomers:
    def __init__(self, rail: ArenaRail) -> None:
        self._rail = rail

    def fetch(self, customer_id: str, **kwargs) -> dict:
        for person in self._rail._facts.people.values():
            if person.mandate is not None and person.mandate.razorpay_customer_id == customer_id:
                return {"id": customer_id, "email": person.email, "contact": person.contact}
        raise LookupError(f"no customer {customer_id} at the rail")


class _RailPayments:
    def __init__(self, rail: ArenaRail) -> None:
        self._rail = rail

    def createRecurring(self, data: dict, **kwargs) -> dict:  # noqa: N802 — the SDK's name
        rail = self._rail
        entity_id = data["notes"]["vasool_entity_id"]
        order = rail._orders[data["order_id"]]
        order["attempts"] += 1
        order["status"], order["amount_paid"], order["amount_due"] = "paid", data["amount"], 0
        rail.debits.append((entity_id, data["order_id"], rail._clock.now()))
        payment_id = SimulatedRazorpay._id("pay_", ArenaRail._key(kwargs.get("headers")))
        if entity_id in rail._lose:
            rail._lose.discard(entity_id)
            raise lost_response(f"the rail took {payment_id} and the response never arrived")
        return {"razorpay_payment_id": payment_id}


@dataclass
class _ByMandate:
    """Routes a proposal to Razorpay's documented adapters when its payment's
    mandate carries a Razorpay token, and to the simulated ones otherwise — so
    only a scene that gives someone a token plays the documented surface, and
    every other attack's ledger is untouched by its existence."""

    facts: ArenaFacts
    documented: DocumentedAdapters

    def _documented(self, proposal: Proposal) -> bool:
        mandate = self.facts.mandate_for(proposal.entity_id)
        return mandate is not None and mandate.token_id is not None


class _Notifier(_ByMandate):
    def request(self, proposal: Proposal) -> NoticeRequest:
        if self._documented(proposal):
            return self.documented.notifier.request(proposal)
        return SimulatedRail().request(proposal)


class _Debiter(_ByMandate):
    def debit(self, proposal: Proposal) -> DebitAttempt:
        if self._documented(proposal):
            return self.documented.debiter.debit(proposal)
        return SimulatedDebiter().debit(proposal)


class _StatusCheck(_ByMandate):
    def check(self, proposal: Proposal) -> StatusReading:
        if self._documented(proposal):
            return self.documented.status.check(proposal)
        return NullStatusCheck().check(proposal)


class Arena:
    """One attack's world. Implements `criterion.Scene`."""

    EPOCH = EPOCH
    """The same calendar anchor the wind tunnel uses, so an attack's
    timestamps read against the same September as every other artefact."""

    def __init__(self, *, reconciles: bool = True) -> None:
        """`reconciles=False` is the shipped default of a deployment that has
        wired no settlement lookup — which is every deployment until a merchant
        decides to give the agent read access to their payment stream. A01 is
        closed only for those that have; A27 is the same attack against those
        that have not, and it is registered to fail."""
        self._reconciles = reconciles
        self.clock = VirtualClock(self.EPOCH)
        self.facts = ArenaFacts(merchant=MerchantPolicy(merchant_id=MERCHANT_ID))
        self._razorpay = SimulatedRazorpay()
        self.rail = ArenaRail(self.facts, self.clock)
        documented = documented_adapters(sdk_client=self.rail, mandates=self.facts.mandate_for)
        self._inner = RazorpayExecutor(
            client=self._razorpay,
            # Delivery always succeeds. comms.py still enforces the DLT
            # template and the channel, which is the half that can refuse, and
            # no transport-failure rate is anything this package models.
            comms=CommsSender(deliver=lambda proposal, params: {"delivered": True}),
            registered_templates=template_ids(),
            notifier=_Notifier(self.facts, documented),
            debiter=_Debiter(self.facts, documented),
            status=_StatusCheck(self.facts, documented),
        )
        self.executor = WatchedExecutor(inner=self._inner, facts=self.facts, clock=self.clock)
        # What the merchant's account shows. An attack that pays out of band
        # records the payment here, and the agent may consult it exactly as a
        # deployment with a wired lookup would — which is the whole of A01's
        # fix and the only way the attack can be scored against it.
        self.settlement = SimulatedSettlementLookup()
        self.machine = PolicyMachine(
            clock=self.clock,
            facts=self.facts,
            executor=self.executor,
            settlement=self.settlement if self._reconciles else NullSettlementLookup(),
            ours=lambda: self._inner.retry_index.payment_ids(),
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
        mandate: MandateRecord | None = None,
        consent: ConsentRecord | None = "default",  # type: ignore[assignment]
    ) -> Person:
        """Register a human. Calling this twice with one `human_id` and two
        emails is attack A07's whole mechanism, not a mistake.

        `is_mandate` gives them the mandate the simulator gives every mandate
        customer — card, general category, active, valid long past the arena
        (windtunnel/world.py::simulated_mandate); `mandate` names one exactly,
        for an attack about a particular rail, category or state."""
        if mandate is None and is_mandate:
            mandate = MandateRecord(
                mandate_id=f"arena_mandate_{human_id}",
                rail=MandateRail.CARD,
                category=MandateCategory.GENERAL,
                state=MandateState.ACTIVE,
                valid_until=self.EPOCH + timedelta(days=365),
            )
        contact = contact or self._contact_for(human_id)
        email = email or f"{human_id}@example.invalid"
        customer_id = derive_customer_id(contact, email, pepper=ADVERSARY_PEPPER)
        self.facts.identities.add(contact, email)
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
            mandate=mandate,
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
        upi: bool = False,
    ) -> str:
        """A `payment.failed` webhook arrives. Returns the entity_id.

        `upi` delivers it in the UPI Autopay envelope, carrying one of the 61
        reasons Razorpay documents for a failed subsequent UPI payment
        (docs/taxonomy.md §12); every other failure arrives as a card one.

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
            source=None if upi else (source or payloads.source_on_disk(reason)),
            amount_paise=amount_paise,
            upi=upi,
        )
        self.facts.scripts[entity_id] = script
        body = self._failure_body(script, entity_id=entity_id, occurred_at=occurred_at or self.clock.now())
        self.deliver(body, event_id=event_id or self._event_id_for(entity_id))
        return entity_id

    @staticmethod
    def _failure_body(script: Script, *, entity_id: str, occurred_at: datetime) -> dict[str, Any]:
        if script.upi:
            return payloads.upi_failure_body(
                reason=script.reason,
                entity_id=entity_id,
                contact=script.person.contact,
                email=script.person.email,
                amount_paise=script.amount_paise,
                occurred_at=occurred_at,
            )
        return payloads.failure_body(
            reason=script.reason,
            source=script.source,
            entity_id=entity_id,
            contact=script.person.contact,
            email=script.person.email,
            amount_paise=script.amount_paise,
            occurred_at=occurred_at,
        )

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
        body = self._failure_body(script, entity_id=record.razorpay_request_id, occurred_at=self.clock.now())
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
        payment_id = SimulatedRazorpay._id("pay_oob_", entity_id)
        # The money is in the merchant's account from this moment, whether or
        # not any webhook correlates it — which is exactly A01's point.
        self.settlement.record(
            customer_id=script.person.customer_id,
            payment_id=payment_id,
            amount_paise=script.amount_paise,
            at=self.clock.now(),
        )
        self.deliver(
            payloads.capture_body(
                payment_id=payment_id,
                amount_paise=script.amount_paise,
            ),
            event_id=self._event_id_for(f"{entity_id}|out_of_band"),
        )
        return self.state_of(entity_id) is State.RECOVERED

    def lose_next_debit_response(self, entity_id: str) -> None:
        """The rail will take this payment's next debit and lose the answer.

        Only a payment whose mandate carries a Razorpay token reaches the
        arena's rail (see `_ByMandate`); for any other, the debit never gets
        there and there is nothing to lose. The money moves either way the
        response goes — a lost response is not a failed debit, which is the
        whole of attack A26."""
        self.rail.lose_next_response(entity_id)

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

    def mandate_event(self, person: Person, trigger: Trigger, **changes: object) -> Person:
        """Something happens to a person's mandate — they pause or revoke it,
        the merchant cancels it — at the arena's clock.

        Through `MandateMachine`, never by editing the record, so a scene can
        only do to a mandate what the cited lifecycle permits: pausing a card
        mandate or a payer revoking a non-revocable one raises here exactly as
        it would anywhere else. Nothing is purged from the queue — a debit
        built before the change meets `MandateStateGuard` when it comes due,
        and the receipt says so.
        """
        if person.mandate is None:
            raise ValueError(f"{person.human_id} holds no mandate")
        record, _ = MandateMachine().apply(person.mandate, trigger, at=self.clock.now(), **changes)
        updated = dataclasses.replace(person, mandate=record)
        self.facts.people[person.customer_id] = updated
        return updated

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
            self._take_rail_evidence(stored["failure_event"])
            self.machine.observe(stored["failure_event"])
        return inserted

    def _take_rail_evidence(self, event) -> None:
        """A UPI debit that failed because the mandate was revoked, paused or
        expired moves the world's record of it, before the agent reads the
        failure — the world holds the record (vasool/mandate/evidence.py)."""
        if event.method != "upi":
            return
        person = self.facts.people.get(event.customer_id)
        if person is None or person.mandate is None:
            return
        updated, observation = apply_rail_evidence(person.mandate, event.error_reason, at=self.clock.now())
        if observation is not None:
            self.facts.mandate_log.append(observation)
        if updated is not person.mandate:
            self.facts.people[person.customer_id] = dataclasses.replace(person, mandate=updated)

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

    def rail_debits(self, entity_id: str) -> int:
        return self.rail.debits_for(entity_id)

    def ledger_digest(self) -> str:
        """SHA-256 over the receipt chain. architectural invariant 5, per attack."""
        basis = [(r.receipt_id, r.prev_hash, r.hash) for r in self.ledger()]
        return hashlib.sha256(
            json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
