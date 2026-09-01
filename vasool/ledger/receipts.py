"""Receipt: design spec §3, hash-chained on prev_hash.

Wraps a Transition (vasool/policy/transitions.py) rather than duplicating its
fields — event_id, the proposal and the verdicts all already exist there. A
Receipt is built FROM one via `receipt_from_transition`, not assembled by
hand at each call site.

**Not one receipt per Transition.** Money moves, is refused, or is escalated
at four kinds of transition: GATED->EXECUTING (executed), GATED->BLOCKED,
GATED->ESCALATED, and *->RECOVERED (settled — see below) — see `_RECEIPTABLE`
below for why executed is keyed on EXECUTING and not the AWAITING state that
follows it. Two of those four also arrive by a second route, closing an
episode with nothing proposed at all — see "closures" below. Every other
transition — SCHEDULED, DIAGNOSED, DEFERRED — is already a complete record in
the transition log itself and represents no decision about money; wrapping
every one of them in a Receipt would bury the four that matter under the rest.
taxonomy.md §5's argument that restraint has to be as visible as action is
about BLOCKED and ESCALATED specifically, not about every intermediate state a
proposal passes through on its way there.

**On design spec §3's Literal outcome field.** It reads
`Literal["recovered", "failed", "pending", "blocked", "deferred"]`, and two
things are wrong with it. First, it has no member for the RISK_BLOCK ->
HUMAN_QUEUE path at all — taxonomy.md §5 calls that path out by name as the
one where correct behaviour is invisible unless the ledger says so, and a
Literal that cannot express "escalated" cannot honour that. Second,
"recovered" and "failed" describe the eventual business outcome of a
recovery, which is not knowable at the instant a Receipt is written: the
episode moves to AWAITING, not RECOVERED, and a Receipt is written once, at
the decision, never edited later when settlement resolves. `Outcome` below
replaces "recovered"/"failed" with EXECUTED / EXECUTION_FAILED — what is
actually known when the receipt is written — and adds ESCALATED and RECOVERED
(next).

**Where amount_recovered_paise actually comes from.** It used to be hardcoded
to 0 here, because the recovered amount is only knowable once money is
confirmed to have landed — a fact vasool.policy.machine.PolicyMachine.settled()
learns from a later, separate webhook, not from anything the executed
proposal carried. settled() now takes that amount and puts it on the
transition to State.RECOVERED (Transition.settled_amount_paise).

That raised the question the session brief posed directly: does the earlier
EXECUTED receipt get amended with the figure once it's known, or does
RECOVERED become a fourth receiptable state with its own receipt? Amendment
loses on the architecture's own terms. A Receipt's `hash` covers every field
including amount_recovered_paise (`_compute_hash` below), and every receipt
after it commits to that hash as its `prev_hash` — the whole point of chaining
is that changing field on receipt N invalidates the hash on N, which no longer
matches what receipt N+1 already recorded as `prev_hash`, and every receipt
after N faces the same problem in turn. "Amend and rehash the rest of the
chain" is indistinguishable from rewriting history, which is precisely what
`verify_chain` exists to catch — the only way to make an amended receipt
verify again is to also rewrite everything after it, at which point the hash
chain is not proving anything a mutable ledger wouldn't. So: a receipt is
written once, at the decision it records, exactly as the module already
argued for "recovered"/"failed" above — and RECOVERED is a fourth entry in
`_RECEIPTABLE`, appended to the end of the chain rather than reaching back
into it. See `receipt_from_transition` for the shape that receipt takes: it
is not gated on a Proposal the way the other three are, because nothing
proposes a settlement.

**Closures: the same shape, for the same reason, twice more.** RECOVERED
turned out not to be one exception but the first of three. Two other
transitions close an episode with no Proposal ever having been gated —
`consent_withdrawn()`'s BLOCKED, because a withdrawal is a statement about a
person rather than a ruling on an action, and `observe()`'s ESCALATED for an
event past MAX_CLOCK_SKEW (A18), which fires before any proposal is built.
Both are ordinary production paths — a real DPDP withdrawal, a real skewed
webhook — and both used to raise here, so no ledger could be built for a
windtunnel seed containing a withdrawal, which is essentially every seed.

So the branch is not "RECOVERED plus two special cases" but the distinction
those three share: **is this receipt a ruling on a proposed action, or a
record of something that happened to the episode?** The policy plane names
which by setting `Transition.closure`, and a closure gets its own `Outcome`
rather than a BLOCKED that merely happens to carry no verdicts — an
empty-verdicts BLOCKED is otherwise indistinguishable from a guard chain that
returned nothing, and EVALUATION.md §2a scans the ledger for withdrawals by
name. The verdicts stay empty: synthesising one would put a guard ruling in
the ledger that no guard ever produced, which is a worse lie than saying
nothing.

Skipping these transitions was the other option and it is wrong twice over.
§2a's "no action after consent withdrawal" is specified as a ledger scan, and
a scan needs the withdrawal in the ledger to anchor "after". And the ledger
would stop being a complete account of how episodes terminated — an episode
would end in BLOCKED with the ledger silent about it, which is exactly the
"correct behaviour indistinguishable from a broken agent" taxonomy.md §5
argues the receipt exists to prevent.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from vasool.diagnosis.proposal import Proposal
from vasool.policy.episode import State
from vasool.policy.transitions import Closure, Transition
from vasool.policy.verdict import Verdict

GENESIS_HASH = "0" * 64
"""prev_hash of the first receipt in a chain. Not a hash of anything — a
sentinel, the way an initial commit still needs a parent to point at."""


class Outcome(StrEnum):
    """design spec §3's Literal, corrected — see module docstring. Closed,
    matching the discipline every enum in this codebase holds to."""

    EXECUTED = "executed"
    """The Razorpay (or comms) call was dispatched and accepted. Not
    "recovered" — see module docstring on why that word describes a fact this
    receipt cannot yet know."""

    EXECUTION_FAILED = "execution_failed"
    """The guards allowed this action and it was dispatched, but Razorpay or
    comms refused it downstream — a 4xx, an exhausted 5xx retry budget, or an
    unregistered template caught by comms.py's own check. Distinct from
    BLOCKED: the compliance decision here was ALLOW, and something else went
    wrong, which is a different fact for the report card."""

    BLOCKED = "blocked"

    ESCALATED = "escalated"
    """Missing from design spec §3 entirely — see module docstring."""

    DEFERRED = "deferred"
    """design spec includes this, but no receipt is built for a DEFERRED
    transition (see module docstring on why). Kept in the enum so a future
    session that decides to receipt deferrals is extending a closed set on
    purpose, not inventing a string at the call site."""

    RECOVERED = "recovered"
    """The fourth receiptable state — see the module docstring's argument
    against amending the EXECUTED receipt instead. Written once, when
    PolicyMachine.settled() closes the episode; carries the real
    amount_recovered_paise no other Outcome can."""

    CONSENT_WITHDRAWN = "consent_withdrawn"
    """The episode was closed by a withdrawal, not by a ruling. Distinct from
    BLOCKED on purpose: BLOCKED is a compliance decision about one action and
    carries the verdicts that made it, this carries none, and EVALUATION.md
    §2a scans for withdrawals as a stated fact rather than as "a BLOCKED that
    happens to have no proposal"."""

    CLOCK_SKEW = "clock_skew"
    """A18: the episode was escalated because the event's timestamp was too
    far ahead to believe, before any proposal existed. Distinct from
    ESCALATED for the same reason CONSENT_WITHDRAWN is distinct from
    BLOCKED — an ESCALATED receipt names the guards that escalated, and no
    guard ran here."""


