"""Does money arrive? The one question the world answers for the agent.

**Read EVALUATION.md §1 before this module.** "I am measuring an agent against
a world I built. The simulator decides whether a retry succeeds. I wrote the
simulator." Nothing in this file repairs that. What it does is make the
choices legible: every probability is a registered parameter looked up by
name, every ruling says which parameter decided it and which registered
values fed that, and the three readings §4 does not cover are argued at their
use sites in the direction least favourable to Vasool.

**The boundary.** This module answers "would money arrive". It never decides
whether an action was permitted, when it should happen, or what the agent does
next — that is vasool/'s, and the simulator supplies the world rather than a
second copy of the agent. It reads the failure class, the intervention, the
attempt index and the calendar. It does not read consent, DND, or any
compliance fact: whether a message was legal has nothing to do with whether a
card authorises.

**Where the randomness lives: in the coordinates, not in a generator.** See
windtunnel/rng.py. Two addressing choices here are load-bearing:

  - **The intervention is deliberately NOT part of a settlement's address.**
    The address is (episode, attempt, role). So when one arm retries at
    attempt 2 and another sends a link at attempt 2, both consult the same
    coin and only the probability differs — common random numbers, which is
    what keeps §6a's paired differences from being dominated by the arms
    disagreeing about coin flips rather than about policy.
  - **The role IS part of it.** A nudge shares its sibling retry's
    intervention and attempt (vasool/diagnosis/proposal.py), so without the
    role they would be one draw and a message that settles nothing would
    still consume the retry's answer.

**Out-of-band settlement does not produce a recovery, in any arm.** §4
registers P(out-of-band settlement) per episode-day, and the honest
consequence of docs/taxonomy.md §9.9 is that such a payment is structurally
unattributable: it carries no `vasool_entity_id` and appears in no RetryIndex,
so it is indistinguishable from any other payment on the account. The receiver
correctly declines to correlate it, the episode stays open, and the agent
keeps chasing money the merchant already has. So this parameter does not
generate recovery — it generates double-collection exposure, which is a safety
finding rather than a recovery number, and §4's own wording agrees ("set it to
zero and the attack never fires in evaluation"). Two consequences a reader has
to hold onto:

  1. Out-of-band money is never counted as recovered in ANY arm, so every arm
     is undercounted by the same mechanism. §6a's paired differences are
     unaffected; the marginal recovery rates are biased low across the board,
     and must be read that way.
  2. taxonomy.md §7 lists "hard stop on out-of-band success" as a stopping
     rule. For this path the rule can never fire, because nothing upstream can
     recognise the event — recorded as §9.10 of that document.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from vasool.diagnosis.proposal import ProposalRole
from vasool.diagnosis.rules import IST, in_salary_window
from vasool.diagnosis.taxonomy import (
    CONTACT_INTERVENTIONS,
    RETRY_INTERVENTIONS,
    FailureClass,
    InterventionType,
)
from windtunnel.parameters import Parameter
from windtunnel.rng import draw, uniform

SETTLEMENT = "settlement"
OUT_OF_BAND = "out_of_band"
"""Draw stream names. Distinct so that an episode's settlement answers and its
out-of-band exposure can never collide at the same coordinates."""


class SettlementChannel(StrEnum):
    """How the money would arrive — which decides which real webhook envelope
    the runner replays, and therefore which correlation path in
    vasool/events/settlement.py has to recognise it. Closed."""

    RETRY_CAPTURE = "RETRY_CAPTURE"
    """A `payment.captured` for a payment this agent's own executor created.
    Correlates through the executor's RetryIndex."""

    LINK_PAID = "LINK_PAID"
    """A `payment_link.paid` for a link this agent sent. Correlates through
    the `notes.vasool_entity_id` the executor stamps."""

    OUT_OF_BAND = "OUT_OF_BAND"
    """A `payment.captured` for a payment nobody here dispatched. Correlates
    through nothing, by construction — see the module docstring."""


