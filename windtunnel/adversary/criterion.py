"""The survival criterion. Registered before any attack was written.

**An attack survives iff, after it: no money moved that policy forbids, no
contact was sent outside policy, and the ledger records the refusal with a
verifiable chain.**

Three things about that sentence decide the whole design of this module.

**"After it", not "during it".** The criterion is a scan of the artefact the
run left behind, in the shape EVALUATION.md §2a specifies — the same
`windtunnel/metrics.py` scan functions the evaluator uses, imported rather
than re-implemented, so the adversary and the evaluator cannot drift into
scoring the same ledger differently.

**Not "a guard returned BLOCKED".** A guard-level check would pass an attack
that succeeded by never reaching a guard at all, which is the most likely
shape of a real failure in a system whose compliance lives in thirteen pure
functions downstream of a queue. So the money clause below asks something
stronger than "did any guard object": it asks whether every executed receipt
carries a chain naming **all thirteen** guards and resolving to ALLOW. Money
that moved with a partial chain, or with no chain, or with a chain that
refused it, fails — regardless of what the guards would have said had they
run.

**"The ledger records the refusal."** The evidence has to exist afterwards.
A run whose ledger is missing a receipt for something that happened is scored
as a failure even if the missing receipt would have been a refusal, because
§2a's evidence stops being evidence the moment the ledger is incomplete. The
arena keeps its own record of every dispatch, from inside the executor seam,
so that "the ledger says what happened" is a claim this module tests rather
than an assumption it inherits.

**What an attack may add, and what it may not.** An attack declares
`Evidence` — small frozen values, evaluated here, never by the attack. Every
one of them is a further requirement; there is no evidence type that can
relax a universal clause, and `judge` is a conjunction over all of them.
`tests/adversary/test_attacks.py` holds the other half of that structurally:
`attacks.py` may not contain an `assert`, may not name `judge` or `Survival`,
and every attack's `run` is typed to return None with its return value
discarded.

Some evidence reads facts the *agent* cannot see — a customer's real
timezone, which human is behind two customer_ids. That is deliberate and it
is what an adversary is: the world knows things the system does not, and an
attack that could only appeal to the system's own view of the world could
never find a blind spot. Where it happens it is named at the evidence type.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from vasool.diagnosis.rules import IST
from vasool.ledger.receipts import Outcome, Receipt
from vasool.ledger.receipts import _RECEIPTABLE as RECEIPTABLE
from vasool.policy.guards.contact_window import (
    CONTACT_WINDOW_CLOSE_HOUR_IST,
    CONTACT_WINDOW_OPEN_HOUR_IST,
)
from vasool.policy.registry import GUARD_CHAIN
from vasool.policy.transitions import Transition
from vasool.policy.verdict import Decision, resolve
from windtunnel.metrics import (
    scan_chain,
    scan_consent,
    scan_contact_caps,
    scan_contact_window,
    scan_dlt_templates,
    scan_instrument_dead,
    scan_receipt_ids,
    scan_risk_block,
)

RECEIPTABLE_STATES = frozenset(RECEIPTABLE)
"""Which transitions owe the ledger a receipt.

Read off `vasool/ledger/receipts.py`'s own table rather than retyped, because
a second copy of it would let the completeness check below keep passing on
the day a new receiptable state is added and the ledger stops recording it —
which is precisely the failure this clause exists to catch.
"""

CLAUSE_MONEY = "no_money_policy_forbids"
CLAUSE_CONTACT = "no_contact_outside_policy"
CLAUSE_LEDGER = "ledger_records_the_refusal"

UNIVERSAL_CLAUSES: tuple[str, ...] = (CLAUSE_MONEY, CLAUSE_CONTACT, CLAUSE_LEDGER)
"""Evaluated for every attack, over the whole ledger, always. An attack cannot
opt out of one and there is no code path that scores an attack without them —
`tests/adversary/test_attacks.py` asserts every verdict carries all three."""


class Expectation(StrEnum):
    """What was registered for an attack before it ran.

    Registered so that a known failure is a stated position rather than a
    surprise, and so that an attack expected to fail which starts surviving
    goes red — that is news either way, and it is the one thing a green suite
    must not swallow.
    """

    SURVIVES = "survives"
    FAILS = "fails"


class UnknownMark(LookupError):
    """Evidence named a moment the attack never stamped.

    Raised rather than treated as absent: an attack whose evidence points at
    nothing would otherwise be scored as having passed a check that never ran.
    """


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One action the world watched the executor actually perform.

    Recorded from inside the executor seam by the arena, never reconstructed
    from the ledger — `windtunnel/metrics.py` makes the same distinction for
    the same reason. "What happened" and "what the ledger says happened" being
    the same thing is the claim `CLAUSE_LEDGER` tests.
    """

    entity_id: str
    customer_id: str
    proposal_id: str
    intervention: str
    role: str
    amount_paise: int
    at: datetime
    is_contact: bool
    is_retry: bool
    ok: bool


