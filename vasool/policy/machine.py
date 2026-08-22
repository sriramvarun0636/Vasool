"""The deterministic policy state machine.

No LLM, no wall clock, no randomness. Every transition is logged, and the same
inputs produce the same trajectory — which is the whole reason money movement
lives here rather than in the diagnosis plane.

Three design decisions are worth reading before the code.

**Gating happens immediately before execution, never at propose time.** The
design spec's FSM has no state between DIAGNOSED and GATED, so an action
scheduled 48 hours out would have its compliance check now and its money
movement then. That is adversary attack A04 promoted from a test case to an
architecture. Here a proposal waits in SCHEDULED, and the guard chain runs in
the same tick that executes it, against the same snapshot.

**Re-entry after a deferral re-reads the world, not the diagnosis.** An earlier
draft of this had the machine re-run `classify()` on wake to catch staleness.
That is vacuous: `classify` is a pure function of (event, attempt) and neither
changes while an action waits, so it can only ever return what it returned
before. What genuinely goes stale is the *facts* — consent withdrawn, the
payment settled out of band, a contact sent in the meantime — and those are
re-snapshotted on every gate by construction. Reclassification happens where it
actually belongs: when a *new* failure arrives with a different reason, which is
what A06 really is.

**Deferral is bounded three ways**, because each catches a different pathology:
a count budget for oscillation between guards, an absolute horizon for death by
small increments, and strict monotonic progress (enforced in the Guard base
class) for livelock. A deferral that cannot name a time it will expire is a
refusal wearing a disguise, and the whole "defer rather than block" claim
depends on the difference being real.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import Callable, Sequence
from typing import Protocol

from vasool.clock import Clock
from vasool.diagnosis.proposal import Proposal, notice_proposal_from, proposals_from
from vasool.diagnosis.rules import classify
from vasool.diagnosis.taxonomy import InterventionType
from vasool.events.schemas import FailureEvent
from vasool.policy.episode import (
    Episode,
    EpisodeStore,
    InMemoryEpisodeStore,
    State,
)
from vasool.policy.facts import FactStore, GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.registry import GUARD_CHAIN, evaluate_all
from vasool.policy.transitions import InMemoryTransitionLog, Transition, TransitionLog
from vasool.policy.verdict import ChainResult, Decision, ObligationKind, Verdict

log = logging.getLogger(__name__)

MAX_DEFERRALS = 5
"""How many times one action may be rescheduled before we give up on it.

Five rather than three because a legitimate chain is longer than it looks: an
evening nudge can hit the contact window, then the frequency cap, then a
promise to pay, and each of those is a real rule doing its job. This is the
anti-oscillation backstop, not the primary bound — DEFER_HORIZON is.

# VERIFY: judgment, not statute. Tuned against the deferral chains the thirteen
# can actually produce; a fourteenth guard would want it revisited.
"""

DEFER_HORIZON = timedelta(days=7)
"""How far past its original execution time an action may be pushed.

The bound that does the real work, because it catches the failure the count
budget cannot: five deferrals of a day each are individually reasonable and
collectively absurd. An action a week stale is answering a question the customer
has stopped asking.

# VERIFY: judgment, not statute. Seven days is roughly the point at which a
# recovery message stops reading as a follow-up and starts reading as an
# unrelated demand.
"""

MAX_CLOCK_SKEW = timedelta(minutes=5)
"""How far in the future an event's timestamp may be before we disbelieve it.

