"""Drives virtual time across a whole universe, through the real agent.

**The boundary this file exists to hold.** windtunnel/ decides what happens TO
the agent — which failures arrive, when, and whether an intervention lands.
vasool/ decides what the agent DOES. Nothing here reimplements a guard, a
classification, a schedule or a state transition; it advances a clock, hands
the real `PolicyMachine` real `FailureEvent`s, and replays real settlement
webhooks back at it.

**Both directions of a retry's outcome go back through production's own
correlation.** The runner never calls `PolicyMachine.settled()`, and never
tells the machine which episode a failed retry belongs to. It builds the
webhook body Razorpay would send — off the captured envelopes in data/ — and
hands it to the production code that decides what to make of it:
`vasool/events/settlement.py::settle_from_webhook` for a settlement, the same
dispatch vasool/events/receiver.py's route calls, and
`vasool/events/schemas.py::from_webhook` for a failure, the same function
that route mints its FailureEvent with. Both correlate through the executor's
own RetryIndex, because `createRecurring` creates a new payment whether the
retry succeeds or fails, so both webhooks arrive naming a payment the policy
plane has never seen. So the simulator reaches RECOVERED and advances its
ladder the way production would, and inherits every gap those paths have
rather than papering over them.

The sharpest of those gaps is deliberate. An out-of-band payment carries
neither join key, so `settle_from_webhook` correctly declines to attribute it,
the episode stays open, and the agent goes on chasing money the merchant
already has. That is not a defect in this runner — it is docs/taxonomy.md
§9.10, and measuring it is the point.

**Determinism.** Every ordering here is total and derived: world events sort
by (time, kind, entity), the machine's own queue is processed in insertion
order, and settlements are applied in the order their executions occurred.
Nothing iterates a set or a dict whose order could vary. architectural invariant 5
has to hold across a whole run, not just one episode
(tests/windtunnel/test_runner.py).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import TYPE_CHECKING

from vasool.actions.comms import CommsSender
from vasool.actions.executor import RazorpayExecutor
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import Proposal, template_ids
from vasool.diagnosis.taxonomy import RULES, Rule, lookup
from vasool.events.settlement import settle_from_webhook
from vasool.ledger.receipts import CallJournal, Receipt, build_from_transitions
from vasool.ledger.tracing import trace_id_for
from vasool.policy.episode import State
from vasool.policy.machine import ExecutionResult, PolicyMachine
from vasool.policy.transitions import Transition
from windtunnel import payloads
from windtunnel.outcome import Attempt, OutcomeModel, Ruling, SettlementChannel
from windtunnel.universe import PlannedEpisode, Universe
from windtunnel.world import WorldFactStore

if TYPE_CHECKING:
    from windtunnel.arms import Arm

MAX_STEPS = 200_000
"""Upper bound on the event loop, so a scheduling bug fails loudly rather than
hanging a 1000-seed run. Deferral is already bounded three ways inside the
machine (MAX_DEFERRALS, DEFER_HORIZON, strict progress), so reaching this
means something upstream changed."""


class WorldEventKind(IntEnum):
    """What the world does to the agent, and in what order when two land on
    the same instant.

    The order is not arbitrary. A withdrawal must be processed before the
    failure it might arrive alongside, so the agent cannot open an episode it
    was just told to stop; and an out-of-band payment must land before new
    work is scheduled at that instant, so the double-collection window is
    measured from when the money actually arrived.
    """

    CONSENT_WITHDRAWN = 0
    OUT_OF_BAND = 1
    FAILURE_ARRIVES = 2


@dataclass(frozen=True, slots=True)
class WorldEvent:
    at: datetime
    kind: WorldEventKind
    entity_id: str
    """Empty for a customer-scoped event. Part of the sort key, so two events
    of one kind at one instant still have a total order."""

    customer_id: str
    episode: PlannedEpisode | None = None


@dataclass(frozen=True, slots=True)
class ExecutedAction:
    """One action the agent actually dispatched, as the world saw it.

    Recorded from inside the executor seam rather than reconstructed from the
    ledger afterwards, because "what the agent did" and "what the ledger says
    it did" being the same thing is a claim §2a makes and should not be an
    assumption the runner builds in.
    """

    entity_id: str
    customer_id: str
    proposal_id: str
    intervention: str
    role: str
    attempt: int
    amount_paise: int
    at: datetime
    is_contact: bool
    is_retry: bool
    ok: bool
    true_failure_class: str = ""
    """What the episode's registered (reason, source) actually is, per the
    registered §4 table — not what this arm believed it to be.

    Only ever differs from `Proposal.failure_class` for an arm that
    misclassifies on purpose (EVALUATION.md §5's baselines, §8's A1). Recorded
    so the report card can state the design spec's headline guardrail — retries
    spent on an instrument that could never authorise — for arms whose own
    label would hide it. A world number, and labelled as one wherever it is
    reported: it is not a ledger scan and is not part of §2a.
    """


@dataclass(frozen=True, slots=True)
class OutOfBandOccurrence:
    """The customer paid through a channel this agent cannot see.

    §4's 0.02 per episode-day drives this. It never produces a recovery — see
    the module docstring — so what it produces instead is recorded here: when
    the money arrived, and (computed by the evaluator, not here) whether the
    agent went on acting afterwards.
    """

    entity_id: str
    customer_id: str
    at: datetime
    amount_paise: int
    correlated: bool
    """Whether `settle_from_webhook` managed to attribute it. Always False,
    and recorded rather than asserted-and-discarded so the report card can
    state the fraction rather than the claim."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """Everything one seed produced. Metrics are the evaluator's job (Session
    6); this is the raw material they are computed from."""

    seed: int
    arm: str
    universe: Universe
    transitions: tuple[Transition, ...]
    executed: tuple[ExecutedAction, ...]
    out_of_band: tuple[OutOfBandOccurrence, ...]
    settled: tuple[tuple[str, str], ...]
    """(entity_id, channel) for every settlement the agent actually saw."""

    final_states: dict[str, State]

    rulings: tuple[Ruling, ...] = ()
    """Every ruling the outcome model handed down, in the order it made them.

    Kept for one reason: EVALUATION.md §7 sweeps each parameter independently
    and has to say which conclusions that parameter could have touched.
    `Ruling.depends_on` names the registered parameters that fed each decision,
    so the attribution is a union over what actually happened rather than a
    re-derivation of which rule *would* have applied — see
    windtunnel/sweeps.py::parameters_touched. A parameter no ruling in either
    arm consulted cannot have moved that comparison, and saying so from the
    record is stronger than saying it from the code.
    """

    _journal: CallJournal | None = None
    """The executor's own record, kept so `ledger()` can attach a Razorpay
    request id to each receipt. Private because nothing outside this class
    should be reading the action plane's call history."""

    def ledger(self) -> tuple[Receipt, ...]:
        """The hash-chained receipt ledger for this run — the artefact every
        EVALUATION.md §2a claim is specified to be scanned from.

        Built on demand rather than in `_result` because it is derived from
        the transition log rather than an independent record, and an evaluator
        running a thousand seeds for their final states should not pay for a
        chain it never reads.

        This used to raise on every seed containing a consent withdrawal —
        which is essentially every seed — because `receipt_from_transition`
        required a Proposal for every BLOCKED and ESCALATED, and two paths
        close an episode without one. Those are closure receipts now
        (vasool/ledger/receipts.py), so the ledger builds for a whole run.
        """
        return tuple(
            build_from_transitions(
                self.transitions, call_journal=self._journal, trace_id_of=trace_id_for
            )
        )

    def transition_digest(self) -> str:
        """SHA-256 over the whole transition log, canonically encoded.

        architectural invariant 5 — "same seed, byte-identical ledger" — is
        asserted for one episode by tests/test_replay.py and has to hold
        across a full 500-customer run. This is that check at scale.

        It digests the *transition log* rather than the receipt ledger
        because the transition log is the strictly stronger artefact:
        receipts are derived from it, and it additionally covers every
        SCHEDULED, DIAGNOSED and DEFERRED step that no receipt records. So
        this is the determinism check, and `ledger_digest` is what the report
        card should quote, because the ledger is what a hostile reader can
        verify.
        """
        return _digest(
            [
                (
                    t.at.isoformat(),
                    t.entity_id,
                    t.from_state.value,
                    t.to_state.value,
                    t.note,
                    t.proposal.proposal_id if t.proposal is not None else None,
                    t.settled_amount_paise,
                )
                for t in self.transitions
            ]
        )

    def ledger_digest(self) -> str:
        """SHA-256 over the receipt chain."""
        return _digest([(r.receipt_id, r.prev_hash, r.hash) for r in self.ledger()])

    def actions_after_out_of_band(self) -> tuple[tuple[str, ExecutedAction], ...]:
        """Every action taken on an episode after that episode's money had
        already arrived out of band — the double-collection exposure §4's
        parameter actually generates.

        The count depends on the 0.02 guess; the *fraction* of out-of-band
        occurrences that see a subsequent action is agent behaviour. The
        evaluator reports both, labelled, because they are different kinds of
        claim (EVALUATION.md §2).
        """
        arrived = {o.entity_id: o.at for o in self.out_of_band}
        return tuple(
            (action.entity_id, action)
            for action in self.executed
            if action.entity_id in arrived and action.at > arrived[action.entity_id]
        )