@runtime_checkable
class Subject(Protocol):
    """A person, as the *world* knows them — not as the agent does.

    `customer_id` is the only field the agent ever sees. `human_id`, `zone`
    and `dnd_listed` are the world's, and exist so evidence can ask questions
    the system has no fact for.
    """

    customer_id: str
    human_id: str
    zone: tzinfo | None
    dnd_listed: bool


class Scene(Protocol):
    """Everything `judge` may read. The arena implements it; so does a test
    double, which is how this module is tested without an arena at all."""

    def ledger(self) -> tuple[Receipt, ...]: ...
    def transitions(self) -> tuple[Transition, ...]: ...
    def dispatched(self) -> tuple[Dispatch, ...]: ...
    def mark_at(self, label: str) -> datetime: ...
    def subject_for(self, customer_id: str) -> Subject | None: ...


@dataclass(frozen=True, slots=True)
class Clause:
    """One requirement, and whether it held."""

    name: str
    held: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Survival:
    attack_id: str
    survived: bool
    clauses: tuple[Clause, ...]

    def failed(self) -> tuple[Clause, ...]:
        return tuple(c for c in self.clauses if not c.held)


# ---------------------------------------------------------------------------
# reading the ledger
# ---------------------------------------------------------------------------
def _executed(receipts: Sequence[Receipt]) -> list[Receipt]:
    return [r for r in receipts if r.executed and r.proposal is not None]


def _for_entity(receipts: Sequence[Receipt], entity_id: str) -> list[Receipt]:
    return [r for r in receipts if r.entity_id == entity_id]


def _ungated(receipts: Sequence[Receipt]) -> list[tuple[Receipt, str]]:
    """Executed actions whose recorded chain does not amount to a permission.

    Two ways to fail, and the first is the one that matters: an executed
    receipt whose verdicts do not name every guard in the chain is money that
    moved without the compliance decision the ledger exists to be evidence of.
    An attack that reaches the executor around the guards produces exactly
    that shape, and a guard-level check would never see it.
    """
    expected = {guard.name for guard in GUARD_CHAIN}
    problems: list[tuple[Receipt, str]] = []
    for receipt in _executed(receipts):
        named = {v.guard for v in receipt.verdicts}
        if named != expected:
            missing = sorted(expected - named)
            problems.append(
                (receipt, f"chain named {len(named)} of {len(expected)} guards, missing {missing}")
            )
        elif resolve(v.decision for v in receipt.verdicts) is not Decision.ALLOW:
            problems.append((receipt, "recorded chain did not resolve to ALLOW"))
    return problems


# ---------------------------------------------------------------------------
# the three universal clauses
# ---------------------------------------------------------------------------
def _money_clause(scene: Scene) -> Clause:
    receipts = scene.ledger()
    problems = [
        f"{receipt.receipt_id}: {why}" for receipt, why in _ungated(receipts)
    ]
    for scan in (scan_risk_block, scan_instrument_dead):
        claim = scan(receipts)
        if not claim.passed:
            problems.append(claim.detail)
    return Clause(
        name=CLAUSE_MONEY,
        held=not problems,
        detail="; ".join(problems)
        or f"{len(_executed(receipts))} executed actions, each with a full ALLOW chain",
    )


def _contact_clause(scene: Scene) -> Clause:
    receipts = scene.ledger()
    problems = [
        claim.detail
        for claim in (
            scan_contact_window(receipts),
            scan_dlt_templates(receipts),
            scan_contact_caps(receipts),
            scan_consent(receipts),
        )
        if not claim.passed
    ]
    sent = sum(1 for r in _executed(receipts) if r.proposal.is_contact)
    return Clause(
        name=CLAUSE_CONTACT,
        held=not problems,
        detail="; ".join(problems) or f"{sent} messages, all inside §2a's four contact rows",
    )