_CLOSURE_OUTCOME: dict[Closure, Outcome] = {
    Closure.CONSENT_WITHDRAWN: Outcome.CONSENT_WITHDRAWN,
    Closure.SETTLED: Outcome.RECOVERED,
    Closure.CLOCK_SKEW: Outcome.CLOCK_SKEW,
}
"""What a closure is called in the ledger.

Total over `Closure` by construction, and deliberately a lookup that raises
rather than a `.get` with a fallback: a Closure member added upstream without
a name here is a KeyError at the first receipt it reaches, which is the
failure this mapping exists to force. A default would silently file the new
closure under an outcome that means something else.
"""


_RECEIPTABLE: dict[State, Outcome] = {
    State.EXECUTING: Outcome.EXECUTED,
    State.BLOCKED: Outcome.BLOCKED,
    State.ESCALATED: Outcome.ESCALATED,
    State.RECOVERED: Outcome.RECOVERED,
}
"""Which to_state produces a receipt, and what Outcome it starts as.

EXECUTING, not AWAITING, for the executed case — and this matters. Reading
vasool/policy/machine.py::_execute closely: the GATED->EXECUTING transition
is logged with `proposal=proposal, chain=result`, carrying every verdict that
allowed the action. The EXECUTING->AWAITING transition that follows it is
logged via `self._log(episode, State.EXECUTING, State.AWAITING, "awaiting
outcome", proposal=proposal)` — note no `chain=` kwarg, so that Transition's
`chain` is None. Keying receipts on to_state==AWAITING would raise on every
single executed proposal; EXECUTING is the transition that actually carries
the verdicts, and it happens exactly once per execution, so it is both
correct and sufficient. This is also the more precise thing to receipt
anyway: it is the transition that recorded *why* execution was allowed.

This maps only the *state*. Whether a given transition is a compliance
decision or a closure is not knowable from its to_state — BLOCKED and
ESCALATED arrive by both routes — so `receipt_from_transition` reads
`Transition.closure` and overrides the outcome above via `_CLOSURE_OUTCOME`
when one is set. RECOVERED is the case where the two agree: every transition
to it is a closure (`Closure.SETTLED`), because nothing proposes a
settlement — vasool/policy/machine.py::settled() can close an episode with no
proposal in play at all, since an out-of-band payment can arrive before
anything was ever gated (tests/test_machine.py's A07 case)."""


