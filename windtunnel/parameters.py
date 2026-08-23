"""Every number this simulator runs on, with its provenance, in one place.

docs/EVALUATION.md §4 requires each parameter to carry a provenance tag "in
the simulator's own source, not only in this document", and that "a parameter
with no tag fails a test". It cannot even be constructed: `Parameter` has no
default for `provenance`. What tests/windtunnel/test_parameters.py adds is the
stronger check — that every value and tag here is the value and tag the
pre-registered document actually registers, parsed out of the markdown in both
directions. Editing either side alone fails.

**Two registries, because two things are registered.** `OUTCOME_PARAMETERS`
holds §4's eight, which decide whether money arrives. `WORLD_PARAMETERS` holds
the world-shape numbers §3d does not register — how many episodes a customer
has, when they arrive, how large they are, who consented. §3d fixes the
failure-reason mix and the customer count and stops there, but §3a's whole
argument for randomising at the customer level is that customers share a
frequency-cap budget across episodes, which is vacuous if every customer has
exactly one episode. So the shape had to be chosen, and choosing it silently
would be exactly the drift §3c exists to prevent. They are registered under
§10 and swept under §7 like anything else.

**On `[derived]`.** §4 defines it as "computed from a `[cited]` figure by
stated arithmetic". This project has no cited figures — §4 says so at length —
so anything computed from a guess is itself a guess, and is tagged that way.
Letting `[derived]` launder a number would make the 7/8 guess fraction, which
§4 calls a headline result in its own right, quietly false. The one
`[derived]` below is §4's own registered INSTRUMENT_DEAD zero, kept as the
document tags it: re-tagging it here would be editing code to make a
pre-registered document look consistent, which is backwards.

**What is deliberately not a parameter.** Several application rules follow
from the registered eight without adding a ninth, and windtunnel/outcome.py
documents each at its use site rather than inventing a knob for it: a
TRANSIENT retry uses the same rate at every attempt; an out-of-window
REAUTH_LINK uses the registered in-window rate; a LIQUIDITY retry that lands
in a salary window without having been aimed at one gets the registered
out-of-window rate times the registered uplift.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class Provenance(StrEnum):
    """§4's three tags. Closed, like every other enum in this codebase."""

    CITED = "cited"
    """Traced to a named external source, recorded in docs/VERIFIED.md.
    Nothing in this project carries this tag, and §4 explains why: conditional
    retry-success probabilities at this granularity are not published by
    anyone, and inventing a citation for them "would be the first dishonest
    sentence in this repository"."""

    DERIVED = "derived"
    """Computed from a [cited] figure by stated arithmetic. With no cited
    figures in existence, only §4's definitional zero holds this tag."""

    GUESS = "guess"
    """My judgement, with no external support."""


UNIT_INTERVAL = (0.0, 1.0)
"""Bounds for a probability. §7 sweeps every parameter at ±50%, and +50% on a
0.97 rate would otherwise register 1.455 as a probability — a certainty the
parameter cannot express. Clamping keeps the top of the sweep meaningful."""


@dataclass(frozen=True, slots=True)
class Parameter:
    """One number, and everything a reader needs to attack it.

    `provenance` has no default on purpose — see the module docstring.
    """

    name: str
    value: float
    provenance: Provenance
    registered_in: str
    """Which section of docs/EVALUATION.md registers this: "§4" for the
    pre-registered outcome model, "§10" for a world-shape parameter added by
    amendment before any holdout run."""

    note: str
    """§4: "so a reader can attack the reasoning rather than the number"."""

    registered_as: str | None = None
    """The verbatim §4 row label, for the parameters §4 registers. None for
    everything added under §10. tests/windtunnel/test_parameters.py matches on
    this string, so the correspondence with the document is exact rather than
    by convention."""

    bounds: tuple[float, float] | None = None
    """Range a sweep may not leave. UNIT_INTERVAL for probabilities."""


def swept(
    registry: dict[str, Parameter], name: str, factor: float
) -> dict[str, Parameter]:
    """§7's sensitivity sweep: one parameter scaled, every other held fixed.

    Returns a new registry; the original is never mutated, so a sweep cannot
    leak into the run that follows it. Raises KeyError on an unknown name
    rather than silently sweeping nothing — a sweep that does nothing would
    report "survives all sweeps" for a conclusion nobody actually tested.

    §7 itself (the four-point −50/−25/+25/+50 grid, and what to conclude from
    it) belongs to the evaluator, not here. This is only the mechanism.
    """
    parameter = registry[name]
    value = parameter.value * factor
    if parameter.bounds is not None:
        low, high = parameter.bounds
        value = min(max(value, low), high)
    return {**registry, name: replace(parameter, value=value)}