Adversary attack A18. Clocks disagree by seconds constantly, so a strict bound
would reject ordinary traffic; an event claiming to be an hour ahead is either
skew worth investigating or a corrupted payload, and scheduling from it would
mean acting on something that has not happened.
"""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ok: bool
    detail: str = ""


class Executor(Protocol):
    """The action plane's seam. Stage 5 supplies the module that calls Razorpay;
    nothing in the policy plane knows or cares which one it is."""

    def execute(self, proposal: Proposal) -> ExecutionResult: ...


@dataclass
class RecordingExecutor:
    """Session 3's executor: records what it was asked to do and does nothing.

    The action plane is stage 5. Keeping the machine executor-agnostic means the
    whole policy plane is testable end to end now, and means the module that
    will eventually call Razorpay is reached through one seam that is already
    exercised.
    """

    executed: list[Proposal] = field(default_factory=list)

    def execute(self, proposal: Proposal) -> ExecutionResult:
        self.executed.append(proposal)
        return ExecutionResult(ok=True, detail="recorded (no action plane in stage 3)")


@dataclass(frozen=True, slots=True)
class ScheduledItem:
    """A proposal waiting for its time, and its deferral history."""

    event: FailureEvent
    proposal: Proposal
    origin: Proposal
    """The proposal as first made. Never mutated — a deferral creates a
    successor — so this is what the receipt shows alongside what actually ran."""

    deferrals: int = 0
    first_deferred_at: datetime | None = None
    causes: tuple[Verdict, ...] = ()
    """Every clause cited on the way here, in order."""


class PolicyMachine:
    def __init__(
        self,
        *,
        clock: Clock,
        facts: FactStore,
        executor: Executor,
        episodes: EpisodeStore | None = None,
        transitions: TransitionLog | None = None,
        chain: tuple[Guard, ...] = GUARD_CHAIN,
    ) -> None:
        self._clock = clock
        self.facts = facts
        self.executor = executor
        self.episodes = episodes if episodes is not None else InMemoryEpisodeStore()
        self.transitions = transitions if transitions is not None else InMemoryTransitionLog()
        self._chain = chain
        self._queue: list[ScheduledItem] = []

    # -- inspection -------------------------------------------------------
    def pending(self) -> tuple[ScheduledItem, ...]:
        return tuple(self._queue)

    def state_of(self, entity_id: str) -> State | None:
        episode = self.episodes.get(entity_id)
        return episode.state if episode else None

    # -- inbound ----------------------------------------------------------
    def observe(self, event: FailureEvent) -> None:
        """A failure arrived. Classify it, and schedule what it implies."""
        now = self._clock.now()

        if event.occurred_at > now + MAX_CLOCK_SKEW:
            episode = self.episodes.open(event, now=now)
            self._to(
                episode,
                State.ESCALATED,
                f"event timestamped {event.occurred_at.isoformat()}, more than "
                f"{MAX_CLOCK_SKEW} ahead of now — refusing to schedule from a "
                "clock we do not believe",
            )
            return

        episode = self.episodes.open(event, now=now)
        if episode.is_terminal:
            log.info(
                "episode %s is %s; absorbing further failure",
                episode.entity_id,
                episode.state,
            )
            return

        attempt = episode.attempts_used + 1
        diagnosis = classify(event, clock=self._clock, attempt=attempt)
        episode = self._to(episode, State.DIAGNOSED, diagnosis.rationale)

        proposals = proposals_from(diagnosis, event, now=now)
        if not proposals:
            self._to(
                episode,
                State.EXHAUSTED,
                "retry budget spent and §4's row names no escalation",
            )
            return

        for proposal in proposals:
            self._schedule(ScheduledItem(event=event, proposal=proposal, origin=proposal))

    def consent_withdrawn(self, customer_id: str) -> None:
        """DPDP, and adversary attack A12.

        Purge *and* stop, which are two things and not one. Emptying the queue
        handles the work that is waiting; closing the episodes handles the work
        that is not. An episode whose retry has already fired sits in AWAITING
        with nothing queued, so a queue-only purge leaves it open and the next
        failure webhook — which Razorpay will deliver, twice — starts the chase
        over. There is no carve-out for customers whose payment happened to fail
        again after they withdrew.

        Scoped to the customer, not the payment. A withdrawal is a statement
        about the person.

        One boundary worth naming: a withdrawal for a customer we hold no
        episode for closes nothing, because there is nothing to close. A failure
        arriving for them later is stopped by ConsentGuard at gate time instead,
        from the consent record the FactStore holds. Keeping a withdrawal
        registry here as well would put a second, quietly diverging copy of that
        record inside the state machine.
        """
        self._stop(
            episodes=self.episodes.for_customer(customer_id),
            matches=lambda item: item.proposal.customer_id == customer_id,
            state=State.BLOCKED,
            note="consent withdrawn — queue purged and the episode closed",
        )

    def settled(self, entity_id: str, *, reason: str) -> None:
        """The money arrived, or went back. Either way, stop chasing.

        Covers adversary attacks A07 (customer pays out of band while a retry is
        in flight — the double-collection case) and A14 (refund issued while a
        retry is pending). Both are the same instruction to this machine: there
        is nothing left to recover.

        Closes the episode whether or not anything was queued, for the same
        reason A12 does — the dangerous moment is precisely the one where a
        retry has fired and we are awaiting its outcome.
        """
        episode = self.episodes.get(entity_id)
        self._stop(
            episodes=(episode,) if episode is not None else (),
            matches=lambda item: item.proposal.entity_id == entity_id,
            state=State.RECOVERED,
            note=f"stopped: {reason}",
        )

    # -- the tick ---------------------------------------------------------
    def tick(self) -> None:
        """Gate and act on everything now due."""
        now = self._clock.now()
        due = [item for item in self._queue if item.proposal.execute_at <= now]
        for item in due:
            self._gate(item, now)

    def _gate(self, item: ScheduledItem, now: datetime) -> None:
        episode = self.episodes.get(item.proposal.entity_id)
        if episode is None or episode.is_terminal:
            self._queue.remove(item)
            return

        ctx = self._context(item, episode, now)

        if ctx.facts.merchant.kill_switch:
            # Honoured mid-flight, and non-destructively: the action stays
            # queued, so switching the merchant back on resumes the work rather
            # than discovering it was thrown away.
            log.warning("kill switch on for %s — holding %s", episode.merchant_id, item.proposal.proposal_id)
            return

        self._queue.remove(item)
        result = evaluate_all(ctx, self._chain)
        episode = self._to(
            episode,
            State.GATED,
            f"{len(result.verdicts)} guards evaluated",
            proposal=item.proposal,
            chain=result,
        )

        if result.decision is Decision.ALLOW:
            if item.proposal.intervention is InterventionType.HUMAN_QUEUE:
                # Permitted, but not by us. HUMAN_QUEUE is a handoff, not an
                # automated action, and letting it reach the executor would put
                # "the agent did something" in the ledger for the one path whose
                # entire correctness is that it did not.
                self._to(
                    episode,
                    State.ESCALATED,
                    item.proposal.rationale,
                    proposal=item.proposal,
                    chain=result,
                )
            else:
                self._execute(episode, item, result)
        elif result.decision is Decision.DEFER:
            self._defer(episode, item, result, now)
        elif result.decision is Decision.ESCALATE:
            self._to(
                episode,
                State.ESCALATED,
                "; ".join(v.reason for v in result.deciding() if v.reason),
                proposal=item.proposal,
                chain=result,
            )
        else:
            self._to(
                episode,
                State.BLOCKED,
                "; ".join(v.reason for v in result.blocking() if v.reason),
                proposal=item.proposal,
                chain=result,
            )

    # -- outcomes ---------------------------------------------------------
    def _execute(self, episode: Episode, item: ScheduledItem, result: ChainResult) -> None:
        proposal = item.proposal
        episode = self._to(
            episode,
            State.EXECUTING,
            f"{proposal.intervention.value} ({proposal.role.value})",
            proposal=proposal,
            chain=result,
        )
        self.executor.execute(proposal)
        episode = self.episodes.advance(
            episode,
            State.AWAITING,
            attempts_used=episode.attempts_used + (1 if proposal.is_retry else 0),
            contacts_sent=episode.contacts_sent + (1 if proposal.is_contact else 0),
            executed_keys=episode.executed_keys | {proposal.idempotency_key},
        )
        self._log(episode, State.EXECUTING, State.AWAITING, "awaiting outcome", proposal=proposal)

        for obligation in result.obligations:
            if obligation.kind is ObligationKind.SEND_PRE_DEBIT_NOTICE:
                self._schedule(
                    dataclasses.replace(
                        item,
                        proposal=notice_proposal_from(proposal, execute_at=obligation.not_before),
                        origin=proposal,
                    )
                )

    def _defer(
        self, episode: Episode, item: ScheduledItem, result: ChainResult, now: datetime
    ) -> None:
        until = result.defer_until
        assert until is not None  # ChainResult guarantees it on a DEFER
        causes = item.causes + tuple(result.deciding())

        if item.deferrals + 1 > MAX_DEFERRALS:
            self._to(
                episode,
                State.BLOCKED,
                f"deferred {item.deferrals} times already (budget {MAX_DEFERRALS}) — "
                "an action that keeps being rescheduled is being refused slowly",
                proposal=item.proposal,
                chain=result,
                causes=causes,
            )
            return

        horizon = item.origin.execute_at + DEFER_HORIZON
        if until > horizon:
            self._to(
                episode,
                State.BLOCKED,
                f"deferring to {until.isoformat()} would push this past the "
                f"{DEFER_HORIZON.days}-day horizon from {item.origin.execute_at.isoformat()} — "
                "a week-stale recovery answers a question the customer stopped asking",
                proposal=item.proposal,
                chain=result,
                causes=causes,
            )
            return

        successor = item.proposal.model_copy(
            update={"execute_at": until, "supersedes": item.proposal.proposal_id}
        )
        deferred = dataclasses.replace(
            item,
            proposal=successor,
            deferrals=item.deferrals + 1,
            first_deferred_at=item.first_deferred_at or now,
            causes=causes,
        )
        self._schedule(
            deferred,
            state=State.DEFERRED,
            note="; ".join(v.reason for v in result.deciding() if v.reason),
            chain=result,
            causes=causes,
        )

    # -- plumbing ---------------------------------------------------------
    def _context(self, item: ScheduledItem, episode: Episode, now: datetime) -> GuardContext:
        """The store covers the world outside this episode; the episode covers
        its own counters. Splitting it this way keeps FactStore from having to
        know what an episode is."""
        facts = self.facts.snapshot(event=item.event, proposal=item.proposal, now=now)
        facts = dataclasses.replace(
            facts,
            attempts_used=episode.attempts_used,
            episode_contacts=episode.contacts_sent,
            executed_keys=facts.executed_keys | episode.executed_keys,
        )
        return GuardContext(
            now=now,
            effective_at=max(now, item.proposal.execute_at),
            event=item.event,
            proposal=item.proposal,
            facts=facts,
        )

    def _schedule(
        self,
        item: ScheduledItem,
        *,
        state: State = State.SCHEDULED,
        note: str | None = None,
        **extra,
    ) -> None:
        """Queue an action and record where that leaves the episode.

        `state` because a deferred action rests in DEFERRED rather than sliding
        back into SCHEDULED. An episode waiting because §4 chose a time and one
        waiting because a guard moved it are different situations: the second is
        a compliance hold, it is a number the report card needs, and collapsing
        both into SCHEDULED makes it unrecoverable from the episode alone.

        The proposal logged is the successor — the thing now queued, at its new
        time. It carries `supersedes`, so the proposal that was actually gated
        is one hop away, and the deferral's causes travel on the transition.
        """
        self._queue.append(item)
        episode = self.episodes.get(item.proposal.entity_id)
        if episode is None:
            return
        self._to(
            episode,
            state,
            note
            or f"{item.proposal.intervention.value} at {item.proposal.execute_at.isoformat()}",
            proposal=item.proposal,
            **extra,
        )

    def _stop(
        self,
        *,
        episodes: Sequence[Episode],
        matches: Callable[[ScheduledItem], bool],
        state: State,
        note: str,
    ) -> None:
        """Empty the queue of matching work, and close the named episodes.

        The episodes are passed in rather than derived from the doomed items,
        because the episode that most needs closing is the one with nothing
        queued.
        """
        for item in [item for item in self._queue if matches(item)]:
            self._queue.remove(item)
        for episode in episodes:
            if not episode.is_terminal:
                self._to(episode, state, note)

    def _to(self, episode: Episode, state: State, note: str, **extra) -> Episode:
        previous = episode.state
        updated = self.episodes.advance(episode, state)
        self._log(updated, previous, state, note, **extra)
        return updated

    def _log(self, episode: Episode, previous: State, state: State, note: str, **extra) -> None:
        self.transitions.append(
            Transition(
                at=self._clock.now(),
                entity_id=episode.entity_id,
                from_state=previous,
                to_state=state,
                note=note,
                **extra,
            )
        )
