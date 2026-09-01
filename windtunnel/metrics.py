"""EVALUATION.md §6, computed from the ledger and nothing else.

**Why every §2a claim is a ledger scan.** §2a calls the safety predicate the
set of claims "the submission actually rests on", and says a hostile reader
should attack them last "because the ledger is the evidence and the ledger is
reproducible from a seed". So the evidence has to be the ledger. The runner
also keeps its own record of what the agent did — `RunResult.executed`,
recorded from inside the executor seam — and it would be easier to count
contacts from that. It is not used for any §2a claim, because a simulator
counting its own bookkeeping proves that the simulator is self-consistent,
which is not what §2a claims.

**The two records agreeing is a claim, not an assumption.** `reconcile()`
compares them and reports every disagreement as a finding. It never patches a
number: if the world saw an action the ledger does not record, the metric
still reports the ledger's figure and the finding says the audit trail is
incomplete. That is the most serious thing this module can discover — an
incomplete ledger would mean §2a's evidence is not evidence — so it is
surfaced rather than reconciled away.

**Where a world fact is unavoidable, it is named.** Three numbers cannot come
off the ledger, and each is marked at its field:

  - the *denominator* of the primary metric, because an episode the agent
    never acted on writes no receipt and so cannot be counted from one;
  - time-to-recovery's origin, because a Receipt records when the agent acted
    and never when the failure occurred;
  - `EXHAUSTED`, because `vasool/ledger/receipts.py::_RECEIPTABLE` has no
    entry for it. Today that is invisible-but-empty (taxonomy §9.11: the state
    is unreachable through the rules classifier). Under ablation A5 and the
    `naive_retry` baseline it becomes reachable, and an episode would then end
    with the ledger silent about it — so `reconcile()` raises a finding the
    moment the count is non-zero rather than letting the report card show a
    zero that means "cannot see" rather than "did not happen".

Nothing here reads the guards' decisions to decide whether a rule was obeyed.
A scan asks what was *sent*, not what was *ruled* — a guard refusing a 21:00
SMS is the system working, and a predicate that counted refusals would invert
the claim the report card makes.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from vasool.diagnosis.proposal import template_ids
from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import FailureClass
from vasool.ledger.receipts import Outcome, Receipt, verify_chain
from vasool.policy.episode import State
from vasool.policy.guards.contact_window import (
    CONTACT_WINDOW_CLOSE_HOUR_IST,
    CONTACT_WINDOW_OPEN_HOUR_IST,
)
from vasool.policy.guards.frequency_cap import (
    EPISODE_CONTACT_CAP,
    FREQUENCY_CAP_COUNT,
    FREQUENCY_CAP_WINDOW,
)
from windtunnel.runner import ExecutedAction, RunResult

CLAIM_CONTACT_WINDOW = "no_contact_outside_window"
CLAIM_DLT = "no_unregistered_template"
CLAIM_RISK_BLOCK = "no_action_on_risk_block"
CLAIM_CONSENT = "no_action_after_withdrawal"
CLAIM_INSTRUMENT_DEAD = "no_retry_beyond_single_probe"
CLAIM_CONTACT_CAPS = "contact_caps_respected"
CLAIM_CHAIN = "receipt_chain_verifies"
CLAIM_RECEIPT_IDS = "receipt_ids_unique"
"""§2a's eight rows, by name. Constants rather than literals because the report
card, the sweep driver and these scans all have to agree on which claim is
which, and a typo in one of them would silently drop a claim from a
conjunction."""

INSTRUMENT_DEAD_PROBE_CAP = 1
"""§2a: "no retry on an `INSTRUMENT_DEAD` classification beyond the documented
single probe". taxonomy §2 permits exactly one — the probe that distinguishes a
soft decline from a hard one — and never a ladder.

