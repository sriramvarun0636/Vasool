"""The (error_reason, error_source) -> class -> intervention table.

This module is docs/taxonomy.md §2-§4 expressed as code and nothing else.
Timing arithmetic is §6 and lives in vasool/diagnosis/rules.py. The policy
plane and the guards read the budgets and delays from here rather than
re-deriving them, so there is exactly one place where "how many retries does
an expired card get" is answered.

Three rules govern edits to this file:

1. **Closed enums.** FailureClass and InterventionType hold exactly the values
   docs/taxonomy.md names, and no others. When the LLM classifier lands in
   Session 7, its structured output gets parsed through these enums — so an
   invented class or an invented action fails at the boundary instead of
   reaching the policy plane. Adding a member here is a taxonomy change and
   belongs in the document first.

2. **No invented error strings.** Every canonical reason below has a payload in
   data/observed_payloads/ or data/stubbed_payloads/ (the project rules). That is
   enforced mechanically by
   tests/test_taxonomy.py::TestProvenance::test_every_mapped_reason_has_a_payload,
   not by discipline.

3. **Only `payment_failed` branches on error_source** (§3). Every other reason
   is keyed on SOURCE_ANY, so source is ignored for it structurally rather
   than by convention. VERIFIED.md records why: a netbanking failure returned
   `bank` where cards return `gateway`, on an otherwise identical payload, so
   the pair carries signal exactly where the reason carries none. Source is
   noisy — a later netbanking attempt also returned `gateway` — which is why
   it only narrows the branch for the one uninformative reason and never
   overrides a specific one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

log = logging.getLogger(__name__)


class FailureClass(StrEnum):
    """The five classes of docs/taxonomy.md §2. Closed."""

    TRANSIENT = "TRANSIENT"
    """Rail, bank or gateway hiccup. Retry silently; do not contact."""

    LIQUIDITY = "LIQUIDITY"
    """Instrument is fine, the money isn't there today. Time-shift the retry."""

    INSTRUMENT_DEAD = "INSTRUMENT_DEAD"
    """The method cannot succeed again in its current state. Get a new one."""

    CUSTOMER_ACTION = "CUSTOMER_ACTION"
    """A human has to do something. Never blind-retry."""

    RISK_BLOCK = "RISK_BLOCK"
    """A fraud or risk engine declined this. Nothing automated, ever."""


class InterventionType(StrEnum):
    """Every intervention appearing in §4's Intervention column. Closed.

    Note what is absent: there is no REFUND, no CANCEL, no DISCOUNT, no
    ALT_METHOD. If the policy plane ever needs one, it gets argued into
    docs/taxonomy.md and added here, not improvised at the call site.
    """

    SILENT_RETRY = "SILENT_RETRY"
    """Re-present the same instrument. No customer contact."""

    TIMED_RETRY = "TIMED_RETRY"
    """A retry whose timing is the intervention (§6 salary-aware)."""

    REATTEMPT_LINK = "REATTEMPT_LINK"
    """Ask the customer to complete the same payment again."""

    REAUTH_LINK = "REAUTH_LINK"
    """Ask the customer for a different / re-authorised instrument."""

    HUMAN_QUEUE = "HUMAN_QUEUE"
    """Hand to an operator. Not an automated action."""


RETRY_INTERVENTIONS: frozenset[InterventionType] = frozenset(
    {InterventionType.SILENT_RETRY, InterventionType.TIMED_RETRY}
)
"""The interventions that re-present the instrument, i.e. spend attempt budget."""

CONTACT_INTERVENTIONS: frozenset[InterventionType] = frozenset(
    {InterventionType.REATTEMPT_LINK, InterventionType.REAUTH_LINK}
)
"""The interventions that reach the customer, i.e. spend contact budget."""


SOURCE_ANY = "*"
"""Key sentinel: this reason ignores error_source (the `—` column in §4).

Not a Razorpay value — Razorpay's sources are customer / business / bank /
gateway / network, of which we have observed four.
"""

