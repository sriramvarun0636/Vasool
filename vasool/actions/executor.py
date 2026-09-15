"""The executor. The only module besides razorpay_client.py permitted to
touch the Razorpay SDK, and the only one permitted to import
razorpay_client.py at all (tests/test_actions_boundary.py enforces both).

Implements the `Executor` protocol vasool.policy.machine.PolicyMachine
already talks to — RecordingExecutor is stage 3's stand-in for exactly this
class, so wiring this in is a one-line change at the call site and nothing
upstream needs to know the difference.

One method per (InterventionType, role) pair that can actually reach here.
HUMAN_QUEUE never does: vasool/policy/machine.py::_gate routes it straight to
ESCALATED without ever calling execute(), because a human queue is a handoff,
not an action this module performs (see that module's comment on the same
point). Receiving one here anyway is the programming error the session brief
names, not a silent no-op — `_dispatch` raises rather than swallowing it.

Also owns RetryIndex: `_retry` records the id of the payment the debit
created, from the rail's own answer. vasool/events/settlement.py reads it back
to correlate a later `payment.captured` webhook to the episode it closes — see
RetryIndex's own docstring for what that does and does not guarantee.

**Three calls go through ports, and each port's default refuses.** The mandate
debit (vasool/actions/debit.py), the pre-debit notice (vasool/actions/notice.py)
and the status check (vasool/actions/status.py) have never been observed on
this account. Razorpay documents all three, and the documented adapters are at
the bottom of this module — here, because this is the one module the action
plane's boundary lets call the client (tests/test_actions_boundary.py). They are
wired only by `build_documented`, never by default (docs/EVALUATION.md §10,
2026-09-15).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from vasool.actions.comms import CommsRefused, CommsSender
from vasool.actions.debit import DebitAttempt, MandateDebiter, MandateSource, NullMandateDebiter
from vasool.actions.notice import NoticeOrders, NoticeRequest, NullPreDebitNotifier, PreDebitNotifier
from vasool.actions.razorpay_client import RazorpayCallFailed, RazorpayClient, RazorpayConfig
from vasool.actions.status import NullStatusCheck, StatusCheck, StatusReading
from vasool.diagnosis.proposal import Channel, Proposal, ProposalRole
from vasool.diagnosis.taxonomy import InterventionType
from vasool.policy.machine import ExecutionResult, RailStatus

log = logging.getLogger(__name__)


class UnroutableProposal(Exception):
    """A proposal whose intervention/role this executor has no method for.

    InterventionType is a closed enum (vasool/diagnosis/taxonomy.py) — every
    member has to be argued into the taxonomy document before it exists in
    code — so reaching this branch means either a HUMAN_QUEUE proposal
    escaped the machine's escalation path, or the taxonomy grew a member this
    module was never taught to execute. Either way it is a bug to surface,
    not an action to skip quietly: a proposal the ledger never hears about is
    indistinguishable from one that succeeded.
    """


@dataclass(frozen=True, slots=True)
class RazorpayCallRecord:
    """What actually happened when this executor called Razorpay for one
    proposal.

    Kept separate from ExecutionResult (vasool/policy/machine.py), which
    carries only what the policy plane acts on — ok, whether a debit's outcome
    is unknown, and what a status check found — and never Razorpay-shaped
    data. vasool/ledger/receipts.py reads this journal structurally (it declares
    its own Protocol shape, not an import of this class) to attach
    razorpay_request_id / razorpay_response to a receipt without the policy
    plane ever needing to carry Razorpay-shaped data.
    """

    proposal_id: str
    ok: bool
    detail: str
    razorpay_request_id: str | None = None
    razorpay_response: dict | None = None
    outcome_unknown: bool = False
    """A debit that may have moved money though its call failed — the receipt
    says OUTCOME_UNKNOWN rather than EXECUTION_FAILED."""

    status: RailStatus | None = None
    settled_amount_paise: int | None = None


class ExecutionJournal:
    """Where RazorpayExecutor remembers what it did, keyed by proposal_id.
    vasool/ledger/receipts.py reads this; nothing else should need to."""

    def __init__(self) -> None:
        self._by_proposal: dict[str, RazorpayCallRecord] = {}

    def record(self, call: RazorpayCallRecord) -> None:
        self._by_proposal[call.proposal_id] = call

    def get(self, proposal_id: str) -> RazorpayCallRecord | None:
        return self._by_proposal.get(proposal_id)


class RetryIndex:
    """Where RazorpayExecutor remembers which entity_id a retried payment id
    belongs to. vasool/events/settlement.py reads this to correlate a later
    `payment.captured` webhook back to the episode a SILENT_RETRY/TIMED_RETRY
    was for — see that module's `entity_id_from_payment_captured` for why
    this is a non-guessed join key: the debit returns the rail's own id for
    the payment it just created, and this is that same id recorded
    against the entity_id that asked for it. Nothing inferred, nothing
    matched by order_id/amount/customer.

    **In-memory and process-local, deliberately not persisted here.** Nothing
    in this codebase durably stores the action plane's own call history yet —
    ExecutionJournal beside this class has exactly the same property, and the
    only durable store that exists at all (EventStore) is scoped to *received*
    webhooks, not calls we made ourselves; reusing it for this would distort
    what it's for. The transition log is the other candidate and it is ruled
    out on purpose too: vasool/ledger/receipts.py's own docstring establishes
    that Razorpay-shaped data (a request id, a response body) deliberately
    never enters the policy plane, so it must not carry this either. So: a
    process restart between a retry firing and its `payment.captured`
    arriving loses the mapping. That capture will not be recognised as ours —
    the episode simply stays in AWAITING rather than reaching RECOVERED
    through this path. Not silently wrong (nothing gets settled that
    shouldn't), but a real gap, not a theoretical one, and durability for the
    whole action/ledger plane is a bigger change than this session's scope.
    """

    def __init__(self) -> None:
        self._by_payment_id: dict[str, str] = {}

    def record(self, payment_id: str, entity_id: str) -> None:
        self._by_payment_id[payment_id] = entity_id

    def entity_id_for(self, payment_id: str) -> str | None:
        return self._by_payment_id.get(payment_id)


def _medium_for(channel: Channel) -> str:
    if channel is Channel.WHATSAPP:
        # VERIFY: payment_link.notifyBy supports "sms" and "email" only —
        # there is no WhatsApp medium in the SDK. Unreachable today because
        # DEFAULT_CHANNEL (vasool/diagnosis/proposal.py) is always SMS, but a
        # future channel-selection feature must not silently fall through to
        # the wrong medium.
        raise UnroutableProposal("no notify_payment_link medium for WHATSAPP")
    return "sms" if channel is Channel.SMS else "email"


@dataclass
class RazorpayExecutor:
    """One executor per merchant — `registered_templates` is that merchant's
    current DLT registration, sourced from the same place PolicyFacts is
    (the project's own allergy to a second, quietly diverging copy of a record
    applies here too: this is data flowing from one config source at wiring
    time, not an independent registry)."""

    client: RazorpayClient
    comms: CommsSender
    registered_templates: frozenset[str]
    journal: ExecutionJournal = field(default_factory=ExecutionJournal)
    retry_index: RetryIndex = field(default_factory=RetryIndex)
    notifier: PreDebitNotifier = field(default_factory=NullPreDebitNotifier)
    """Where a pre-debit notice is requested. Not comms: the issuer sends the
    notice, and the merchant only asks the rail for it (vasool/actions/notice.py).
    The default refuses, because no such request is wired."""

    debiter: MandateDebiter = field(default_factory=NullMandateDebiter)
    """Where a SILENT_RETRY or TIMED_RETRY reaches the rail
    (vasool/actions/debit.py). The default refuses, because no debit call has
    been observed on this account."""

    status: StatusCheck = field(default_factory=NullStatusCheck)
    """Where a STATUS_CHECK asks the rail what happened
    (vasool/actions/status.py). The default cannot tell, and a person decides."""

    def execute(self, proposal: Proposal) -> ExecutionResult:
        record = self._dispatch(proposal)
        self.journal.record(record)
        return ExecutionResult(
            ok=record.ok,
            detail=record.detail,
            outcome_unknown=record.outcome_unknown,
            status=record.status,
            settled_amount_paise=record.settled_amount_paise,
        )

    def _dispatch(self, proposal: Proposal) -> RazorpayCallRecord:
        if proposal.intervention is InterventionType.HUMAN_QUEUE:
            raise UnroutableProposal(
                "HUMAN_QUEUE never reaches an executor — the state machine "
                "escalates it before execute() is called (see module docstring)"
            )
        if proposal.role is ProposalRole.PRE_DEBIT_NOTICE:
            return self._notify(proposal)
        if proposal.role is ProposalRole.NUDGE:
            return self._send(proposal)
        if proposal.intervention is InterventionType.STATUS_CHECK:
            return self._check(proposal)
        if proposal.intervention in (InterventionType.SILENT_RETRY, InterventionType.TIMED_RETRY):
            return self._retry(proposal)
        if proposal.intervention in (InterventionType.REATTEMPT_LINK, InterventionType.REAUTH_LINK):
            return self._link(proposal)
        raise UnroutableProposal(
            f"no executor method for {proposal.intervention.value} / {proposal.role.value}"
        )

    def _retry(self, proposal: Proposal) -> RazorpayCallRecord:
        attempt = self.debiter.debit(proposal)
        if not attempt.ok:
            log.warning("retry not taken for %s: %s", proposal.proposal_id, attempt.detail)
            return RazorpayCallRecord(
                proposal.proposal_id,
                ok=False,
                detail=attempt.detail,
                outcome_unknown=attempt.outcome_unknown,
            )
        razorpay_payment_id = attempt.payment_id
        if razorpay_payment_id is not None:
            self.retry_index.record(razorpay_payment_id, proposal.entity_id)
        return RazorpayCallRecord(
            proposal.proposal_id,
            ok=True,
            detail="retry dispatched",
            razorpay_request_id=razorpay_payment_id,
            razorpay_response=attempt.response,
        )

    def _link(self, proposal: Proposal) -> RazorpayCallRecord:
        try:
            link = self.client.create_payment_link(
                amount_paise=proposal.amount_paise,
                currency="INR",
                description=proposal.rationale,
                notes={
                    "vasool_proposal_id": proposal.proposal_id,
                    "vasool_entity_id": proposal.entity_id,
                    # vasool_entity_id is what vasool/events/settlement.py reads
                    # off the payment_link.paid webhook this link eventually
                    # fires, to close the recovery episode it belongs to. notes
                    # is merchant-supplied metadata, not a Razorpay-authored
                    # field, so setting a second key on it is not inventing
                    # anything Razorpay says — it is data we attach and expect
                    # echoed back, the same way vasool_proposal_id already was.
                },
                idempotency_key=proposal.idempotency_key,
            )
        except RazorpayCallFailed as exc:
            log.warning("link creation failed for %s: %s", proposal.proposal_id, exc)
            return RazorpayCallRecord(proposal.proposal_id, ok=False, detail=str(exc))

        try:
            self._send(proposal, link=link)
        except CommsRefused as exc:
            # The link exists on Razorpay's side even though we failed to
            # tell the customer about it — worth keeping in the receipt.
            return RazorpayCallRecord(
                proposal.proposal_id,
                ok=False,
                detail=f"link created but not sent: {exc}",
                razorpay_request_id=link.get("id"),
                razorpay_response=link,
            )
        return RazorpayCallRecord(
            proposal.proposal_id,
            ok=True,
            detail="link created and sent",
            razorpay_request_id=link.get("id"),
            razorpay_response=link,
        )

    def _notify(self, proposal: Proposal) -> RazorpayCallRecord:
        """Ask the rail for the pre-debit notification. Never through comms:
        a notice the issuer sends has no DLT template of ours to carry."""
        request = self.notifier.request(proposal)
        if not request.ok:
            log.warning("pre-debit notice not requested for %s: %s", proposal.proposal_id, request.detail)
        return RazorpayCallRecord(proposal.proposal_id, ok=request.ok, detail=request.detail)

    def _check(self, proposal: Proposal) -> RazorpayCallRecord:
        """Ask the rail whether the debit happened. A reading that could not be
        had is not a failed action — it is recorded as not ok so the receipt
        says the check told us nothing, and the state machine escalates."""
        reading = self.status.check(proposal)
        return RazorpayCallRecord(
            proposal.proposal_id,
            ok=reading.answer is not RailStatus.CANNOT_TELL,
            detail=reading.detail,
            razorpay_request_id=reading.reference,
            status=reading.answer,
            settled_amount_paise=reading.amount_paise,
        )

    def _send(self, proposal: Proposal, *, link: dict | None = None) -> RazorpayCallRecord:
        params = {"link": link["short_url"], "payment_link_id": link["id"]} if link else {}
        self.comms.send(proposal=proposal, registered_templates=self.registered_templates, params=params)
        return RazorpayCallRecord(proposal.proposal_id, ok=True, detail="sent")

    @classmethod
    def build_documented(
        cls,
        *,
        client: RazorpayClient,
        registered_templates: frozenset[str],
        mandates: MandateSource,
    ) -> RazorpayExecutor:
        """An executor whose debit, notice and status check are Razorpay's
        documented calls rather than the refusing defaults.

        **Not the default, and nothing in this repository wires it into a
        run.** None of the three calls has been observed on this account —
        subscriptions and UPI are unavailable before activation
        (docs/VERIFIED.md) — and the registered rule is that production keeps
        the refusing adapters until one live call has been seen
        (docs/EVALUATION.md §10, 2026-09-15). This exists so the documented
        contract is code that tests can hold to the documentation, instead of
        prose.
        """
        executor = cls.build(client=client, registered_templates=registered_templates)
        adapters = _adapters(client, mandates)
        executor.notifier, executor.debiter, executor.status = (
            adapters.notifier,
            adapters.debiter,
            adapters.status,
        )
        return executor

    @classmethod
    def from_env(cls, *, registered_templates: frozenset[str]) -> RazorpayExecutor:
        """A live executor, credentials from RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET
        via RazorpayConfig.from_env() (the project rules: read secrets from the
        environment, never hardcode one).

        This is the one seam a caller outside actions/ may use to get a live
        client wired up at all: tests/test_actions_boundary.py restricts
        razorpay_client.py's own module to being reached from here, so
        vasool/demo.py cannot construct a RazorpayClient or a RazorpayConfig
        itself. Raises RuntimeError (from RazorpayConfig.from_env()) if the
        credentials aren't set — a plain builtin, not something reaching this
        method requires knowing about.
        """
        return cls.build(client=RazorpayClient(config=RazorpayConfig.from_env()), registered_templates=registered_templates)

    @classmethod
    def build(cls, *, client: RazorpayClient, registered_templates: frozenset[str]) -> RazorpayExecutor:
        """Construct an executor with the default deliverer: a payment-link
        message goes out through Razorpay's own notifyBy, anything else has
        no transport yet (see comms.py's Deliverer VERIFY note). The common
        case, where nothing outside actions/ needs to see the seam. Tests
        that want to fake delivery construct RazorpayExecutor directly with a
        CommsSender of their own instead.
        """

        def deliver(proposal: Proposal, params: dict) -> dict:
            payment_link_id = params.get("payment_link_id")
            if payment_link_id is not None:
                return client.notify_payment_link(
                    payment_link_id=payment_link_id,
                    medium=_medium_for(proposal.channel),
                    idempotency_key=proposal.idempotency_key,
                )
            log.warning(
                "no delivery channel wired for %s (role=%s) — comms.py "
                "enforced the template; nothing sends it yet",
                proposal.proposal_id,
                proposal.role.value,
            )
            return {"delivered": False, "reason": "no transport wired for non-payment-link messages"}

        return cls(client=client, comms=CommsSender(deliver=deliver), registered_templates=registered_templates)


# ---------------------------------------------------------------------------
# Razorpay's documented calls, behind the three ports. Wired only by
# `RazorpayExecutor.build_documented`. Every one is documentation, not
# observation: Razorpay, *Create Subsequent Payments* (UPI, cards), read from
# the markdown source on 2026-09-15 and pinned by SHA-256 in
# docs/EVALUATION.md §10 of that date.
# ---------------------------------------------------------------------------
def _episode_notes(proposal: Proposal) -> dict:
    """The same merchant metadata `_link` stamps on a payment link, so a
    payment created from an order carries its episode back with it."""
    return {"vasool_proposal_id": proposal.proposal_id, "vasool_entity_id": proposal.entity_id}


@dataclass
class RazorpayPreDebitNotifier:
    """The pre-debit notice as Razorpay documents it: an order created with a
    `notification` object carrying the mandate's `token_id`. Razorpay delivers
    the notice; the order then reports `notification.delivered_at`, which is
    the time `PreDebitNoticeGuard` should count from, read by whatever
    production FactStore reads orders."""

    client: RazorpayClient
    mandates: MandateSource
    orders: NoticeOrders

    def request(self, proposal: Proposal) -> NoticeRequest:
        mandate = self.mandates(proposal.entity_id)
        if mandate is None or mandate.token_id is None:
            return NoticeRequest(
                ok=False,
                detail="no mandate token on record for this payment; nothing to notify against",
            )
        try:
            order = self.client.create_notice_order(
                amount_paise=proposal.amount_paise,
                currency="INR",
                token_id=mandate.token_id,
                notes=_episode_notes(proposal),
                idempotency_key=proposal.idempotency_key,
            )
        except RazorpayCallFailed as exc:
            return NoticeRequest(ok=False, detail=str(exc))
        self.orders.record(proposal.entity_id, order["id"])
        return NoticeRequest(
            ok=True,
            detail="order created with a notification object; Razorpay delivers the notice",
            reference=order["id"],
        )


@dataclass
class RazorpayMandateDebiter:
    """A mandate debit as Razorpay documents it: `createRecurring` with its
    eight mandatory fields, against the order the notice was requested on.

    Refuses rather than guessing when anything a documented call needs is
    missing: a payment on no mandate (a one-time payment has no token, and
    nothing documented re-presents one), a mandate with no Razorpay token or
    customer, or no notified order. The customer's email and contact are read
    from Razorpay's customer entity for the call and never kept."""

    client: RazorpayClient
    mandates: MandateSource
    orders: NoticeOrders

    def debit(self, proposal: Proposal) -> DebitAttempt:
        mandate = self.mandates(proposal.entity_id)
        if mandate is None:
            return DebitAttempt(
                ok=False,
                detail="a one-time payment has no token to present again; nothing documented re-presents it",
            )
        if mandate.token_id is None or mandate.razorpay_customer_id is None:
            return DebitAttempt(ok=False, detail="the mandate carries no Razorpay token or customer id")
        order_id = self.orders.order_for(proposal.entity_id)
        if order_id is None:
            return DebitAttempt(
                ok=False,
                detail="no order was notified for this debit; a debit is presented against its notice's order",
            )
        try:
            customer = self.client.fetch_customer(mandate.razorpay_customer_id)
            response = self.client.create_recurring_payment(
                email=customer["email"],
                contact=customer["contact"],
                amount_paise=proposal.amount_paise,
                currency="INR",
                order_id=order_id,
                customer_id=mandate.razorpay_customer_id,
                token_id=mandate.token_id,
                notes=_episode_notes(proposal),
                idempotency_key=proposal.idempotency_key,
            )
        except RazorpayCallFailed as exc:
            return DebitAttempt(ok=False, detail=str(exc), outcome_unknown=exc.outcome_unknown)
        return DebitAttempt(
            ok=True,
            detail="debit requested",
            payment_id=response.get("razorpay_payment_id"),
            response=response,
        )


@dataclass
class RazorpayOrderStatusCheck:
    """A status check read off the documented order entity, whose `status` is
    `created` ("till a payment is attempted on it"), `attempted` or `paid`
    ("After the successful capture of the payment").

    `paid` is DEBITED. `created` with no attempts is NOT_DEBITED. `attempted`
    is PENDING: the order alone cannot tell a failed attempt from one still in
    flight, so it is asked again rather than read as either. No order, or a
    call that fails, cannot tell."""

    client: RazorpayClient
    orders: NoticeOrders

    def check(self, proposal: Proposal) -> StatusReading:
        order_id = self.orders.order_for(proposal.entity_id)
        if order_id is None:
            return StatusReading(RailStatus.CANNOT_TELL, "no order on record for this payment")
        try:
            order = self.client.fetch_order(order_id)
        except RazorpayCallFailed as exc:
            return StatusReading(RailStatus.CANNOT_TELL, str(exc), reference=order_id)
        state = order.get("status")
        if state == "paid":
            return StatusReading(
                RailStatus.DEBITED, "the order is paid", amount_paise=order.get("amount_paid"), reference=order_id
            )
        if state == "created" and not order.get("attempts"):
            return StatusReading(RailStatus.NOT_DEBITED, "no payment was attempted on the order", reference=order_id)
        if state == "attempted":
            return StatusReading(RailStatus.PENDING, "a payment was attempted and none captured yet", reference=order_id)
        return StatusReading(RailStatus.CANNOT_TELL, f"order status {state!r} says nothing either way", reference=order_id)


@dataclass(frozen=True, slots=True)
class DocumentedAdapters:
    notifier: RazorpayPreDebitNotifier
    debiter: RazorpayMandateDebiter
    status: RazorpayOrderStatusCheck


def documented_adapters(*, sdk_client: object, mandates: MandateSource) -> DocumentedAdapters:
    """The three documented adapters, over the real `RazorpayClient`, around
    any object shaped like the Razorpay SDK.

    For the adversary's arena, which plays Razorpay's documented surface as a
    world it controls — taking debits, losing a response — so that an attack
    exercises the real client, its transport rules and these adapters end to
    end (windtunnel/adversary/arena.py). It lives here because only this
    module may construct the client (tests/test_actions_boundary.py). No
    backoff sleeps: the arena's clock is virtual."""
    return _adapters(RazorpayClient(sdk_client=sdk_client, sleep=lambda _seconds: None), mandates)  # type: ignore[arg-type]


def _adapters(client: RazorpayClient, mandates: MandateSource) -> DocumentedAdapters:
    """One notice-order ledger shared by all three: the debit is presented
    against the order its notice created, and the status check reads it."""
    orders = NoticeOrders()
    return DocumentedAdapters(
        notifier=RazorpayPreDebitNotifier(client=client, mandates=mandates, orders=orders),
        debiter=RazorpayMandateDebiter(client=client, mandates=mandates, orders=orders),
        status=RazorpayOrderStatusCheck(client=client, orders=orders),
    )


def lost_response(detail: str) -> Exception:
    """The exception the SDK surfaces when a response never arrives: a
    `requests` read timeout, raised unwrapped (its own retry is off by
    default). For a world that plays Razorpay's surface — windtunnel/ may not
    import the network stack, and it should not need to in order to lose a
    response the way the network does."""
    return requests.exceptions.ReadTimeout(detail)