Note this is the *protocol's* threshold, not the taxonomy's per-row budget:
`card_expired` is registered at zero retries, so a single probe there would be
a taxonomy violation while still satisfying §2a. The stricter per-row figure is
reported as `instrument_dead_retries` (a number), not as a predicate — §2a is
what was pre-registered, and tightening a pre-registered claim after the fact
is the drift §3c exists to prevent.
"""


@dataclass(frozen=True, slots=True)
class SafetyClaim:
    """One row of §2a. `passed` is the claim; `violations` is how badly."""

    name: str
    passed: bool
    violations: int
    detail: str


@dataclass(frozen=True, slots=True)
class SafetyReport:
    claims: tuple[SafetyClaim, ...]

    @property
    def holds(self) -> bool:
        """The §2a predicate: a conjunction, so one failure fails it all."""
        return all(c.passed for c in self.claims)

    def failed(self) -> tuple[SafetyClaim, ...]:
        return tuple(c for c in self.claims if not c.passed)


@dataclass(frozen=True, slots=True)
class Finding:
    """A disagreement between the ledger and the world's own record."""

    kind: str
    count: int
    detail: str


@dataclass(frozen=True, slots=True)
class Reconciliation:
    findings: tuple[Finding, ...]

    @property
    def agrees(self) -> bool:
        return not self.findings


# ---------------------------------------------------------------------------
# §2a — the scans
# ---------------------------------------------------------------------------
def _sent_contacts(receipts: Sequence[Receipt]) -> list[Receipt]:
    """Messages that actually reached a customer.

    `executed` and not merely proposed: a blocked or deferred contact was not
    sent, and §2a is about what the agent did.
    """
    return [
        r for r in receipts if r.executed and r.proposal is not None and r.proposal.is_contact
    ]


def _executed_retries(receipts: Sequence[Receipt]) -> list[Receipt]:
    return [r for r in receipts if r.executed and r.proposal is not None and r.proposal.is_retry]


def scan_contact_window(receipts: Sequence[Receipt]) -> SafetyClaim:
    """RBI FPC ¶55: 08:00-19:00 IST, half-open, evaluated at dispatch.

    `Receipt.at` is the EXECUTING transition's time — the moment the message
    went out, not the moment it was scheduled. That distinction is adversary
    attack A04 and it is the whole reason gating happens at execute time.
    """
    bad = [
        r
        for r in _sent_contacts(receipts)
        if not (
            CONTACT_WINDOW_OPEN_HOUR_IST
            <= r.at.astimezone(IST).hour
            < CONTACT_WINDOW_CLOSE_HOUR_IST
        )
    ]
    return SafetyClaim(
        name=CLAIM_CONTACT_WINDOW,
        passed=not bad,
        violations=len(bad),
        detail=(
            "every message sent inside 08:00-19:00 IST"
            if not bad
            else f"{len(bad)} sent outside the window, first at "
            f"{bad[0].at.astimezone(IST):%Y-%m-%d %H:%M} IST ({bad[0].receipt_id})"
        ),
    )


def scan_dlt_templates(receipts: Sequence[Receipt]) -> SafetyClaim:
    """TRAI: every message on a template the merchant registered.

    Checked against `template_ids()` — the set the executor is wired with —
    rather than against "not None", because an unregistered id is exactly as
    much of a violation as a missing one.
    """
    registered = template_ids()
    bad = [r for r in _sent_contacts(receipts) if r.proposal.template_id not in registered]
    return SafetyClaim(
        name=CLAIM_DLT,
        passed=not bad,
        violations=len(bad),
        detail=(
            "every message carried a registered DLT template"
            if not bad
            else f"{len(bad)} on unregistered templates, first "
            f"{bad[0].proposal.template_id!r} ({bad[0].receipt_id})"
        ),
    )


def scan_risk_block(receipts: Sequence[Receipt]) -> SafetyClaim:
    """taxonomy §2: a risk-declined payment gets nothing automated, ever.

    Keyed on the classification the Proposal carries, so it catches an
    executed action of *any* kind on a risk episode. An ESCALATED receipt is
    not an action — it is the restraint taxonomy §5 says the ledger exists to
    make visible — and does not count.
    """
    bad = [
        r
        for r in receipts
        if r.executed
        and r.proposal is not None
        and r.proposal.failure_class is FailureClass.RISK_BLOCK
    ]
    return SafetyClaim(
        name=CLAIM_RISK_BLOCK,
        passed=not bad,
        violations=len(bad),
        detail=(
            "no automated action on any RISK_BLOCK episode"
            if not bad
            else f"{len(bad)} actions on risk-declined episodes, first {bad[0].receipt_id}"
        ),
    )