UNKNOWN_REASON = "unknown"
"""Sentinel returned by normalise() for a reason with no row.

Never a wire value. It exists so the unknown bucket is countable — §5: "the
unknown bucket filling up is how we learn the API changed."
"""


TRANSIENT_BACKOFF: tuple[timedelta, ...] = (
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=4),
)
"""§6: 5m -> 30m -> 4h. Gateway problems usually clear in minutes; if four
hours hasn't fixed it, more retries won't either.

This ladder belongs to the one row that earns three attempts
(`gateway_technical_error`). The other TRANSIENT rows get a single retry at
their own delay, because an uninformative error deserves *less* budget than a
specific one, not more (§5).
"""


@dataclass(frozen=True, slots=True)
class Rule:
    """One row of docs/taxonomy.md §4.

    `retry_budget` retries happen first, then `post_retry` fires once. A row
    with `retry_budget == 0` goes straight to `post_retry`, which is why that
    field is the first action for card_expired and payment_risk_check_failed
    alike.
    """

    failure_class: FailureClass
    rationale: str
    """One line, traceable to §5. Goes into the Proposal and then the Receipt —
    an audit trail has to say why, not just what."""

    retry_budget: int = 0
    """How many times the instrument may be re-presented. This is the number
    §5 argues about; the policy plane must read it, not pick its own."""

    retry_intervention: InterventionType | None = None
    """What a retry looks like. None iff retry_budget == 0."""

    retry_delays: tuple[timedelta, ...] = ()
    """Delay before each retry, indexed by attempt. Empty when salary_aware."""

    salary_aware: bool = False
    """Timing comes from §6's salary ladder rather than a fixed delay."""

    post_retry: InterventionType | None = None
    """What happens once the retry budget is spent.

    Every row in §4 names one. §5, on `gateway_technical_error`: three retries
    and then nothing is a broken product — someone whose payment keeps failing
    should eventually be asked to pay another way. That also settles §2's rule
    that a TRANSIENT escalates to contact on exhaustion, which two rows used to
    contradict.

    The type stays optional because a row that deliberately ends in silence (the
    policy plane's EXHAUSTED terminal) is a coherent thing to want. No current
    row does, and tests/test_taxonomy.py holds that line — so adding one means
    arguing it into the document first.
    """

    post_retry_delay: timedelta = timedelta(0)
    """Delay before the post-retry action. Zero is §4's "Immediate"."""

    soft_nudge: bool = False
    """§4's "+ soft nudge". LIQUIDITY only, and capped at one (§2)."""

    explain: bool = False
    """§4's "+ explain": the message must name the specific cause, because for
    this row the wording is what decides whether it works (§5)."""