@dataclass(frozen=True, slots=True)
class Attempt:
    """One money-moving action the agent actually dispatched, as the world
    sees it.

    Built by the runner from a Proposal the executor really executed, never
    from one that was merely proposed: an action the guards blocked did not
    happen, and asking the world whether it would have worked would credit
    the agent with recoveries it never attempted.
    """

    episode_id: str
    failure_class: FailureClass
    intervention: InterventionType
    role: ProposalRole
    attempt: int
    amount_paise: int
    effective_at: datetime
    """When the action reached the world — not when it was decided. The
    calendar test below reads this, for the same reason
    vasool/policy/facts.py::GuardContext keeps `effective_at` separate from
    `now`."""

    salary_timed: bool
    """Whether the agent *aimed* this retry at a salary window, as opposed to
    landing in one by luck.

    An explicit field rather than something inferred from the intervention,
    because that inference is exactly what ablation A2 changes: A2 keeps
    LIQUIDITY's intervention and removes its timing. Deriving it here would
    make the ablation unable to express itself.
    """


@dataclass(frozen=True, slots=True)
class Ruling:
    """What the world decided, and enough of why to audit it."""

    money_arrives: bool
    channel: SettlementChannel | None
    probability: float
    parameter: str
    """The rule that decided this, by name."""

    depends_on: tuple[str, ...]
    """Which registered parameters fed that rule. §7 sweeps each parameter
    independently and needs to know which conclusions a given parameter could
    have touched; a single `parameter` label is not enough where a rule
    composes two of them (the lucky-window case composes the out-of-window
    rate with the uplift)."""

    draw_value: float
    """The uniform variate itself. Kept so a surprising run can be traced to
    the draw rather than argued about."""


_NO_MONEY = ("no_money_moved", ())