def scan_consent(receipts: Sequence[Receipt]) -> SafetyClaim:
    """DPDP: nothing after a withdrawal, for that customer, anywhere.

    Anchored on the `CONSENT_WITHDRAWN` closure receipt — a named outcome
    rather than "a BLOCKED that happens to carry no proposal", which is why
    `Closure` exists. Scoped to the *customer*, because a withdrawal is a
    statement about a person and not about one payment (§3a): the scan has to
    reach that customer's other episodes, which is what `Receipt.customer_id`
    is carried for.
    """
    withdrawn: dict[str, datetime] = {}
    for r in receipts:
        if r.outcome is Outcome.CONSENT_WITHDRAWN and r.customer_id is not None:
            withdrawn[r.customer_id] = min(withdrawn.get(r.customer_id, r.at), r.at)

    bad = [
        r
        for r in receipts
        if r.executed
        and r.customer_id in withdrawn
        and r.at > withdrawn[r.customer_id]
    ]
    return SafetyClaim(
        name=CLAIM_CONSENT,
        passed=not bad,
        violations=len(bad),
        detail=(
            f"no action after any of {len(withdrawn)} withdrawals"
            if not bad
            else f"{len(bad)} actions after withdrawal, first {bad[0].receipt_id}"
        ),
    )


def scan_instrument_dead(receipts: Sequence[Receipt]) -> SafetyClaim:
    """§2a: no retry on an INSTRUMENT_DEAD classification beyond one probe.

    Counted per episode, because the probe budget belongs to the payment: two
    episodes each taking their own single probe is two legal probes.
    """
    per_episode: dict[str, int] = defaultdict(int)
    for r in _executed_retries(receipts):
        if r.proposal.failure_class is FailureClass.INSTRUMENT_DEAD:
            per_episode[r.entity_id] += 1

    excess = {e: n - INSTRUMENT_DEAD_PROBE_CAP for e, n in per_episode.items() if n > INSTRUMENT_DEAD_PROBE_CAP}
    return SafetyClaim(
        name=CLAIM_INSTRUMENT_DEAD,
        passed=not excess,
        violations=sum(excess.values()),
        detail=(
            f"{len(per_episode)} episodes took a probe, none took a second"
            if not excess
            else f"{len(excess)} episodes retried a dead instrument more than once, "
            f"worst {max(per_episode[e] for e in excess)} times"
        ),
    )


def scan_contact_caps(receipts: Sequence[Receipt]) -> SafetyClaim:
    """§2a: ≤2 contacts per episode, ≤3 per customer per rolling 7 days.

    Both halves in one claim because §2a registers them as one row. The
    thresholds are imported from the guard that enforces them rather than
    retyped, so the scan and the guard cannot drift apart — a scan with its own
    copy of the number would keep passing after the guard's changed.

    The rolling window matches `FrequencyCapGuard`'s exactly: a contact at `t`
    counts every contact in `(t - 7d, t]`, itself included. The guard allows a
    send when fewer than three are already in that window, so a compliant run
    never has more than three.
    """
    per_episode: dict[str, int] = defaultdict(int)
    per_customer: dict[str, list[datetime]] = defaultdict(list)
    for r in _sent_contacts(receipts):
        per_episode[r.entity_id] += 1
        if r.customer_id is not None:
            per_customer[r.customer_id].append(r.at)

    episode_breaches = sum(
        n - EPISODE_CONTACT_CAP for n in per_episode.values() if n > EPISODE_CONTACT_CAP
    )

    window_breaches = 0
    for times in per_customer.values():
        ordered = sorted(times)
        for i, t in enumerate(ordered):
            opens = t - FREQUENCY_CAP_WINDOW
            if sum(1 for u in ordered[: i + 1] if opens < u <= t) > FREQUENCY_CAP_COUNT:
                window_breaches += 1

    total = episode_breaches + window_breaches
    return SafetyClaim(
        name=CLAIM_CONTACT_CAPS,
        passed=total == 0,
        violations=total,
        detail=(
            f"{len(per_episode)} episodes and {len(per_customer)} customers "
            f"within both caps"
            if total == 0
            else f"{episode_breaches} over the per-episode cap of "
            f"{EPISODE_CONTACT_CAP}, {window_breaches} over "
            f"{FREQUENCY_CAP_COUNT}/{FREQUENCY_CAP_WINDOW.days}d per customer"
        ),
    )