@dataclass(frozen=True, slots=True)
class Receipt:
    """Immutable. `hash` covers every other field; `prev_hash` links it to
    whatever came before."""

    receipt_id: str
    prev_hash: str
    hash: str
    canonical_payload: str

    entity_id: str
    customer_id: str | None
    """Who the episode belongs to.

    EVALUATION.md §2a's claims are per *customer* and the ledger is per
    *entity*, so the join has to be in the receipt. A receipt with a Proposal
    could borrow it from there — a closure receipt cannot, and an episode
    closed by a withdrawal with nothing ever gated is the common case. Without
    it, a withdrawal in the ledger names an entity and nothing else, and the
    scan cannot reach that customer's other episodes to check whether any of
    them were acted on afterwards.

    Optional in the type because a hand-built Transition may carry none;
    PolicyMachine sets it on every transition it logs."""

    event_id: str | None
    """None on a closure receipt — see `proposal` below."""
    proposal: Proposal | None
    """None on a closure receipt: a withdrawal, a settlement or a disbelieved
    clock closes an *episode*, not a Proposal, and each can do it with no
    Proposal ever having been gated."""
    verdicts: tuple[Verdict, ...]
    """Empty on a closure receipt — no guard chain runs over one, and
    synthesising a Verdict to fill this would record a ruling that never
    happened."""

    executed: bool
    razorpay_request_id: str | None
    razorpay_response: dict | None

    outcome: Outcome
    amount_recovered_paise: int
    """0 on every outcome except RECOVERED, where it is what settled() was
    told the customer actually paid (see receipt_from_transition and this
    module's docstring on why that receipt is a fourth state rather than an
    amendment to the EXECUTED one)."""

    at: datetime
    trace_id: str


class CallRecord(Protocol):
    """What vasool/actions/executor.py::RazorpayCallRecord looks like, from
    this module's point of view — a structural shape, not an import, so
    ledger/ never has to depend on actions/ to build a receipt."""

    ok: bool
    razorpay_request_id: str | None
    razorpay_response: dict | None


class CallJournal(Protocol):
    """What vasool/actions/executor.py::ExecutionJournal looks like."""

    def get(self, proposal_id: str) -> CallRecord | None: ...


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _compute_hash(
    *,
    prev_hash: str,
    receipt_id: str,
    entity_id: str,
    customer_id: str | None,
    event_id: str | None,
    proposal: Proposal | None,
    verdicts: Sequence[Verdict],
    executed: bool,
    razorpay_request_id: str | None,
    razorpay_response: dict | None,
    outcome: Outcome,
    amount_recovered_paise: int,
    at: datetime,
    trace_id: str,
) -> tuple[str, str]:
    payload = {
        "prev_hash": prev_hash,
        "receipt_id": receipt_id,
        "entity_id": entity_id,
        "customer_id": customer_id,
        "event_id": event_id,
        "proposal": proposal.model_dump(mode="json") if proposal is not None else None,
        "verdicts": [v.model_dump(mode="json") for v in verdicts],
        "executed": executed,
        "razorpay_request_id": razorpay_request_id,
        "razorpay_response": razorpay_response,
        "outcome": outcome.value,
        "amount_recovered_paise": amount_recovered_paise,
        "at": at.isoformat(),
        "trace_id": trace_id,
    }
    canonical_str = _canonical(payload)
    return hashlib.sha256(canonical_str.encode()).hexdigest(), canonical_str