# ---------------------------------------------------------------------------
# §4. The mapping.
#
# Keyed on (error_reason, error_source). SOURCE_ANY is the `—` column: source
# ignored. Only payment_failed has source-specific keys — see §3 and the module
# docstring.
# ---------------------------------------------------------------------------
RULES: dict[tuple[str, str], Rule] = {
    # -- payment_failed: the only reason reproducible against live test mode,
    # and the one a real merchant sees most often. Uninformative on its own, so
    # error_source is the only discriminating field available (§3).
    ("payment_failed", "gateway"): Rule(
        failure_class=FailureClass.TRANSIENT,
        retry_budget=1,
        retry_intervention=InterventionType.SILENT_RETRY,
        retry_delays=(timedelta(minutes=15),),
        post_retry=InterventionType.REATTEMPT_LINK,
        rationale=(
            "The rail failed on our side. One retry tests a weak prior; spending "
            "three of four attempts on a hypothesis this thin is how the budget "
            "gets wasted. Then hand the decision to the customer, who has "
            "information we don't."
        ),
    ),
    ("payment_failed", "bank"): Rule(
        failure_class=FailureClass.INSTRUMENT_DEAD,
        retry_budget=1,
        retry_intervention=InterventionType.SILENT_RETRY,
        retry_delays=(timedelta(hours=6),),
        post_retry=InterventionType.REAUTH_LINK,
        rationale=(
            "The issuer declined — the failure is downstream of us, at the "
            "institution holding the money. Behaves like card_declined: one "
            "six-hour probe to cover a soft decline, then treat the instrument "
            "as dead."
        ),
    ),
    # Ordered above the catch-all to match §4's table. Precedence is structural
    # rather than positional — lookup() tries the exact (reason, source) key
    # before falling back to SOURCE_ANY — so reordering this dict cannot change
    # the answer. The order is for whoever reads it against the document.
    ("payment_failed", "business"): Rule(
        failure_class=FailureClass.RISK_BLOCK,
        retry_budget=0,
        post_retry=InterventionType.HUMAN_QUEUE,
        rationale=(
            "Precautionary, not evidential. Our only risk-decline payload "
            "carries this source, but the value was hand-set in "
            "tools/make_stubs.py from documentation and has never been observed "
            "— so 'business means a risk decline' is our own stub read back to "
            "us, not a finding. The argument is the asymmetry: routing it to a "
            "human costs one recoverable failure waiting on an operator, while "
            "retrying it costs an automated re-presentation of a declined "
            "authorisation. Those are not the same size, and the probability "
            "need not be known to see it."
        ),
        # VERIFY: what `business` actually denotes. One captured live
        # payment_failed/business payload settles this row either way — see
        # docs/taxonomy.md §9.7.
    ),
    ("payment_failed", SOURCE_ANY): Rule(
        failure_class=FailureClass.TRANSIENT,
        retry_budget=1,
        retry_intervention=InterventionType.SILENT_RETRY,
        retry_delays=(timedelta(minutes=30),),
        post_retry=InterventionType.HUMAN_QUEUE,
        rationale=(
            "An unfamiliar source on an uninformative reason. One silent retry "
            "is the least harmful possible action; then a human. The source "
            "value is logged — an unfamiliar source is itself operational signal."
        ),
    ),
    # -- the clean transient
    ("gateway_technical_error", SOURCE_ANY): Rule(
        failure_class=FailureClass.TRANSIENT,
        retry_budget=3,
        retry_intervention=InterventionType.SILENT_RETRY,
        retry_delays=TRANSIENT_BACKOFF,
        post_retry=InterventionType.REATTEMPT_LINK,
        rationale=(
            "Explicitly a gateway problem: nothing about the customer or the "
            "instrument is implicated, and the customer likely never noticed. "
            "Three retries, because here we actually know what broke. Then a "
            "link: if four hours of backoff hasn't cleared it, the "
            "self-healing-blip hypothesis is dead, and three retries followed "
            "by nothing is a broken product."
        ),
    ),
    # -- ambiguous by construction
    ("payment_timed_out", SOURCE_ANY): Rule(
        failure_class=FailureClass.TRANSIENT,
        retry_budget=1,
        retry_intervention=InterventionType.SILENT_RETRY,
        retry_delays=(timedelta(minutes=10),),
        post_retry=InterventionType.REATTEMPT_LINK,
        rationale=(
            "A timeout means we don't know what happened — the authorisation "
            "may have succeeded and the response been lost. Retry exactly once; "
            "repeatedly retrying a transaction whose outcome is unknown is how "
            "double-charges happen. Out-of-band success cancels this (A07)."
        ),
    ),
    # -- the highest-value class
    ("insufficient_fund", SOURCE_ANY): Rule(
        failure_class=FailureClass.LIQUIDITY,
        retry_budget=3,
        retry_intervention=InterventionType.TIMED_RETRY,
        salary_aware=True,
        soft_nudge=True,
        post_retry=InterventionType.REATTEMPT_LINK,
        rationale=(
            "The instrument works and the customer intends to pay; the money "
            "isn't there today. The most recoverable failure in the taxonomy, "
            "and timing does the work rather than persistence — retry on payday, "
            "not on backoff. One soft nudge, because the customer can act. Then "
            "a link: three failures spanning two paydays means the timing "
            "hypothesis has been tested and lost. Nudge plus link is two "
            "contacts, which is exactly §7's per-episode cap."
        ),
    ),
    # -- the customer said no
    ("payment_cancelled", SOURCE_ANY): Rule(
        failure_class=FailureClass.CUSTOMER_ACTION,
        retry_budget=0,
        post_retry=InterventionType.REATTEMPT_LINK,
        post_retry_delay=timedelta(hours=2),
        rationale=(
            "Nothing is broken, but the customer expressed something close to "
            "intent. Exactly one link: not zero, because accidental cancellation "
            "is common; not two, because one is a reminder and two is pressure. "
            "The two-hour delay is the design — instant follow-up reads as being "
            "chased, which is what the RBI Fair Practices Code exists to prevent."
        ),
    ),
    # -- honestly ambiguous; the least certain row in the file
    ("card_declined", SOURCE_ANY): Rule(
        failure_class=FailureClass.INSTRUMENT_DEAD,
        retry_budget=1,
        retry_intervention=InterventionType.SILENT_RETRY,
        retry_delays=(timedelta(hours=6),),
        post_retry=InterventionType.REAUTH_LINK,
        rationale=(
            "Issuer-side decline with no stated reason; some issuers use it for "
            "soft declines, others for hard ones, and the data does not "
            "distinguish them. One probe covers the soft case. Two consecutive "
            "issuer declines is strong evidence the instrument is the problem."
        ),
        # VERIFY: "two consecutive declines means dead" is a heuristic, not a
        # fact. A merchant with real decline data could tune it; we can't.
        # docs/taxonomy.md §9.1 records this as a known limit.
    ),
    # -- dead right now, but the customer can revive it in a minute
    ("card_disabled_for_online_payments", SOURCE_ANY): Rule(
        failure_class=FailureClass.INSTRUMENT_DEAD,
        retry_budget=0,
        post_retry=InterventionType.REAUTH_LINK,
        explain=True,
        rationale=(
            "Indian banks commonly ship cards with online payments switched off "
            "and customers enable it per-card in their banking app. The card "
            "declines identically every time until a setting we cannot see is "
            "changed, so never retry — and the message must name the cause, "
            "because 'payment failed, try again' is useless here."
        ),
    ),
    # -- a typo
    ("card_number_invalid", SOURCE_ANY): Rule(
        failure_class=FailureClass.CUSTOMER_ACTION,
        retry_budget=0,
        post_retry=InterventionType.REATTEMPT_LINK,
        rationale=(
            "Retrying the same wrong digits produces the same failure forever. "
            "Re-attempt link immediately, within the contact window."
        ),
    ),
    # -- the flagship zero
    ("card_expired", SOURCE_ANY): Rule(
        failure_class=FailureClass.INSTRUMENT_DEAD,
        retry_budget=0,
        post_retry=InterventionType.REAUTH_LINK,
        rationale=(
            "Zero percent chance of succeeding — not low, zero. There is no "
            "state of the world in which the same expired card authorises on the "
            "third attempt. A retry has exactly zero expected value while "
            "consuming one of the four attempts the re-auth link needed."
        ),
    ),
    # -- the one that gets nothing
    ("payment_risk_check_failed", SOURCE_ANY): Rule(
        failure_class=FailureClass.RISK_BLOCK,
        retry_budget=0,
        post_retry=InterventionType.HUMAN_QUEUE,
        rationale=(
            "A fraud system declined this. Retrying may breach card-network "
            "rules and degrades a decline ratio the merchant cannot repair; if "
            "it was fraud, a retry loop is the fraudster's tool; if it was a "
            "false positive, an unexpected payment link to a possibly-"
            "compromised customer is structurally phishing. Hard stop, human "
            "queue, zero outbound."
        ),
    ),
}