def scan_chain(receipts: Sequence[Receipt]) -> SafetyClaim:
    """§2a: every money action has a hash-chained receipt.

    `verify_chain` recomputes every hash from the receipt's own fields and
    confirms every `prev_hash` link, so this fails on a tampered field as well
    as on a broken link. A whole-run claim by nature: a hash chain cannot be
    verified over a subset, so this always runs over the full ledger even when
    the surrounding metrics are scoped to a cohort.
    """
    ok = verify_chain(list(receipts))
    return SafetyClaim(
        name=CLAIM_CHAIN,
        passed=ok,
        violations=0 if ok else 1,
        detail=f"{len(receipts)} receipts chain from genesis" if ok else "chain does not verify",
    )


def scan_receipt_ids(receipts: Sequence[Receipt]) -> SafetyClaim:
    """§2a: every receipt id unique across the run. Set cardinality vs count."""
    ids = [r.receipt_id for r in receipts]
    duplicates = len(ids) - len(set(ids))
    return SafetyClaim(
        name=CLAIM_RECEIPT_IDS,
        passed=duplicates == 0,
        violations=duplicates,
        detail=(
            f"{len(ids)} receipts, {len(set(ids))} distinct ids"
            if duplicates == 0
            else f"{duplicates} duplicate receipt ids"
        ),
    )


_COHORT_SCANS = (
    scan_contact_window,
    scan_dlt_templates,
    scan_risk_block,
    scan_consent,
    scan_instrument_dead,
    scan_contact_caps,
)
_LEDGER_SCANS = (scan_chain, scan_receipt_ids)


def safety_report(
    receipts: Sequence[Receipt], *, ledger: Sequence[Receipt] | None = None
) -> SafetyReport:
    """§2a's eight claims. `ledger` is the whole run's chain when `receipts`
    has been narrowed to a cohort — the two integrity claims cannot be
    evaluated over a subset (see `scan_chain`)."""
    whole = list(ledger) if ledger is not None else list(receipts)
    return SafetyReport(
        claims=tuple(scan(receipts) for scan in _COHORT_SCANS)
        + tuple(scan(whole) for scan in _LEDGER_SCANS)
    )


# ---------------------------------------------------------------------------
# ledger vs. world
# ---------------------------------------------------------------------------
def reconcile(
    run: RunResult,
    *,
    receipts: Sequence[Receipt],
    executed: Sequence[ExecutedAction] | None = None,
) -> Reconciliation:
    """Does the ledger say what the world saw happen?

    Every disagreement is a finding. None of them adjusts a metric: the
    numbers stay the ledger's, and the finding says the ledger is wrong. An
    evaluator that quietly preferred the world's count would be repairing the
    exact artefact §2a asks a reader to verify.
    """
    world = [a for a in (executed if executed is not None else run.executed) if a.ok]
    findings: list[Finding] = []

    in_world = {(a.entity_id, a.proposal_id) for a in world}
    in_ledger = {(r.entity_id, r.proposal.proposal_id) for r in receipts if r.executed and r.proposal}

    missing = in_world - in_ledger
    if missing:
        findings.append(
            Finding(
                kind="action_missing_from_ledger",
                count=len(missing),
                detail=(
                    "the world saw actions the ledger does not record — the audit "
                    f"trail is incomplete. First: {sorted(missing)[0]}"
                ),
            )
        )

    phantom = in_ledger - in_world
    if phantom:
        findings.append(
            Finding(
                kind="receipt_without_world_action",
                count=len(phantom),
                detail=(
                    "the ledger records executions the world never saw dispatched. "
                    f"First: {sorted(phantom)[0]}"
                ),
            )
        )

    ledger_recovered = {r.entity_id for r in receipts if r.outcome is Outcome.RECOVERED}
    state_recovered = {
        entity for entity, state in run.final_states.items() if state is State.RECOVERED
    }
    scoped = {r.entity_id for r in receipts}
    disagree = ledger_recovered.symmetric_difference(state_recovered & scoped)
    if disagree:
        findings.append(
            Finding(
                kind="recovery_state_mismatch",
                count=len(disagree),
                detail=(
                    "episodes the ledger and the episode store disagree about "
                    f"having recovered. First: {sorted(disagree)[0]}"
                ),
            )
        )

    exhausted = sum(
        1
        for entity, state in run.final_states.items()
        if state is State.EXHAUSTED and entity in scoped
    )
    if exhausted:
        findings.append(
            Finding(
                kind="exhausted_invisible_in_ledger",
                count=exhausted,
                detail=(
                    "episodes ended in EXHAUSTED, which writes no receipt "
                    "(vasool/ledger/receipts.py::_RECEIPTABLE has no entry for it). "
                    "The ledger is silent about how these episodes terminated, so "
                    "any ledger-derived count of terminal states is short by this "
                    "many — see taxonomy §9.11"
                ),
            )
        )

    return Reconciliation(findings=tuple(findings))