# ---------------------------------------------------------------------------
# §4 — the outcome model. Pre-registered; every value and tag here is asserted
# against the document itself by tests/windtunnel/test_parameters.py.
# ---------------------------------------------------------------------------
OUTCOME_PARAMETERS: dict[str, Parameter] = {
    "retry_success_transient": Parameter(
        name="retry_success_transient",
        value=0.35,
        provenance=Provenance.GUESS,
        registered_in="§4",
        registered_as="P(success | `SILENT_RETRY` on `TRANSIENT`, attempt 1)",
        bounds=UNIT_INTERVAL,
        note=(
            "§4: gateway blips clear on their own; roughly a third clearing "
            "within one backoff step is defensible and deliberately not "
            "generous. Applied flat across every TRANSIENT attempt rather "
            "than decayed — see windtunnel/outcome.py, where the flatness is "
            "argued as anti-Vasool rather than left to look like an oversight."
        ),
    ),
    "retry_success_liquidity_in_window": Parameter(
        name="retry_success_liquidity_in_window",
        value=0.55,
        provenance=Provenance.GUESS,
        registered_in="§4",
        registered_as="P(success | `TIMED_RETRY` on `LIQUIDITY`, in salary window)",
        bounds=UNIT_INTERVAL,
        note=(
            "§4: the most consequential guess in the file, because the gap "
            "between this and the out-of-window rate IS taxonomy §6's "
            "salary-timing hypothesis expressed as a parameter. A2's result "
            "is largely determined by it, and §4 says to treat A2's headline "
            "number as untrustworthy until §7 reports on it."
        ),
    ),
    "retry_success_liquidity_out_of_window": Parameter(
        name="retry_success_liquidity_out_of_window",
        value=0.15,
        provenance=Provenance.GUESS,
        registered_in="§4",
        registered_as="P(success | `TIMED_RETRY` on `LIQUIDITY`, outside window)",
        bounds=UNIT_INTERVAL,
        note=(
            "§4: the other half of the salary-timing pair. Also the base the "
            "uplift multiplies for a retry that lands in a window without "
            "having been aimed at one — see salary_window_uplift."
        ),
    ),
    "retry_success_instrument_dead": Parameter(
        name="retry_success_instrument_dead",
        value=0.0,
        provenance=Provenance.DERIVED,
        registered_in="§4",
        registered_as="P(success | retry on `INSTRUMENT_DEAD`)",
        bounds=UNIT_INTERVAL,
        note=(
            "§4's one non-guess, and definitional rather than measured: an "
            "expired card authorising is not a low-probability event, it is "
            "not an event (taxonomy §5). Tagged [derived] because the "
            "pre-registered document tags it [derived]; see the module "
            "docstring on why that is not re-litigated here."
        ),
    ),
    "reauth_link_completion": Parameter(
        name="reauth_link_completion",
        value=0.25,
        provenance=Provenance.GUESS,
        registered_in="§4",
        registered_as="P(customer completes `REAUTH_LINK` | delivered, in-window)",
        bounds=UNIT_INTERVAL,
        note=(
            "§4: completion requires re-entering full card details on a page "
            "arriving unprompted; friction is high and trust is low. Applied "
            "regardless of whether the send was inside the contact window, "
            "because no out-of-window rate is registered and using this one "
            "denies Vasool any modelled recovery credit for ContactWindowGuard "
            "— the direction §4's asymmetry commitment requires."
        ),
    ),
    "reattempt_link_completion": Parameter(
        name="reattempt_link_completion",
        value=0.35,
        provenance=Provenance.GUESS,
        registered_in="§4",
        registered_as="P(customer completes `REATTEMPT_LINK` | delivered)",
        bounds=UNIT_INTERVAL,
        note=(
            "§4: same flow as a re-auth, less friction — the instrument "
            "already works, so this is a retry the customer chose rather than "
            "a new instrument they must supply."
        ),
    ),
    "out_of_band_per_episode_day": Parameter(
        name="out_of_band_per_episode_day",
        value=0.02,
        provenance=Provenance.GUESS,
        registered_in="§4",
        registered_as="P(out-of-band settlement, per episode-day)",
        bounds=UNIT_INTERVAL,
        note=(
            "§4: rare but non-zero, and the parameter that drives A07 into "
            "the run at all. It does not produce recovery in this simulator "
            "— an out-of-band payment is structurally unattributable "
            "(taxonomy §9.9), so what it produces is double-collection "
            "exposure. See windtunnel/outcome.py."
        ),
    ),
    "salary_window_uplift": Parameter(
        name="salary_window_uplift",
        value=2.0,
        provenance=Provenance.GUESS,
        registered_in="§4",
        registered_as="Salary-window balance uplift multiplier",
        note=(
            "§4: a round number, chosen because it is arguable in both "
            "directions rather than because anything supports it. It is never "
            "applied on top of the registered in-window rate — that pair is "
            "already a 3.67x gap and stacking would double-count. It is what "
            "prices a retry that LANDS in a salary window without having been "
            "timed for one, which is the only thing an unregistered arm "
            "(naive_retry, ablation A2) ever does."
        ),
    ),
    # -- Added under §10. Not in §4, so tests assert it carries no
    # `registered_as` and is tagged [guess].
    "retry_success_unpriced_class": Parameter(
        name="retry_success_unpriced_class",
        value=0.35,
        provenance=Provenance.GUESS,
        registered_in="§10",
        bounds=UNIT_INTERVAL,
        note=(
            "§4 prices no retry against CUSTOMER_ACTION or RISK_BLOCK, "
            "because Vasool never retries either. The baselines do: "
            "naive_retry retries every failure regardless of reason, and "
            "ablation A1 routes everything as TRANSIENT. Set to the highest "
            "registered retry rate, because only the baselines make these "
            "retries and generous is therefore the anti-Vasool direction — "
            "zero would flatter Vasool by construction, which §4's asymmetry "
            "commitment forbids."
        ),
    ),
}