class SimulatedRazorpay:
    """A Razorpay that never leaves the process.

    Ids are derived from the idempotency key rather than counted, so they do
    not depend on call order — the same discipline windtunnel/rng.py holds to,
    and necessary for the same reason: a retry's id is what the RetryIndex
    correlates a later capture through, so an order-dependent id would make
    settlement order-dependent too.

    No failure modes are modelled. Razorpay refusing a call is a real thing
    (`RazorpayCallFailed`, and Outcome.EXECUTION_FAILED exists for it), but no
    parameter for its rate is registered in EVALUATION.md §4, and inventing
    one would be adding a knob the protocol never agreed to.
    """

    @staticmethod
    def _id(prefix: str, idempotency_key: str) -> str:
        return prefix + hashlib.sha256(idempotency_key.encode()).hexdigest()[:14]

    def create_payment_link(self, *, idempotency_key: str, **kwargs) -> dict:
        link_id = self._id("plink_", idempotency_key)
        return {"id": link_id, "short_url": f"https://rzp.io/l/{link_id[-8:]}"}

    def notify_payment_link(self, **kwargs) -> dict:
        return {"success": True}

    def retry_payment(self, *, idempotency_key: str, **kwargs) -> dict:
        return {"id": self._id("pay_", idempotency_key)}