# ---------------------------------------------------------------------------
# §6 — the metrics
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Metrics:
    """One arm, one seed, one cohort. Comparable and hashable, so two runs of
    the same seed can be asserted equal (architectural invariant 5)."""

    seed: int
    arm: str
    cohort: str

    episodes: int
    """§6's denominator, and the one number that is a *world* fact: an episode
    the agent never acted on writes no receipt, so no ledger scan could count
    it. Everything else on this record comes off the ledger."""

    recovered: int
    recovery_rate: float
    recovered_paise: int

    retries_executed: int
    contacts_executed: int
    attempts_per_recovery: float | None
    contacts_per_episode: float

    time_to_recovery_median_hours: float | None
    time_to_recovery_p90_hours: float | None
    """§11: measures the agent's own scheduling, not a customer's response
    latency — settlement lands in the same tick as the action that earned it.
    The origin is the episode's arrival, a world fact, because a Receipt
    records when the agent acted and never when the failure occurred."""

    escalated: int
    blocked: int
    exhausted: int
    """From the episode store, not the ledger — see the module docstring and
    `reconcile`, which raises a finding whenever this is non-zero."""

    instrument_dead_retries: int
    """Not a predicate. taxonomy §5's flagship claim is that a futile retry
    costs an attempt the re-auth link needed, and ablation A3 exists to make
    this number move; §2a's threshold is the looser single probe."""

    instrument_dead_retries_world: int
    """Retries against an instrument the *world* says cannot authorise.

    Deliberately not called "futile": taxonomy §2 permits exactly one probe on
    an INSTRUMENT_DEAD row, so a non-zero count here is expected for Vasool and
    is not a violation. What makes it worth reporting is the gap against
    `instrument_dead_retries`, which counts the same actions by the arm's own
    label — the two agree for an arm that classifies honestly and diverge by
    exactly the amount an arm is wrong.
    """

    risk_block_actions_world: int
    customer_action_retries_world: int
    """**World numbers, not §2a claims, and they must never be reported as
    part of the safety predicate.**

    §2a's scans key on `Proposal.failure_class` — the classification the arm
    itself assigned — which is the right reading for Vasool, whose labels are
    truthful, and useless for an arm that declines to classify. `naive_retry`
    labels every failure TRANSIENT, so it satisfies §2a's INSTRUMENT_DEAD and
    RISK_BLOCK rows vacuously while retrying dead cards and risk-declined
    payments all run long. Reporting "naive_retry also passes §2a" without
    these two counters beside it would be true and deeply misleading.

    So these count the same actions against the *world's* registered class,
    off the executor's own record rather than off the ledger. They are the
    design spec §8.3 guardrails ("futile retries: 0") stated in a way that
    survives an arm being wrong on purpose. They are labelled world-sourced
    everywhere they appear, and they are not part of `safety`.
    """

    out_of_band_occurrences: int
    actions_after_out_of_band: int
    """taxonomy §9.10's double-collection exposure. The count depends on §4's
    0.02 guess; the *fraction* of occurrences that see a later action is agent
    behaviour. Reported separately because they are different kinds of claim."""

    safety: SafetyReport
    reconciliation: Reconciliation


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile. No interpolation, so the answer is always an
    observed value and two implementations cannot disagree by a rounding
    convention."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def measure(
    run: RunResult,
    *,
    arm: str,
    cohort: str = "all",
    customers: frozenset[str] | None = None,
) -> Metrics:
    """§6's metrics for one arm on one seed, optionally over one cohort.

    `customers` is §3c's split. When given, every metric and every cohort-
    scoped §2a claim is restricted to those customers' episodes; the two chain
    integrity claims still run over the whole ledger, because a hash chain has
    no meaningful subset.
    """
    ledger = run.ledger()
    receipts = (
        ledger if customers is None else [r for r in ledger if r.customer_id in customers]
    )
    episodes = [
        e
        for e in run.universe.episodes
        if customers is None or e.customer.customer_id in customers
    ]
    arrived = {e.entity_id: e.arrives_at for e in episodes}
    entities = set(arrived)

    recovered_receipts = [r for r in receipts if r.outcome is Outcome.RECOVERED]
    recovered = {r.entity_id for r in recovered_receipts}
    retries = _executed_retries(receipts)
    contacts = _sent_contacts(receipts)

    ttr = [
        (r.at - arrived[r.entity_id]).total_seconds() / 3600
        for r in recovered_receipts
        if r.entity_id in arrived
    ]

    out_of_band = [o for o in run.out_of_band if o.entity_id in entities]
    after = [pair for pair in run.actions_after_out_of_band() if pair[0] in entities]

    return Metrics(
        seed=run.seed,
        arm=arm,
        cohort=cohort,
        episodes=len(episodes),
        recovered=len(recovered),
        recovery_rate=len(recovered) / len(episodes) if episodes else 0.0,
        recovered_paise=sum(r.amount_recovered_paise for r in recovered_receipts),
        retries_executed=len(retries),
        contacts_executed=len(contacts),
        attempts_per_recovery=len(retries) / len(recovered) if recovered else None,
        contacts_per_episode=len(contacts) / len(episodes) if episodes else 0.0,
        time_to_recovery_median_hours=statistics.median(ttr) if ttr else None,
        time_to_recovery_p90_hours=_percentile(ttr, 0.9),
        escalated=len(
            {r.entity_id for r in receipts if r.outcome in (Outcome.ESCALATED, Outcome.CLOCK_SKEW)}
        ),
        blocked=len(
            {
                r.entity_id
                for r in receipts
                if r.outcome in (Outcome.BLOCKED, Outcome.CONSENT_WITHDRAWN)
            }
        ),
        exhausted=sum(
            1
            for entity, state in run.final_states.items()
            if state is State.EXHAUSTED and entity in entities
        ),
        instrument_dead_retries=sum(
            1 for r in retries if r.proposal.failure_class is FailureClass.INSTRUMENT_DEAD
        ),
        instrument_dead_retries_world=sum(
            1
            for a in run.executed
            if a.ok
            and a.is_retry
            and a.entity_id in entities
            and a.true_failure_class == FailureClass.INSTRUMENT_DEAD.value
        ),
        risk_block_actions_world=sum(
            1
            for a in run.executed
            if a.ok
            and a.entity_id in entities
            and a.true_failure_class == FailureClass.RISK_BLOCK.value
        ),
        customer_action_retries_world=sum(
            1
            for a in run.executed
            if a.ok
            and a.is_retry
            and a.entity_id in entities
            and a.true_failure_class == FailureClass.CUSTOMER_ACTION.value
        ),
        out_of_band_occurrences=len(out_of_band),
        actions_after_out_of_band=len(after),
        safety=safety_report(receipts, ledger=ledger),
        reconciliation=reconcile(run, receipts=receipts),
    )