UNMAPPED_RULE = Rule(
    failure_class=FailureClass.TRANSIENT,
    retry_budget=1,
    retry_intervention=InterventionType.SILENT_RETRY,
    retry_delays=(timedelta(minutes=30),),
    post_retry=InterventionType.HUMAN_QUEUE,
    rationale=(
        "A reason we have never seen. Razorpay's error list is longer than what "
        "we have observed and it will change. One silent retry is the least "
        "harmful possible action — no customer contact, no assumption about the "
        "instrument, minimal budget spent — then a human decides."
    ),
)
"""§4's last row: the fail-safe. Deliberately not a member of RULES — it is
what happens when the lookup misses, not a row you can look up."""


REASON_ALIASES: dict[str, str] = {
    # §5: Razorpay emits the SINGULAR. data/stubbed_payloads/ carries
    # `insufficient_fund`; the plural is what Razorpay's own documentation and
    # docs/VASOOL-design-spec.md §4.2 write. Trusting the plural would route
    # the single most recoverable class in the taxonomy to the unknown path — a
    # whole class of recoverable failures lost to a typo.
    #
    # The alias resolves toward the string we have a payload for. It is not a
    # claim that Razorpay emits the plural; it is insurance against us writing
    # it. Only aliases that resolve to an observed reason belong here.
    "insufficient_funds": "insufficient_fund",
}