@dataclass
class ObservingExecutor:
    """The real RazorpayExecutor, with the world watching.

    Two things happen here that cannot happen anywhere else. The world's
    bookkeeping is updated *synchronously*, so a second proposal gated later
    in the same tick sees the first one's contact against the frequency cap.
    And the outcome model is consulted, with the resulting settlement
    *queued* rather than applied — applying it here would call `settled()`
    while `PolicyMachine._execute` is mid-transition, marking the episode
    RECOVERED and then advancing it to AWAITING on top.
    """

    inner: RazorpayExecutor
    world: WorldFactStore
    outcome: OutcomeModel
    clock: VirtualClock
    rules: dict[tuple[str, str], Rule] = field(default_factory=lambda: RULES)
    """The §4 table this arm classifies against. Read for one thing only —
    whether this arm's row for the episode aims its retry at a salary window —
    and never to decide whether money arrives."""
    executed: list[ExecutedAction] = field(default_factory=list)
    rulings: list[Ruling] = field(default_factory=list)
    pending_settlements: list[tuple[Proposal, SettlementChannel]] = field(default_factory=list)
    pending_failures: list[Proposal] = field(default_factory=list)
    """Retries the world declined. Each owes the agent another
    `payment.failed` — see Runner._deliver_retry_failures."""

    def execute(self, proposal: Proposal) -> ExecutionResult:
        at = self.clock.now()
        plan = self.world.episode_for(proposal.entity_id)
        result = self.inner.execute(proposal)
        self.world.record_execution(proposal, at=at)
        self.executed.append(
            ExecutedAction(
                entity_id=proposal.entity_id,
                customer_id=proposal.customer_id,
                proposal_id=proposal.proposal_id,
                intervention=proposal.intervention.value,
                role=proposal.role.value,
                attempt=proposal.attempt,
                amount_paise=proposal.amount_paise,
                at=at,
                is_contact=proposal.is_contact,
                is_retry=proposal.is_retry,
                ok=result.ok,
                true_failure_class=plan.failure_class.value,
            )
        )

        if result.ok:
            ruling = self.outcome.rule_on(
                Attempt(
                    episode_id=proposal.entity_id,
                    # **The world's class, never the agent's.** This used to
                    # read `proposal.failure_class`, which is what this arm
                    # *believes* the failure is. That was invisible while
                    # Vasool was the only arm — its taxonomy is the
                    # simulator's ground truth, so belief and truth were the
                    # same object. They are not the same for an arm that
                    # misclassifies on purpose, and reading the belief would
                    # invert the experiment: A1 labels an expired card
                    # TRANSIENT, so the outcome model would price its retry at
                    # the transient rate and the card would authorise. The
                    # ablation would then earn recoveries by being wrong, and
                    # "no taxonomy" would beat the taxonomy. An expired card
                    # not authorising is a fact about the world (taxonomy §5),
                    # so the world is what decides it — resolved through the
                    # registered table by `PlannedEpisode.failure_class`.
                    failure_class=plan.failure_class,
                    intervention=proposal.intervention,
                    role=proposal.role,
                    attempt=proposal.attempt,
                    amount_paise=proposal.amount_paise,
                    effective_at=at,
                    # Whether this arm *aimed* the retry at a salary window,
                    # read off the arm's own §4 row rather than off the
                    # intervention. Ablation A2 keeps LIQUIDITY's TIMED_RETRY
                    # and removes its salary timing, so inferring this from
                    # the intervention would leave A2 unable to express
                    # itself — which is why `Attempt` carries it as a field
                    # (windtunnel/outcome.py).
                    salary_timed=lookup(plan.reason, plan.source, rules=self.rules)[
                        1
                    ].salary_aware,
                )
            )
            self.rulings.append(ruling)
            if ruling.money_arrives and ruling.channel is not None:
                self.pending_settlements.append((proposal, ruling.channel))
            elif proposal.is_retry:
                # A re-presentation that did not authorise fails, and a
                # failure is a webhook. Without this the ladder never
                # advances past attempt 1 and taxonomy §4's retry budgets are
                # inert — see Runner._deliver_retry_failures.
                self.pending_failures.append(proposal)

        return result