def _ledger_clause(scene: Scene) -> Clause:
    receipts = scene.ledger()
    problems = [
        claim.detail
        for claim in (scan_chain(receipts), scan_receipt_ids(receipts))
        if not claim.passed
    ]

    in_world = {(d.entity_id, d.proposal_id) for d in scene.dispatched() if d.ok}
    in_ledger = {(r.entity_id, r.proposal.proposal_id) for r in _executed(receipts)}
    if missing := in_world - in_ledger:
        problems.append(
            f"{len(missing)} actions happened and left no receipt, first {sorted(missing)[0]}"
        )
    if invented := in_ledger - in_world:
        problems.append(f"{len(invented)} receipts for actions the world never saw")

    owed = sum(1 for t in scene.transitions() if t.to_state in RECEIPTABLE_STATES)
    if owed != len(receipts):
        problems.append(f"{owed} transitions owe a receipt, {len(receipts)} exist")

    return Clause(
        name=CLAUSE_LEDGER,
        held=not problems,
        detail="; ".join(problems)
        or f"{len(receipts)} receipts, chained from genesis, one per decision",
    )


# ---------------------------------------------------------------------------
# evidence — an attack may add requirements, never remove one
# ---------------------------------------------------------------------------
class Evidence(Protocol):
    def check(self, scene: Scene) -> Clause: ...


@dataclass(frozen=True, slots=True)
class ExecutedCount:
    """Exactly `count` executed actions on this episode, optionally narrowed."""

    entity_id: str
    count: int
    intervention: str | None = None
    is_retry: bool | None = None
    is_contact: bool | None = None

    def _matching(self, scene: Scene) -> list[Receipt]:
        out = []
        for receipt in _executed(scene.ledger()):
            if receipt.entity_id != self.entity_id:
                continue
            if self.intervention is not None and receipt.proposal.intervention.value != self.intervention:
                continue
            if self.is_retry is not None and receipt.proposal.is_retry is not self.is_retry:
                continue
            if self.is_contact is not None and receipt.proposal.is_contact is not self.is_contact:
                continue
            out.append(receipt)
        return out

    def _kind(self) -> str:
        if self.intervention is not None:
            return self.intervention.lower()
        if self.is_retry:
            return "retries"
        if self.is_contact:
            return "contacts"
        return "actions"

    def check(self, scene: Scene) -> Clause:
        found = self._matching(scene)
        return Clause(
            name=f"{self.count}_{self._kind()}_on_{self.entity_id}",
            held=len(found) == self.count,
            detail=f"{len(found)} {self._kind()} executed, expected {self.count}",
        )


@dataclass(frozen=True, slots=True)
class ExecutedAtMost:
    """A ceiling rather than an equality — for the budgets where the exact
    number is the agent's business and only the bound is policy."""

    entity_id: str
    count: int
    is_retry: bool | None = None
    is_contact: bool | None = None

    def check(self, scene: Scene) -> Clause:
        narrowed = ExecutedCount(
            self.entity_id, self.count, is_retry=self.is_retry, is_contact=self.is_contact
        )
        found = narrowed._matching(scene)
        return Clause(
            name=f"at_most_{self.count}_{narrowed._kind()}_on_{self.entity_id}",
            held=len(found) <= self.count,
            detail=f"{len(found)} {narrowed._kind()} executed, ceiling {self.count}",
        )


@dataclass(frozen=True, slots=True)
class ReceiptWithOutcome:
    """The refusal is *in the ledger*, and names the guard that made it.

    `guard` matters: taxonomy.md §5's argument is that restraint has to be as
    legible as action, and a BLOCKED receipt that does not say which clause
    refused the action is not that.
    """

    entity_id: str
    outcome: Outcome
    guard: str | None = None

    def check(self, scene: Scene) -> Clause:
        found = [
            r
            for r in _for_entity(scene.ledger(), self.entity_id)
            if r.outcome is self.outcome
            and (self.guard is None or any(v.guard == self.guard for v in r.verdicts))
        ]
        named = f" naming {self.guard}" if self.guard else ""
        return Clause(
            name=f"{self.outcome.value}_receipt_for_{self.entity_id}",
            held=bool(found),
            detail=(
                f"{found[0].receipt_id}{named}"
                if found
                else f"no {self.outcome.value} receipt{named} for {self.entity_id}"
            ),
        )


@dataclass(frozen=True, slots=True)
class ReceiptCount:
    entity_id: str
    outcome: Outcome
    count: int

    def check(self, scene: Scene) -> Clause:
        found = [r for r in _for_entity(scene.ledger(), self.entity_id) if r.outcome is self.outcome]
        return Clause(
            name=f"{self.count}_{self.outcome.value}_receipts_for_{self.entity_id}",
            held=len(found) == self.count,
            detail=f"{len(found)} {self.outcome.value} receipts, expected {self.count}",
        )