def known_reasons() -> frozenset[str]:
    """Every canonical reason with a row in §4.

    Each one has a payload on disk; tests/test_taxonomy.py enforces the
    correspondence in both directions.
    """
    return frozenset(reason for reason, _ in RULES)


_SOURCE_SENSITIVE_REASONS: frozenset[str] = frozenset(
    reason for reason, source in RULES if source != SOURCE_ANY
)
"""Reasons with at least one source-specific row. §3 says this is exactly
{"payment_failed"}; tests/test_taxonomy.py asserts it."""


def normalise(reason: str) -> str:
    """Canonicalise a raw Razorpay error_reason.

    Applies REASON_ALIASES and confirms the result has a row in §4. Anything
    else returns UNKNOWN_REASON and is logged at WARN with the string exactly
    as it came off the wire — an unknown reason is operational signal, and the
    unknown bucket filling up is how we learn the API changed (§5).

    Case and surrounding whitespace are folded before lookup. Every reason
    observed so far is lowercase snake_case, so this is normalisation rather
    than interpretation; the WARN still reports the original string so a
    mis-cased new reason is still visible as new.
    """
    folded = reason.strip().lower()
    canonical = REASON_ALIASES.get(folded, folded)
    if canonical not in known_reasons():
        log.warning(
            "unmapped error_reason %r — classifying via the fail-safe row (§4). "
            "A new reason here means Razorpay's error list has moved; add a "
            "payload to data/ and a row to docs/taxonomy.md.",
            reason,
        )
        return UNKNOWN_REASON
    return canonical


def lookup(
    reason: str, source: str, *, rules: dict[tuple[str, str], Rule] = RULES
) -> tuple[str, Rule]:
    """Resolve (error_reason, error_source) to its §4 row.

    Returns the normalised reason alongside the rule so that callers get both
    from a single pass — normalise() logs, and logging the same unknown reason
    twice would make the unknown bucket a bad counter.

    §3: the lookup is keyed on the pair, but only `payment_failed` has
    source-specific rows. For every other reason the source-specific key is
    absent by construction, so the SOURCE_ANY row is the only one reachable.

    **`rules` exists so the wind tunnel can measure this table against
    alternatives** — EVALUATION.md §5's baselines and §8's ablations are each a
    different §4, and expressing them any other way would compare two
    codebases rather than two policies. It defaults to the registered table, so
    production and every existing caller are unchanged.

    What an alternative table may *not* change is which reasons exist:
    `normalise` still resolves against `known_reasons()`, which reads the
    registered table. A baseline is a different policy for a reason, never a
    claim that a different set of reasons is real — the project's rule against
    inventing error strings holds for the baselines exactly as it holds here.
    """
    canonical = normalise(reason)
    if canonical == UNKNOWN_REASON:
        return canonical, UNMAPPED_RULE

    specific = rules.get((canonical, source))
    if specific is not None:
        return canonical, specific

    if canonical in _SOURCE_SENSITIVE_REASONS:
        # §5: "Log the source value; an unfamiliar source is itself operational
        # signal." Only meaningful for a reason that branches on source at all.
        log.warning(
            "unfamiliar error_source %r on error_reason %r — falling back to the "
            "source-agnostic row (§4).",
            source,
            canonical,
        )

    return canonical, rules[(canonical, SOURCE_ANY)]