class Runner:
    """One arm, one seed, one universe."""

    def __init__(
        self,
        universe: Universe,
        *,
        outcome: OutcomeModel,
        pepper: str,
        arm: "Arm | None" = None,
    ) -> None:
        """`arm` is EVALUATION.md §5's baseline or §8's ablation to run, and
        defaults to full Vasool.

        An arm is a configuration of the real `PolicyMachine` — a §4 table, a
        guard chain and a chain-resolution rule — never a second agent. The
        FSM, the thirteen guards, the executor and the ledger are the same
        objects in every arm, which is what makes the comparison a comparison
        of two policies rather than of two codebases.
        """
        from windtunnel.arms import VASOOL

        self.arm = arm if arm is not None else VASOOL
        self.universe = universe
        self._pepper = pepper
        self.outcome = outcome
        self.clock = VirtualClock(universe.epoch)
        self.world = WorldFactStore(universe=universe)
        self._razorpay = SimulatedRazorpay()
        self._inner = RazorpayExecutor(
            client=self._razorpay,
            # Delivery always succeeds: comms.py still enforces the DLT
            # template and the channel, which is the part that can refuse,
            # and no transport-failure rate is registered in §4.
            comms=CommsSender(deliver=lambda proposal, params: {"delivered": True}),
            registered_templates=template_ids(),
        )
        self.executor = ObservingExecutor(
            inner=self._inner,
            world=self.world,
            outcome=outcome,
            clock=self.clock,
            rules=self.arm.rules,
        )
        self.machine = PolicyMachine(
            clock=self.clock,
            facts=self.world,
            executor=self.executor,
            chain=self.arm.chain,
            rules=self.arm.rules,
            resolve=self.arm.resolve,
        )
        self._out_of_band: list[OutOfBandOccurrence] = []
        self._settled: list[tuple[str, str]] = []

    # -- the loop ---------------------------------------------------------
    def run(self) -> RunResult:
        pending = self._world_events()
        cursor = 0
        steps = 0

        while steps < MAX_STEPS:
            steps += 1
            next_world = pending[cursor].at if cursor < len(pending) else None
            next_due = min(
                (item.proposal.execute_at for item in self.machine.pending()), default=None
            )
            moment = _earliest(next_world, next_due)
            if moment is None or moment > self.universe.horizon:
                break

            self.clock.advance_to(moment)
            while cursor < len(pending) and pending[cursor].at <= moment:
                self._apply(pending[cursor])
                cursor += 1

            self.machine.tick()
            self._drain_settlements()
            self._deliver_retry_failures()
        else:
            raise RuntimeError(
                f"seed {self.universe.seed}: hit the {MAX_STEPS}-step cap with work "
                "still pending — deferral is bounded three ways inside "
                "PolicyMachine, so this means something upstream changed"
            )

        return self._result()

    def _world_events(self) -> list[WorldEvent]:
        """Everything the world does, in one totally-ordered list.

        Built up front rather than discovered as the run proceeds, because
        every one of them is a fact about the universe rather than a
        consequence of the agent's behaviour — which is what lets all four
        arms see an identical world (EVALUATION.md §5).
        """
        events: list[WorldEvent] = []
        for episode in self.universe.episodes:
            events.append(
                WorldEvent(
                    at=episode.arrives_at,
                    kind=WorldEventKind.FAILURE_ARRIVES,
                    entity_id=episode.entity_id,
                    customer_id=episode.customer.customer_id,
                    episode=episode,
                )
            )
            if episode.out_of_band_at is not None:
                events.append(
                    WorldEvent(
                        at=episode.out_of_band_at,
                        kind=WorldEventKind.OUT_OF_BAND,
                        entity_id=episode.entity_id,
                        customer_id=episode.customer.customer_id,
                        episode=episode,
                    )
                )
        for customer in self.universe.customers:
            if customer.consent_withdrawn_at is not None:
                events.append(
                    WorldEvent(
                        at=customer.consent_withdrawn_at,
                        kind=WorldEventKind.CONSENT_WITHDRAWN,
                        entity_id="",
                        customer_id=customer.customer_id,
                    )
                )
        events.sort(key=lambda e: (e.at, e.kind, e.entity_id, e.customer_id))
        return events

    def _apply(self, event: WorldEvent) -> None:
        if event.kind is WorldEventKind.FAILURE_ARRIVES:
            assert event.episode is not None
            self.machine.observe(event.episode.event)
        elif event.kind is WorldEventKind.CONSENT_WITHDRAWN:
            # DPDP, and adversary attack A12. Purges queued work and closes
            # the customer's open episodes; the ConsentRecord already carries
            # the withdrawal, so ConsentGuard independently refuses anything
            # arriving later.
            self.machine.consent_withdrawn(event.customer_id)
        else:
            self._apply_out_of_band(event)

    def _apply_out_of_band(self, event: WorldEvent) -> None:
        """The customer paid somewhere else.

        Fires a genuine `payment.captured` — the real captured envelope, with
        a payment id this agent never dispatched — through the same dispatch
        production uses, and records what that dispatch made of it. It makes
        nothing of it, always, because there is no join key: no
        `vasool_entity_id`, no RetryIndex entry (docs/taxonomy.md §9.9).

        Skipped entirely once the episode is closed. An episode that already
        reached a terminal state is not being chased, so a later payment by
        that customer is an unrelated transaction rather than an out-of-band
        settlement of this recovery — counting it would inflate the exposure
        metric with events that expose nothing.
        """
        assert event.episode is not None
        state = self.machine.state_of(event.entity_id)
        if state is None or state in {State.RECOVERED, State.BLOCKED, State.ESCALATED, State.EXHAUSTED}:
            return

        body = payloads.capture_body(
            payment_id=SimulatedRazorpay._id("pay_oob_", event.entity_id),
            amount_paise=event.episode.amount_paise,
        )
        correlated = settle_from_webhook(
            event_name="payment.captured",
            body=body,
            machine=self.machine,
            retry_index=self._inner.retry_index,
        )
        self._out_of_band.append(
            OutOfBandOccurrence(
                entity_id=event.entity_id,
                customer_id=event.customer_id,
                at=event.at,
                amount_paise=event.episode.amount_paise,
                correlated=correlated is not None,
            )
        )

    def _drain_settlements(self) -> None:
        """Replay the settlement webhooks the executions earned.

        In the order the executions happened, and after the tick rather than
        inside it — see ObservingExecutor.
        """
        pending, self.executor.pending_settlements = self.executor.pending_settlements, []
        for proposal, channel in pending:
            if channel is SettlementChannel.LINK_PAID:
                event_name = "payment_link.paid"
                body = payloads.link_paid_body(
                    entity_id=proposal.entity_id, amount_paise=proposal.amount_paise
                )
            else:
                # The id Razorpay's own response carried, read back off the
                # executor's journal — never recomputed here, because the
                # whole correlation rests on it being the executor's record
                # rather than a guess (vasool/events/settlement.py).
                record = self._inner.journal.get(proposal.proposal_id)
                if record is None or record.razorpay_request_id is None:
                    continue
                event_name = "payment.captured"
                body = payloads.capture_body(
                    payment_id=record.razorpay_request_id, amount_paise=proposal.amount_paise
                )

            entity_id = settle_from_webhook(
                event_name=event_name,
                body=body,
                machine=self.machine,
                retry_index=self._inner.retry_index,
            )
            if entity_id is not None:
                self._settled.append((entity_id, channel.value))

    def _deliver_retry_failures(self) -> None:
        """Tell the agent that a retry it dispatched did not authorise.

        **This is what drives the retry ladder, and without it the taxonomy is
        inert.** `PolicyMachine.observe` computes `attempt` as
        `episode.attempts_used + 1`, so attempt 2 of
        `gateway_technical_error`'s 5m/30m/4h ladder only exists if a second
        `payment.failed` arrives for the same episode. Nothing else in the
        simulator would ever produce one, and every episode would rest in
        AWAITING after a single try.

        **The follow-up carries the retry's own new payment id**, which is
        what Razorpay actually sends: `retry_payment` wraps `createRecurring`,
        which creates a new payment, so its failure webhook names that payment
        and not the one the episode opened on. Read back off the executor's
        journal, never recomputed here — the same discipline
        `_drain_settlements` holds to, and for the same reason: the whole
        correlation rests on this being Razorpay's own id rather than a guess
        (vasool/events/settlement.py).

        Resolving it back to the episode is then production's own job, done
        by the same `from_webhook` the receiver calls, through the same
        RetryIndex the executor filled. This used to model the *documented*
        intent instead — the follow-up carried the original entity_id — with
        a `# VERIFY:` recording that production did not behave that way. It
        does now; the note is gone because the gap it named is closed rather
        than tolerated.

        Same reason and source as the original, because a retry that failed
        for the same underlying cause has not learned anything new.

        # VERIFY: one narrower gap remains and it is not simulated here.
        # RetryIndex is process-local (vasool/actions/executor.py), so in
        # production a restart between a retry firing and its failure webhook
        # arriving loses the mapping, and that webhook opens a fresh episode
        # at attempt 1 — the ladder fragments exactly as it used to. Nothing
        # in windtunnel restarts a process, and no restart-rate parameter is
        # registered in EVALUATION.md §4, so this evaluation measures the
        # agent as it behaves within one process and is optimistic by
        # whatever that rate turns out to be.

        Zero latency, for the same reason settlement has none: no
        failure-latency parameter is registered in EVALUATION.md §4, and
        inventing one would be a parameter the protocol never agreed to.
        """
        pending, self.executor.pending_failures = self.executor.pending_failures, []
        for proposal in pending:
            record = self._inner.journal.get(proposal.proposal_id)
            if record is None or record.razorpay_request_id is None:
                # No id came back, so nothing was ever recorded to correlate
                # against and Razorpay has no new payment to report failing.
                # Unreachable with SimulatedRazorpay, which always answers
                # with an id; not special-cased away, because in production it
                # is the second half of the gap the VERIFY note above names.
                continue
            plan = self.world.episode_for(proposal.entity_id)
            self.machine.observe(
                payloads.failure_event(
                    reason=plan.reason,
                    source=plan.source,
                    entity_id=record.razorpay_request_id,
                    contact=plan.customer.contact,
                    email=plan.customer.email,
                    amount_paise=plan.amount_paise,
                    occurred_at=self.clock.now(),
                    pepper=self._pepper,
                    sequence=proposal.attempt,
                    retry_index=self._inner.retry_index,
                )
            )

    def _result(self) -> RunResult:
        transitions = tuple(self.machine.transitions)
        return RunResult(
            seed=self.universe.seed,
            arm=self.arm.name,
            universe=self.universe,
            transitions=transitions,
            executed=tuple(self.executor.executed),
            out_of_band=tuple(self._out_of_band),
            settled=tuple(self._settled),
            rulings=tuple(self.executor.rulings),
            final_states={
                episode.entity_id: state
                for episode in self.universe.episodes
                if (state := self.machine.state_of(episode.entity_id)) is not None
            },
            _journal=self._inner.journal,
        )


def _digest(rows: object) -> str:
    """Canonical JSON, then SHA-256. `sort_keys` and a fixed separator so the
    digest cannot move for a reason that is not a real difference — the same
    encoding vasool/ledger/receipts.py::_canonical uses."""
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _earliest(*moments: datetime | None) -> datetime | None:
    real = [m for m in moments if m is not None]
    return min(real) if real else None


def run_seed(
    seed: int,
    *,
    pepper: str,
    outcome: OutcomeModel | None = None,
    arm: "Arm | None" = None,
    **universe_kwargs,
) -> RunResult:
    """Build one universe and run the full agent against it.

    The convenience entry point. EVALUATION.md §6a fixes the seed range at
    0..999, and every one of those is an independent world — nothing here is
    shared between seeds, so an evaluator may run them in any order or in
    parallel and get the same ledgers.
    """
    from windtunnel.parameters import OUTCOME_PARAMETERS
    from windtunnel.universe import build_universe

    outcome = outcome or OutcomeModel(parameters=OUTCOME_PARAMETERS, seed=seed)
    universe = build_universe(seed, pepper=pepper, outcome=outcome, **universe_kwargs)
    return Runner(universe, outcome=outcome, pepper=pepper, arm=arm).run()