def _receipt_id(entity_id: str, proposal_id: str | None, to_state: State) -> str:
    """Deterministic, like every other id this codebase derives (see
    vasool/diagnosis/proposal.py::_derive_id) — not uuid4, so replay produces
    the same receipt_id from the same inputs (architectural invariant 5).

    `proposal_id` is None on a closure receipt. That's still deterministic
    and still unique: every closure lands on a terminal state, and an episode
    reaches at most one of those. `_stop` only transitions a non-terminal
    episode, and `observe()` checks `is_terminal` before its own skew
    escalation for exactly this reason — two skewed webhooks for one payment
    would otherwise write two ESCALATED transitions, and both would land on
    this same basis string. So `entity_id` and `to_state` together already
    pick out the one receipt this will ever be, and a closure cannot collide
    with the gated BLOCKED/ESCALATED for the same episode either, since those
    carry a real `proposal_id` in the basis.

    **The uniqueness guarantee, and where it actually comes from.** Session
    4.7 found two receipts sharing an id — card_expired and
    card_disabled_for_online_payments in data/stubbed_payloads/ both produce
    `rcpt_aa8ce1313a1ceab9`. Investigated: every file in that directory
    carries the *same* `payment.entity.id`, because `tools/make_stubs.py`
    derives every stub from one real capture and only ever edits the error
    fields. Two stub scenarios collide exactly when they also happen to map
    to the same (intervention, attempt, role) — here, both are
    REAUTH_LINK/attempt-1/PRIMARY — which makes `proposal_id` identical too
    (it's derived from the same entity_id, see
    vasool/diagnosis/proposal.py::_derive_id), so the basis string below is
    identical for both. This is a fixture artifact, not a defect in this
    function: `entity_id` is part of the hash basis, so two receipts collide
    only if their `entity_id` is *also* identical.

    In production `entity_id` is `payment.entity.id`, a Razorpay-assigned id
    that is globally unique per payment — so two receipts for two genuinely
    different payments cannot collide; they differ on `entity_id` alone.
    Within one entity_id, a collision needs identical `proposal_id` and
    `to_state` too. `proposal_id` is deterministic on
    (entity_id, intervention, attempt, role) by design — it is meant to be
    idempotent, so that a replayed webhook (Razorpay redelivers every one,
    docs/VERIFIED.md) or a reclassification lands on the *same* proposal
    rather than a new one. Two receipts that share entity_id, proposal_id,
    and to_state are therefore, by construction, records of the same
    logical decision, not two different ones that happen to collide —
    tests/test_receipts.py::TestReceiptIdUniqueness proves the
    different-entity_id half of this mechanically; the same-entity_id half
    follows from _derive_id's own idempotency, not from anything this
    function does.

    One caveat this proof doesn't cover: the "|" separator below is not
    escaped, so a crafted entity_id or proposal_id containing "|" could in
    principle shift the basis string's field boundaries and collide with a
    different (entity_id, proposal_id) pair. Every observed Razorpay id
    (`pay_...`, `prop_...`) is restricted to alphanumerics, so this is not a
    reachable case today — noted rather than defended against, per this
    project's rule against validating scenarios that can't happen.
    """
    basis = f"{entity_id}|{proposal_id or '-'}|{to_state.value}"
    return "rcpt_" + hashlib.sha256(basis.encode()).hexdigest()[:16]