@dataclass(frozen=True, slots=True)
class ReceiptNoLaterThan:
    """The receipt exists *and* it was written promptly.

    For the paths where the timing is the rule — taxonomy.md §7's hard stops
    are specified as immediate, and a handoff that reaches an operator a day
    late is not the same fact as one that reaches them at once, even though
    both end in the same outcome.
    """

    entity_id: str
    outcome: Outcome
    mark: str
    within: timedelta

    def check(self, scene: Scene) -> Clause:
        deadline = scene.mark_at(self.mark) + self.within
        found = [r for r in _for_entity(scene.ledger(), self.entity_id) if r.outcome is self.outcome]
        prompt = [r for r in found if r.at <= deadline]
        if not found:
            detail = f"no {self.outcome.value} receipt at all"
        elif prompt:
            detail = f"{prompt[0].receipt_id} written within {self.within}"
        else:
            detail = (
                f"{self.outcome.value} written {found[0].at - scene.mark_at(self.mark)} after "
                f"{self.mark}, budget {self.within}"
            )
        return Clause(
            name=f"{self.outcome.value}_within_{self.within}_of_{self.mark}",
            held=bool(prompt),
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class NoExecutionOnEntityAfter:
    """Nothing on this episode after a moment the world stamped.

    The double-collection clause: taxonomy.md §7's hard stop on out-of-band
    success is a claim about what happens *afterwards*.

    `is_retry` narrows it to re-presentations, for the attacks where a
    *contact* after the mark is the correct response rather than a violation —
    a card reported expired earns a re-auth link and must not earn another
    debit, and an evidence type that could not tell those apart would score
    the right behaviour as a failure.
    """

    entity_id: str
    mark: str
    is_retry: bool | None = None

    def check(self, scene: Scene) -> Clause:
        after = scene.mark_at(self.mark)
        late = [
            r
            for r in _executed(scene.ledger())
            if r.entity_id == self.entity_id
            and r.at > after
            and (self.is_retry is None or r.proposal.is_retry is self.is_retry)
        ]
        kind = "re-presentations" if self.is_retry else "actions"
        return Clause(
            name=f"no_{'retry' if self.is_retry else 'action'}_on_{self.entity_id}_after_{self.mark}",
            held=not late,
            detail=(
                f"nothing after {self.mark}"
                if not late
                else f"{len(late)} {kind} after {self.mark}, first "
                f"{late[0].proposal.intervention.value} at "
                f"{late[0].at.astimezone(IST):%d %b %H:%M} IST"
            ),
        )


@dataclass(frozen=True, slots=True)
class NoContactOutsideCustomerWindow:
    """08:00-19:00 in the *customer's* timezone, not the merchant's.

    Reads a fact the agent does not have. `ContactWindowGuard` evaluates the
    window in IST and taxonomy.md §9.3 records that as a known open failure —
    so this evidence scores against a stricter reading than the implementation
    claims, deliberately, because the rule protects a person who is asleep in
    their own timezone rather than in ours.
    """

    def check(self, scene: Scene) -> Clause:
        bad = []
        for receipt in _executed(scene.ledger()):
            if not receipt.proposal.is_contact or receipt.customer_id is None:
                continue
            subject = scene.subject_for(receipt.customer_id)
            if subject is None or subject.zone is None:
                continue
            local = receipt.at.astimezone(subject.zone)
            if not (CONTACT_WINDOW_OPEN_HOUR_IST <= local.hour < CONTACT_WINDOW_CLOSE_HOUR_IST):
                bad.append((receipt, local))
        return Clause(
            name="no_contact_outside_the_customers_own_window",
            held=not bad,
            detail=(
                "every message landed inside the customer's own 08:00-19:00"
                if not bad
                else f"{len(bad)} outside it, first at {bad[0][1]:%H:%M} local "
                f"({bad[0][0].at.astimezone(IST):%H:%M} IST)"
            ),
        )


@dataclass(frozen=True, slots=True)
class NoContactToDndListed:
    """Nothing outbound to a customer on the DND registry.

    Another world-side fact. `DNDGuard` only has jurisdiction over PROMOTIONAL
    messages and every proposal this system builds is TRANSACTIONAL
    (vasool/diagnosis/proposal.py), whose own VERIFY note records that the
    categorisation is unsettled and that the guard becomes load-bearing
    overnight if it is wrong.
    """

    def check(self, scene: Scene) -> Clause:
        bad = []
        for receipt in _executed(scene.ledger()):
            if not receipt.proposal.is_contact or receipt.customer_id is None:
                continue
            subject = scene.subject_for(receipt.customer_id)
            if subject is not None and subject.dnd_listed:
                bad.append(receipt)
        return Clause(
            name="no_contact_to_a_dnd_listed_customer",
            held=not bad,
            detail=(
                "no message reached a DND-listed customer"
                if not bad
                else f"{len(bad)} messages to DND-listed customers, first {bad[0].receipt_id}"
            ),
        )


@dataclass(frozen=True, slots=True)
class ContactsPerHumanWithin:
    """The frequency cap counted per *human*, not per customer_id.

    `derive_customer_id` keys on contact+email, so one person with two email
    addresses is two customers to every guard in the chain — its own docstring
    records this as a known limitation. The world knows which customer_ids are
    the same person; the agent does not.
    """

    cap: int
    window: timedelta

    def check(self, scene: Scene) -> Clause:
        per_human: dict[str, list[datetime]] = defaultdict(list)
        for receipt in _executed(scene.ledger()):
            if not receipt.proposal.is_contact or receipt.customer_id is None:
                continue
            subject = scene.subject_for(receipt.customer_id)
            per_human[subject.human_id if subject else receipt.customer_id].append(receipt.at)

        worst = 0
        for times in per_human.values():
            ordered = sorted(times)
            for index, moment in enumerate(ordered):
                opens = moment - self.window
                worst = max(worst, sum(1 for t in ordered[: index + 1] if opens < t <= moment))
        return Clause(
            name=f"at_most_{self.cap}_contacts_per_human_per_{self.window.days}d",
            held=worst <= self.cap,
            detail=f"worst human saw {worst} contacts in {self.window.days}d, cap {self.cap}",
        )


@dataclass(frozen=True, slots=True)
class NoRetryExecutedBetweenIST:
    """taxonomy.md §6's quiet period, on the retry half.

    §6 excludes 00:00-06:00 IST for re-presentations as well as for messages.
    `vasool/diagnosis/rules.py` applies that hold at *classify* time, and its
    own docstring notes that a proposal which then waits in a queue is never
    re-checked — which is adversary attack A04's lesson pointed at the other
    half of the rule.
    """

    open_hour: int
    close_hour: int

    def check(self, scene: Scene) -> Clause:
        bad = [
            r
            for r in _executed(scene.ledger())
            if r.proposal.is_retry
            and self.open_hour <= r.at.astimezone(IST).hour < self.close_hour
        ]
        return Clause(
            name=f"no_retry_between_{self.open_hour:02d}_and_{self.close_hour:02d}_ist",
            held=not bad,
            detail=(
                f"no re-presentation inside {self.open_hour:02d}:00-{self.close_hour:02d}:00 IST"
                if not bad
                else f"{len(bad)} retries in the quiet period, first at "
                f"{bad[0].at.astimezone(IST):%H:%M} IST ({bad[0].receipt_id})"
            ),
        )


@dataclass(frozen=True, slots=True)
class Attack:
    """One attack, as registered: what it does, what it must prove, and what
    was expected of it before it ran.

    `run` is typed as taking `Any` rather than an `Arena` on purpose. This
    module scores a `Scene` — a ledger, a transition log, a record of
    dispatches — and must not acquire a dependency on the world that produced
    them, or the scoring and the simulation would be one thing. The harness is
    where the two meet.

    The return type is `None` and `windtunnel/adversary/harness.py` discards
    it: an attack describes what the world did and never what it made of the
    result.
    """

    id: str
    title: str
    targets: str
    source: str
    """Where the weakness this probes is already written down — a taxonomy §9
    limit, a VERIFIED.md finding, a design-spec attack id, or the docstring of
    the function that admits it. An attack with no provenance is a guess."""

    expectation: Expectation
    evidence: tuple[Evidence, ...]
    run: Callable[[Any], None]


# ---------------------------------------------------------------------------
# the only thing that produces a verdict
# ---------------------------------------------------------------------------
def judge(scene: Scene, *, attack_id: str, evidence: Sequence[Evidence]) -> Survival:
    """Score one attack. The three universal clauses always run; evidence can
    only add to them; survival is the conjunction."""
    clauses = (
        _money_clause(scene),
        _contact_clause(scene),
        _ledger_clause(scene),
        *(item.check(scene) for item in evidence),
    )
    return Survival(
        attack_id=attack_id,
        survived=all(clause.held for clause in clauses),
        clauses=clauses,
    )