@dataclass(frozen=True, slots=True)
class OutcomeModel:
    """§4, applied. Immutable, so a sweep produces a new model rather than
    mutating the one a run is already using."""

    parameters: dict[str, Parameter]
    seed: int

    def _value(self, name: str) -> float:
        return self.parameters[name].value

    # -- the question ------------------------------------------------------
    def rule_on(self, attempt: Attempt) -> Ruling:
        """Given an intervention on an episode at a time, does money arrive?"""
        probability, parameter, depends_on, channel = self._price(attempt)
        variate = draw(
            self.seed,
            attempt.episode_id,
            SETTLEMENT,
            attempt.attempt,
            attempt.role.value,
        )
        arrives = variate < probability
        return Ruling(
            money_arrives=arrives,
            channel=channel if arrives else None,
            probability=probability,
            parameter=parameter,
            depends_on=depends_on,
            draw_value=variate,
        )

    def _price(
        self, attempt: Attempt
    ) -> tuple[float, str, tuple[str, ...], SettlementChannel | None]:
        """The §4 lookup: which registered rate applies, and through which
        channel the money would land."""
        if attempt.role is not ProposalRole.PRIMARY:
            # A nudge and a pre-debit notice are messages. They can cause a
            # customer to act, but the acting shows up as the sibling retry
            # succeeding or as an out-of-band payment — never as the message
            # itself moving money, which would credit the taxonomy with a
            # recovery it never performed.
            return 0.0, *_NO_MONEY, None

        if attempt.intervention in RETRY_INTERVENTIONS:
            probability, parameter, depends_on = self._retry_rate(attempt)
            return probability, parameter, depends_on, SettlementChannel.RETRY_CAPTURE

        if attempt.intervention in CONTACT_INTERVENTIONS:
            name = (
                "reauth_link_completion"
                if attempt.intervention is InterventionType.REAUTH_LINK
                else "reattempt_link_completion"
            )
            # §4 qualifies the re-auth rate "in-window" and registers no
            # out-of-window value. The same rate is applied either way: it
            # denies Vasool any modelled recovery credit for
            # ContactWindowGuard, which is the anti-Vasool direction §4's
            # asymmetry commitment requires and which inflates F5 against the
            # project's own thesis. Delivery itself is not modelled as
            # failing — no delivery-failure parameter is registered, and
            # inventing one would be a ninth parameter.
            return self._value(name), name, (name,), SettlementChannel.LINK_PAID

        # HUMAN_QUEUE. It never reaches an executor at all
        # (vasool/policy/machine.py escalates first), so this is unreachable
        # from the runner and exists so that a future intervention type fails
        # visibly here rather than silently settling nothing.
        return 0.0, *_NO_MONEY, None

    def _retry_rate(self, attempt: Attempt) -> tuple[float, str, tuple[str, ...]]:
        """§4's retry rows, plus the two readings it does not cover."""
        if attempt.failure_class is FailureClass.INSTRUMENT_DEAD:
            # §4's one non-guess, and definitional: an expired card
            # authorising is not a low-probability event, it is not an event.
            name = "retry_success_instrument_dead"
            return self._value(name), name, (name,)

        if attempt.failure_class is FailureClass.LIQUIDITY:
            return self._liquidity_rate(attempt)

        if attempt.failure_class is FailureClass.TRANSIENT:
            # §4 registers this "at attempt 1" and nothing for attempts 2 and
            # 3, which gateway_technical_error's ladder reaches. Applied flat
            # rather than decayed: real gateway failures presumably decay
            # across attempts, so a flat rate rewards persistence — which is
            # what the baselines do and Vasool does not. Deliberately the
            # anti-Vasool reading, not an oversight.
            name = "retry_success_transient"
            return self._value(name), name, (name,)

        # CUSTOMER_ACTION and RISK_BLOCK. §4 prices no retry against either,
        # because Vasool never retries either — only naive_retry and ablation
        # A1 get here. Set to the highest registered retry rate: only the
        # baselines make these retries, so generous is the anti-Vasool
        # direction, and zero would flatter Vasool by construction.
        name = "retry_success_unpriced_class"
        return self._value(name), name, (name,)

    def _liquidity_rate(self, attempt: Attempt) -> tuple[float, str, tuple[str, ...]]:
        """taxonomy §6's hypothesis, as three registered numbers.

        The gap between aiming and missing IS the salary-timing claim, and
        ablation A2 is a test of the ordering asserted here: missing a window
        < landing in one by luck < aiming for one.
        """
        if not in_salary_window(attempt.effective_at.astimezone(IST).date()):
            name = "retry_success_liquidity_out_of_window"
            return self._value(name), name, (name,)

        if attempt.salary_timed:
            name = "retry_success_liquidity_in_window"
            return self._value(name), name, (name,)

        # In a salary window, but not aimed at one — what naive_retry and
        # ablation A2 do when their fixed backoff happens to land on payday.
        # This is what §4's uplift multiplier prices, and the only thing it
        # prices: it is never stacked on the in-window rate, because 0.55
        # against 0.15 is already a 3.67x gap and multiplying again would
        # double-count the same hypothesis and hand A2 a result it did not
        # earn. Capped at the aimed rate so that luck can never beat aim,
        # which no sweep should be able to invert.
        base = self._value("retry_success_liquidity_out_of_window")
        uplifted = min(base * self._value("salary_window_uplift"), 1.0)
        return (
            uplifted,
            "retry_success_liquidity_lucky_window",
            ("retry_success_liquidity_out_of_window", "salary_window_uplift"),
        )

    # -- the world acting on its own --------------------------------------
    def out_of_band_at(
        self, episode_id: str, *, arrived_at: datetime, horizon_days: int
    ) -> datetime | None:
        """When, if ever, this customer pays through some other channel.

        A per-episode-day Bernoulli at §4's registered rate, scanned forward
        from the episode's arrival and returning the first day it fires.

        **Computed from the episode alone, never from what the agent did.**
        A customer paying elsewhere is a fact about the world, so it has to be
        identical across all four arms — otherwise the arms are not running on
        the same universe and EVALUATION.md §5's premise fails. Because each
        day is an independent addressed draw, lengthening the horizon can only
        reveal later occasions; it can never move or erase an earlier one, so
        an arm that closes an episode sooner still sees the same world.

        The time of day is uniform within the day. That is not a registered
        parameter and is not treated as one: a uniform here is the absence of
        a choice, where any shaped distribution would be a ninth guess.
        """
        rate = self._value("out_of_band_per_episode_day")
        for day in range(horizon_days):
            if draw(self.seed, episode_id, OUT_OF_BAND, day) < rate:
                hour = uniform(0.0, 24.0, self.seed, episode_id, OUT_OF_BAND, "hour", day)
                return arrived_at + timedelta(days=day, hours=hour)
        return None