# ---------------------------------------------------------------------------
# §10 — world shape. Everything §3d leaves open, registered by amendment
# before any holdout run, and swept under §7 like anything else.
# ---------------------------------------------------------------------------
WORLD_PARAMETERS: dict[str, Parameter] = {
    "episodes_per_customer_lambda": Parameter(
        name="episodes_per_customer_lambda",
        value=1.0,
        provenance=Provenance.GUESS,
        registered_in="§10",
        note=(
            "Episodes per customer is 1 + Poisson(lambda), truncated at 6. At "
            "lambda 1.0 roughly 63% of customers have more than one episode, "
            "which is what §3a's argument requires: customers share a "
            "frequency-cap budget and a contact history, and a universe where "
            "almost everyone has exactly one episode makes the stratification "
            "argument decoration and FrequencyCapGuard unreachable."
        ),
    ),
    "episode_arrival_window_days": Parameter(
        name="episode_arrival_window_days",
        value=60.0,
        provenance=Provenance.GUESS,
        registered_in="§10",
        note=(
            "Failures arrive uniformly across a 60-day window. Two months "
            "rather than one so that every phase of taxonomy §6's monthly "
            "salary cycle is exercised — two month-ends and two 1st-7th "
            "windows — rather than a single payday deciding the whole run."
        ),
    ),
    "inter_episode_gap_mean_days": Parameter(
        name="inter_episode_gap_mean_days",
        value=9.0,
        provenance=Provenance.GUESS,
        registered_in="§10",
        note=(
            "A customer's later episodes follow the first at Exponential(mean "
            "9 days) spacing. Chosen so that a little over half of "
            "consecutive pairs fall inside FrequencyCapGuard's 7-day window "
            "and the rest outside it: a spacing that never triggers the cap "
            "would leave it untested, and one that always did would make "
            "every second episode a compliance block."
        ),
    ),
    "settlement_drain_days": Parameter(
        name="settlement_drain_days",
        value=45.0,
        provenance=Provenance.GUESS,
        registered_in="§10",
        note=(
            "How long the clock runs past the last arrival before the run is "
            "cut. Long enough for the longest trajectory the agent can "
            "schedule — taxonomy §6's three-rung salary ladder plus its "
            "escalation link, roughly 40 days — so that an episode is never "
            "counted as unrecovered merely because the simulation stopped."
        ),
    ),
    "amount_median_rupees": Parameter(
        name="amount_median_rupees",
        value=1200.0,
        provenance=Provenance.GUESS,
        registered_in="§10",
        note=(
            "Failed payments are log-normal with this median. Every payload "
            "in data/ is Rs 500 because they came from one test checkout, so "
            "an amount distribution had to be chosen; a constant would leave "
            "AFAThresholdGuard, HumanApprovalGuard and SpendCapGuard "
            "permanently unexercised and the report card would show three of "
            "the thirteen as passing when they had never run."
        ),
    ),
    "amount_sigma_log": Parameter(
        name="amount_sigma_log",
        value=1.4,
        provenance=Provenance.GUESS,
        registered_in="§10",
        note=(
            "Spread of the amount distribution, on the log scale. Sized so a "
            "few percent of episodes clear the Rs 15,000 AFA threshold and a "
            "fraction of a percent clear the Rs 50,000 human-approval "
            "threshold — rare, as they should be, but present in every seed "
            "rather than only in lucky ones."
        ),
    ),
    "consent_on_file_rate": Parameter(
        name="consent_on_file_rate",
        value=0.97,
        provenance=Provenance.GUESS,
        registered_in="§10",
        bounds=UNIT_INTERVAL,
        note=(
            "Share of customers with a DPDP consent record covering payment "
            "recovery. The remainder have none at all, which is not a "
            "permissive state: ConsentGuard declares consent in `requires`, "
            "so a missing record fails closed and blocks. Non-zero because a "
            "world where consent is always on file never exercises the guard "
            "the whole DPDP claim rests on."
        ),
    ),
    "consent_withdrawn_rate": Parameter(
        name="consent_withdrawn_rate",
        value=0.02,
        provenance=Provenance.GUESS,
        registered_in="§10",
        bounds=UNIT_INTERVAL,
        note=(
            "Share of customers who withdraw consent partway through the "
            "run, at a uniformly drawn moment. This is what puts adversary "
            "attack A12 and §2a's 'no action after consent withdrawal' claim "
            "into the run at all — a safety predicate nothing ever tests is "
            "not evidence."
        ),
    ),
    "dnd_listed_rate": Parameter(
        name="dnd_listed_rate",
        value=0.08,
        provenance=Provenance.GUESS,
        registered_in="§10",
        bounds=UNIT_INTERVAL,
        note=(
            "Share of customers on TRAI's DND registry. Inert today and "
            "registered anyway: DNDGuard.applies_to only returns True for "
            "PROMOTIONAL messages and every message this system sends is "
            "TRANSACTIONAL, so the guard is NOT_APPLICABLE throughout. "
            "vasool/diagnosis/proposal.py's own VERIFY note says that "
            "categorisation is genuinely unsettled, and if it moves this "
            "parameter becomes load-bearing overnight."
        ),
    ),
    "mandate_share": Parameter(
        name="mandate_share",
        value=0.35,
        provenance=Provenance.GUESS,
        registered_in="§10",
        bounds=UNIT_INTERVAL,
        note=(
            "Share of customers paying by e-mandate rather than one-time. "
            "Decides three guards at once: RetryCapGuard's ceiling (4 vs 3), "
            "PreDebitNoticeGuard's 24-hour notice, and whether "
            "AFAThresholdGuard applies at all. No subscription payload has "
            "ever been observed on this account (docs/VERIFIED.md), so this "
            "is unanchored in a way most of the others are not."
        ),
    ),
    "promise_to_pay_rate": Parameter(
        name="promise_to_pay_rate",
        value=0.05,
        provenance=Provenance.GUESS,
        registered_in="§10",
        bounds=UNIT_INTERVAL,
        note=(
            "Share of episodes where the customer has promised a date. Small "
            "because a promise requires an interaction most failed payments "
            "never produce, and non-zero because PromiseToPayGuard is one of "
            "the two guards §3a names as coupling a customer's episodes "
            "together."
        ),
    ),
    "promise_horizon_days": Parameter(
        name="promise_horizon_days",
        value=10.0,
        provenance=Provenance.GUESS,
        registered_in="§10",
        note=(
            "A promise lands uniformly within this many days of the failure. "
            "Long enough that the hold PromiseToPayGuard applies genuinely "
            "collides with taxonomy §6's retry ladder rather than expiring "
            "before the first attempt comes due."
        ),
    ),
}