def receipt_from_transition(
    transition: Transition,
    *,
    prev_hash: str,
    trace_id: str,
    call: CallRecord | None = None,
) -> Receipt | None:
    """Build the Receipt this Transition owes the ledger, or None if its
    to_state isn't one of the four that owes one (see module docstring).

    A transition that names a `closure` is handled on its own branch: it is
    not a compliance decision about a Proposal, so it does not require one,
    and `amount_recovered_paise` comes from `transition.settled_amount_paise`
    rather than the constant 0 every other Outcome still gets (nothing else
    confirms money landed — dispatching a retry or a link is not the same
    fact, and only `Closure.SETTLED` ever sets it).
    """
    outcome = _RECEIPTABLE.get(transition.to_state)
    if outcome is None:
        return None

    if transition.closure is not None:
        fields = dict(
            receipt_id=_receipt_id(transition.entity_id, None, transition.to_state),
            entity_id=transition.entity_id,
            customer_id=transition.customer_id,
            event_id=None,
            proposal=None,
            verdicts=(),
            executed=False,
            razorpay_request_id=None,
            razorpay_response=None,
            outcome=_CLOSURE_OUTCOME[transition.closure],
            amount_recovered_paise=transition.settled_amount_paise or 0,
            at=transition.at,
            trace_id=trace_id,
        )
        computed, canonical_str = _compute_hash(prev_hash=prev_hash, **fields)
        return Receipt(prev_hash=prev_hash, hash=computed, canonical_payload=canonical_str, **fields)

    if transition.proposal is None or transition.chain is None:
        raise ValueError(
            f"{transition.to_state} transition for {transition.entity_id} carries "
            "no proposal/chain and names no closure — every EXECUTING/BLOCKED/"
            "ESCALATED transition vasool/policy/machine.py emits does one or "
            "the other, so this means something upstream changed without this "
            "module being updated"
        )

    executed = outcome is Outcome.EXECUTED
    request_id = call.razorpay_request_id if call is not None else None
    response = call.razorpay_response if call is not None else None
    if executed and call is not None and not call.ok:
        outcome = Outcome.EXECUTION_FAILED
        executed = False

    receipt_id = _receipt_id(transition.entity_id, transition.proposal.proposal_id, transition.to_state)
    fields = dict(
        receipt_id=receipt_id,
        entity_id=transition.entity_id,
        customer_id=transition.customer_id,
        event_id=transition.proposal.event_id,
        proposal=transition.proposal,
        verdicts=transition.chain.verdicts,
        executed=executed,
        razorpay_request_id=request_id,
        razorpay_response=response,
        outcome=outcome,
        amount_recovered_paise=0,
        at=transition.at,
        trace_id=trace_id,
    )
    computed, canonical_str = _compute_hash(prev_hash=prev_hash, **fields)
    return Receipt(prev_hash=prev_hash, hash=computed, canonical_payload=canonical_str, **fields)


class ReceiptChain:
    """An in-order, hash-linked sequence of receipts. Append-only, the same
    way vasool/policy/transitions.py::InMemoryTransitionLog is: no update, no
    delete."""

    def __init__(self) -> None:
        self._receipts: list[Receipt] = []

    @property
    def last_hash(self) -> str:
        return self._receipts[-1].hash if self._receipts else GENESIS_HASH

    def append_from(
        self, transition: Transition, *, trace_id: str, call: CallRecord | None = None
    ) -> Receipt | None:
        receipt = receipt_from_transition(transition, prev_hash=self.last_hash, trace_id=trace_id, call=call)
        if receipt is not None:
            self._receipts.append(receipt)
        return receipt

    def __iter__(self):
        return iter(tuple(self._receipts))

    def __len__(self) -> int:
        return len(self._receipts)

    def __getitem__(self, index: int) -> Receipt:
        return self._receipts[index]


def build_from_transitions(
    transitions: Iterable[Transition],
    *,
    call_journal: CallJournal | None = None,
    trace_id_of: Callable[[str], str],
) -> ReceiptChain:
    """The whole ledger for one run, derived from what the policy plane
    already logged plus what the executor's own journal remembers.

    `trace_id_of` takes an entity_id and returns a trace id — pass
    vasool.ledger.tracing.trace_id_for in production; a test can pass
    anything deterministic.
    """
    chain = ReceiptChain()
    for t in transitions:
        proposal_id = t.proposal.proposal_id if t.proposal is not None else None
        call = call_journal.get(proposal_id) if (call_journal is not None and proposal_id) else None
        chain.append_from(t, trace_id=trace_id_of(t.entity_id), call=call)
    return chain


def verify_chain(receipts: Sequence[Receipt]) -> bool:
    """Recompute every hash from its own fields and confirm every prev_hash
    link matches. False the instant either check fails — what a tamper test
    asserts on."""
    prev = GENESIS_HASH
    for r in receipts:
        if r.prev_hash != prev:
            return False
        recomputed, _ = _compute_hash(
            prev_hash=r.prev_hash,
            receipt_id=r.receipt_id,
            entity_id=r.entity_id,
            customer_id=r.customer_id,
            event_id=r.event_id,
            proposal=r.proposal,
            verdicts=r.verdicts,
            executed=r.executed,
            razorpay_request_id=r.razorpay_request_id,
            razorpay_response=r.razorpay_response,
            outcome=r.outcome,
            amount_recovered_paise=r.amount_recovered_paise,
            at=r.at,
            trace_id=r.trace_id,
        )
        if recomputed != r.hash:
            return False
        prev = r.hash
    return True
