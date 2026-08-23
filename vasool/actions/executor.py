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

Also owns RetryIndex: a SILENT_RETRY/TIMED_RETRY has no merchant-notes field
to tag the way `_link` tags a Payment Link, so `_retry` records the id
Razorpay's own response carried instead. vasool/events/settlement.py reads it
back to correlate a later `payment.captured` webhook to the episode it closes
— see RetryIndex's own docstring for what that does and does not guarantee.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from vasool.actions.comms import CommsRefused, CommsSender
from vasool.actions.razorpay_client import RazorpayCallFailed, RazorpayClient, RazorpayConfig
from vasool.diagnosis.proposal import Channel, Proposal, ProposalRole
from vasool.diagnosis.taxonomy import InterventionType
from vasool.policy.machine import ExecutionResult

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

    Kept separate from ExecutionResult (vasool/policy/machine.py) rather than
    adding fields to it — that dataclass belongs to the policy plane's
    Executor protocol, and this session does not touch the policy plane.
    vasool/ledger/receipts.py reads this journal structurally (it declares
    its own Protocol shape, not an import of this class) to attach
    razorpay_request_id / razorpay_response to a receipt without the policy
    plane ever needing to carry Razorpay-shaped data.
    """

    proposal_id: str
    ok: bool
    detail: str
    razorpay_request_id: str | None = None
    razorpay_response: dict | None = None


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
    this is a non-guessed join key: `retry_payment` returns Razorpay's own id
    for the payment it just created, and this is that same id recorded
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
    (CLAUDE.md's own allergy to a second, quietly diverging copy of a record
    applies here too: this is data flowing from one config source at wiring
    time, not an independent registry)."""

    client: RazorpayClient
    comms: CommsSender
    registered_templates: frozenset[str]
    journal: ExecutionJournal = field(default_factory=ExecutionJournal)
    retry_index: RetryIndex = field(default_factory=RetryIndex)

    def execute(self, proposal: Proposal) -> ExecutionResult:
        record = self._dispatch(proposal)
        self.journal.record(record)
        return ExecutionResult(ok=record.ok, detail=record.detail)

    def _dispatch(self, proposal: Proposal) -> RazorpayCallRecord:
        if proposal.intervention is InterventionType.HUMAN_QUEUE:
            raise UnroutableProposal(
                "HUMAN_QUEUE never reaches an executor — the state machine "
                "escalates it before execute() is called (see module docstring)"
            )
        if proposal.role in (ProposalRole.NUDGE, ProposalRole.PRE_DEBIT_NOTICE):
            return self._send(proposal)
        if proposal.intervention in (InterventionType.SILENT_RETRY, InterventionType.TIMED_RETRY):
            return self._retry(proposal)
        if proposal.intervention in (InterventionType.REATTEMPT_LINK, InterventionType.REAUTH_LINK):
            return self._link(proposal)
        raise UnroutableProposal(
            f"no executor method for {proposal.intervention.value} / {proposal.role.value}"
        )

    def _retry(self, proposal: Proposal) -> RazorpayCallRecord:
        try:
            response = self.client.retry_payment(
                entity_id=proposal.entity_id,
                amount_paise=proposal.amount_paise,
                currency="INR",
                idempotency_key=proposal.idempotency_key,
            )
        except RazorpayCallFailed as exc:
            log.warning("retry failed for %s: %s", proposal.proposal_id, exc)
            return RazorpayCallRecord(proposal.proposal_id, ok=False, detail=str(exc))
        razorpay_payment_id = response.get("id")
        if razorpay_payment_id is not None:
            self.retry_index.record(razorpay_payment_id, proposal.entity_id)
        return RazorpayCallRecord(
            proposal.proposal_id,
            ok=True,
            detail="retry dispatched",
            razorpay_request_id=razorpay_payment_id,
            razorpay_response=response,
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

    def _send(self, proposal: Proposal, *, link: dict | None = None) -> RazorpayCallRecord:
        params = {"link": link["short_url"], "payment_link_id": link["id"]} if link else {}
        self.comms.send(proposal=proposal, registered_templates=self.registered_templates, params=params)
        return RazorpayCallRecord(proposal.proposal_id, ok=True, detail="sent")

    @classmethod
    def from_env(cls, *, registered_templates: frozenset[str]) -> RazorpayExecutor:
        """A live executor, credentials from RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET
        via RazorpayConfig.from_env() (CLAUDE.md: read secrets from the
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