# ---------------------------------------------------------------------------
# §3d — the registered failure-reason mix. Pre-registered; asserted against
# the document in both directions.
# ---------------------------------------------------------------------------
REASON_MIX: tuple[tuple[str, float], ...] = (
    ("payment_failed", 0.30),
    ("insufficient_fund", 0.22),
    ("card_declined", 0.12),
    ("gateway_technical_error", 0.10),
    ("payment_timed_out", 0.08),
    ("payment_cancelled", 0.07),
    ("card_expired", 0.05),
    ("card_disabled_for_online_payments", 0.03),
    ("card_number_invalid", 0.02),
    ("payment_risk_check_failed", 0.01),
)
"""§3d, verbatim and in the document's own order.

Registered as `[guess]` in the document itself: no source publishes an Indian
card-decline mix at this granularity, and §3d says defaulting to a uniform mix
"that no real merchant has ever seen" would be worse than an honest guess. The
shape exercises all five failure classes and lets the generic case dominate,
which is the one thing docs/VERIFIED.md does establish.

§3d also warns what rests on it: the mix decides the headline recovery rate,
and `card_expired` at 0.05 means the flagship claim rests on roughly one
episode in twenty, so F2's interval will be wider than the others.
"""

PAYMENT_FAILED_SOURCE_MIX: tuple[tuple[str, float], ...] = (
    ("gateway", 0.70),
    ("bank", 0.25),
    ("business", 0.05),
)
"""§3d's "(gateway / bank / business split 70/25/5)".

This split is what makes the generic reason exercise three different classes:
taxonomy §4 routes payment_failed/gateway to TRANSIENT, /bank to
INSTRUMENT_DEAD and /business to RISK_BLOCK. Only the first two have ever been
observed live — see windtunnel/payloads.py for how the third is assembled
without typing either string.
"""
