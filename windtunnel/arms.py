"""EVALUATION.md §5's baselines and §8's ablations, as configurations.

**An arm is a configuration of the real agent, never a second agent.** Every
arm below runs the same `PolicyMachine`, the same fifteen guard objects, the
same executor and the same ledger; what differs is the §4 table it classifies
against, whether the guard chain is present, and how the chain's verdicts
resolve. That is the whole design. An arm that reimplemented the state machine
would make every number downstream a comparison of two codebases rather than
of two policies, which is the one thing §6a's paired differences cannot
survive.

The seam this rests on is three defaulted parameters in `vasool/` —
`taxonomy.lookup(rules=)`, `rules.classify(rules=)`, and
`PolicyMachine(rules=, resolve=)`. Nothing else in the agent changed, and with
the defaults in place production behaves exactly as before.

**Why a table and not a callable.** Expressing an arm as its own classifier
function was the obvious alternative and it is worse for A2 specifically: A2
removes salary-aware timing, and a windtunnel-authored classifier would then
compute its own backoff — so the A2-vs-Vasool comparison would partly measure
my arithmetic against `vasool/diagnosis/rules.py`'s. As a table, A2 is one
field on one row, and the timing still flows through the real `_retry_at`, the
real `_scheduled` and the real `hold_out_of_quiet_hours`.

**What an arm may not change.** The key set. `normalise()` still resolves
against the registered `known_reasons()`, and every table here has exactly the
registered keys — a baseline is a different policy *for* a reason, never a
claim that a different set of reasons is real. the project's rule against
inventing error strings holds here exactly as it holds in the agent, and
tests/windtunnel/test_arms.py enforces it.

**One thing deliberately not ablated.** §8: the `RISK_BLOCK` rule stays in
every ablation that could have removed it. It was never justified on recovery
grounds — taxonomy §2 defends it on four harm-based arguments — so measuring
its recovery cost would answer a question nobody asked and invite the answer
"then remove it". A1 does route risk declines as `TRANSIENT`, which is why §8
cut the separate RISK_BLOCK ablation as overlapping.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum

from vasool.diagnosis.taxonomy import (
    RULES,
    SOURCE_ANY,
    TRANSIENT_BACKOFF,
    FailureClass,
    InterventionType,
    Rule,
)
from vasool.diagnosis.rules import unchanged
from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.registry import GUARD_CHAIN, evaluate_all
from vasool.policy.verdict import ChainResult, Decision

RuleTable = dict[tuple[str, str], Rule]
Resolver = Callable[[GuardContext, tuple[Guard, ...]], ChainResult]

PASSING: frozenset[Decision] = frozenset({Decision.ALLOW, Decision.NOT_APPLICABLE})
"""Decisions that let a short-circuiting chain keep going. Everything else is
a refusal, which is where the design spec's chain stopped."""


class ArmKind(StrEnum):
    AGENT = "agent"
    BASELINE = "baseline"
    ABLATION = "ablation"


@dataclass(frozen=True, slots=True)
class Arm:
    """One policy over the shared universe."""

    name: str
    kind: ArmKind
    rationale: str
    rules: RuleTable
    chain: tuple[Guard, ...] = GUARD_CHAIN
    resolve: Resolver = evaluate_all
    reconciles: bool = True
    """Whether this arm stops when the rail shows the money arrived elsewhere.

    Reconciliation is **policy**, not a guard: docs/taxonomy.md §7's hard stop
    on out-of-band success is a rule about what the agent should do, and §5.1
    and §5.2 are arms with no policy at all — retry everything, then maybe send
    a link. An arm that had no taxonomy but still knew when to stop would not
    be the strawman §5 defines, and the comparison would credit the taxonomy
    with something the baseline had too.

    This is the seam re-run #4 got wrong in the other direction, where
    rail-keyed classification reached every arm and diluted every ablation
    (§10, 2026-09-17). Registered explicitly this time, before the run: `False`
    for `naive_retry` and `retry_plus_contact`, `True` for everything that
    carries §4's table (§10, 2026-09-21).
    """

    upi_rule: Callable[[Rule], Rule] = unchanged
    """What this arm does to docs/taxonomy.md §12's rule for a UPI failure.

    An arm is an edit to §4's table, and §4's table never classifies a UPI
    payment — §12's does. Until 2026-09-17 that meant every arm classified a
    UPI failure exactly as Vasool does, so on `upi_mandate_share` ×
    `mandate_share` = 0.175 of episodes the comparison compared nothing
    (EVALUATION.md §10, re-run #4). Each arm now says what it is on that rail
    in the same terms §5 and §8 already use; the identity is Vasool's, and the
    arms whose definition is about guards or chain resolution keep it.
    """


# ---------------------------------------------------------------------------
# A4's resolution rule
# ---------------------------------------------------------------------------
def first_refusal(ctx: GuardContext, chain: tuple[Guard, ...]) -> ChainResult:
    """The design spec's short-circuit: stop at the first guard that refuses.

    `vasool/policy/registry.py` replaced this with all-then-resolve-by-severity
    and argues at length for the change — a receipt naming every violated
    clause rather than whichever guard happened to be first, and chain order
    becoming presentation only. Ablation A4 measures whether that correction
    mattered or whether it fixed a bug that never fired.

    **Only the guards actually consulted appear in the result.** The tempting
    alternative — run every guard and mark the suppressed ones
    NOT_APPLICABLE — would put a false verdict in the ledger §2a scans, because
    NOT_APPLICABLE is a jurisdiction claim and not a record of not having been
    asked. A short list is the honest representation of a short-circuited
    chain, and it is also what makes A4's receipts visibly different from
    Vasool's.
    """
    verdicts = []
    for guard in chain:
        verdict = guard.evaluate(ctx)
        verdicts.append(verdict)
        if verdict.decision not in PASSING:
            break
    return ChainResult.of(verdicts)


# ---------------------------------------------------------------------------
# table builders
# ---------------------------------------------------------------------------
def _uniform(rule: Rule) -> RuleTable:
    """One rule for every registered key. The key set is preserved; only the
    policy behind each key changes."""
    return {key: rule for key in RULES}


def _amended(**by_reason: Rule) -> RuleTable:
    """The registered table with named rows replaced, keyed on reason.

    Only reaches source-agnostic rows, which is every row an ablation below
    touches. `payment_failed`'s three source-specific rows are never amended
    by name — A1 replaces the whole table instead.
    """
    table = dict(RULES)
    for reason, rule in by_reason.items():
        table[(reason, SOURCE_ANY)] = rule
    return table


NAIVE_RULE = Rule(
    failure_class=FailureClass.TRANSIENT,
    retry_budget=3,
    retry_intervention=InterventionType.SILENT_RETRY,
    retry_delays=TRANSIENT_BACKOFF,
    post_retry=None,
    rationale=(
        "EVALUATION.md §5.1's strawman: retry every failure on fixed "
        "exponential backoff until the attempt cap, regardless of reason. No "
        "classification, no contact. Labelled TRANSIENT because that is what "
        "an agent with no taxonomy implicitly assumes every failure is — "
        "something that will clear if you try again."
    ),
)
"""§5.1. Note that the label is the *arm's belief*, and the simulator does not
price outcomes on it: `windtunnel/runner.py` hands the outcome model the
world's class, resolved through the registered table. An arm cannot earn a
recovery by being wrong about what failed."""


def _always(rule: Rule) -> Callable[[Rule], Rule]:
    """Every UPI reason takes one rule, whatever §12 says it is.

    The other rail's `_uniform`. An arm defined as "regardless of reason"
    (§5.1) or "no taxonomy" (§8's A1) means it on both rails or it does not
    mean it.
    """

    def apply(_: Rule) -> Rule:
        return rule

    return apply


def _when(failure_class: FailureClass, amend: Callable[[Rule], Rule]) -> Callable[[Rule], Rule]:
    """Amend §12's rule for one class and leave the rest of the table alone.

    The other rail's `_amended`. Keyed on the class rather than on the reason
    because §12 is a class table: one rule serves every reason that maps to
    it, where §4 registers a row per reason.
    """

    def apply(rule: Rule) -> Rule:
        return amend(rule) if rule.failure_class is failure_class else rule

    return apply


UNINFORMATIVE_ROW = RULES[("payment_failed", "gateway")]
"""§10, 2026-08-23: A1's row. The generic reason's single probe, not
`gateway_technical_error`'s three — an agent with no classification has no
basis for knowing a gateway problem is a gateway problem, and handing it the
informed row's budget would give it knowledge the ablation removes."""


VASOOL = Arm(
    name="vasool",
    kind=ArmKind.AGENT,
    rationale=(
        "The registered configuration: docs/taxonomy.md §4's table, all "
        "fifteen guards in registry.py's order, resolved all-then-by-severity. "
        "Every other arm is this with one thing changed."
    ),
    rules=RULES,
)

BASELINES: tuple[Arm, ...] = (
    Arm(
        name="naive_retry",
        reconciles=False,
        kind=ArmKind.BASELINE,
        rationale=(
            "§5.1 — the strawman worth beating: retry everything on 5m/30m/4h "
            "until the cap, no classification, no guards, no contact. Beating "
            "it proves very little, which §5 says outright; it is here because "
            "it is what most of the field builds, and because it is the arm "
            "that makes the cost of futile retries visible."
        ),
        rules=_uniform(NAIVE_RULE),
        upi_rule=_always(NAIVE_RULE),
        chain=(),
    ),
    Arm(
        name="retry_plus_contact",
        reconciles=False,
        kind=ArmKind.BASELINE,
        rationale=(
            "§5.2 — the realistic incumbent, and **the baseline that matters**: "
            "naive_retry plus a payment link once the retries exhaust. No "
            "taxonomy, no compliance layer. F1 is registered against this arm, "
            "so if the paired interval against it includes zero, classification "
            "bought no recovery and the taxonomy is a compliance artifact "
            "rather than a recovery improvement."
        ),
        rules=_uniform(replace(NAIVE_RULE, post_retry=InterventionType.REATTEMPT_LINK)),
        upi_rule=_always(replace(NAIVE_RULE, post_retry=InterventionType.REATTEMPT_LINK)),
        chain=(),
    ),
    Arm(
        name="vasool_ungated",
        kind=ArmKind.BASELINE,
        rationale=(
            "§5.3 — Vasool's full taxonomy and timing with the guard chain "
            "removed, deliberately adversarial to this project's own thesis. "
            "Expected to beat full Vasool on raw recovery; the gap is the price "
            "of the guards and is reported as a headline number rather than "
            "hidden. If the guards cost nothing, the guard chain's claim is "
            "decorative. F5 is registered against this arm at 20 absolute "
            "percentage points (§10, 2026-08-23)."
        ),
        rules=RULES,
        chain=(),
    ),
)

ABLATIONS: tuple[Arm, ...] = (
    Arm(
        name="A1",
        kind=ArmKind.ABLATION,
        rationale=(
            "§8 — no taxonomy: every failure takes the uninformative row's "
            "single probe and re-attempt link. Tests whether classification "
            "does anything at all; §8 is blunt that if A1 matches full Vasool, "
            "docs/taxonomy.md is decoration. Also routes risk declines as "
            "TRANSIENT, which is why §8 cut the separate RISK_BLOCK ablation "
            "as overlapping with this one."
        ),
        rules=_uniform(UNINFORMATIVE_ROW),
        upi_rule=_always(UNINFORMATIVE_ROW),
    ),
    Arm(
        name="A2",
        kind=ArmKind.ABLATION,
        rationale=(
            "§8 — no salary-aware timing: LIQUIDITY keeps its budget, its "
            "nudge, its escalation and its class, and loses only §6's salary "
            "ladder for the registered fixed backoff. Tests taxonomy §6, the "
            "argument most specific to India. §4 warns that A2's result is "
            "largely determined by the 0.55/0.15 in-window pair and says to "
            "treat its headline number as untrustworthy until §7 reports."
        ),
        rules=_amended(
            insufficient_fund=replace(
                RULES[("insufficient_fund", SOURCE_ANY)],
                salary_aware=False,
                retry_delays=TRANSIENT_BACKOFF,
            )
        ),
        # §12's LIQUIDITY rule *is* §4's insufficient_fund row, so the ablation
        # transfers without a second judgement about what it means.
        upi_rule=_when(
            FailureClass.LIQUIDITY,
            lambda rule: replace(rule, salary_aware=False, retry_delays=TRANSIENT_BACKOFF),
        ),
    ),
    Arm(
        name="A3",
        kind=ArmKind.ABLATION,
        rationale=(
            "§8 — no zero-retry rule: the two INSTRUMENT_DEAD rows registered "
            "at zero retries get the single probe their class would otherwise "
            "get (§10, 2026-08-23). The flagship claim, that a futile retry "
            "costs a budget the re-auth link needed. Not three retries: three "
            "would test a strawman, and the claim is about the cost of one."
        ),
        rules=_amended(
            card_expired=replace(
                RULES[("card_expired", SOURCE_ANY)],
                retry_budget=1,
                retry_intervention=InterventionType.SILENT_RETRY,
                retry_delays=(timedelta(minutes=15),),
            ),
            card_disabled_for_online_payments=replace(
                RULES[("card_disabled_for_online_payments", SOURCE_ANY)],
                retry_budget=1,
                retry_intervention=InterventionType.SILENT_RETRY,
                retry_delays=(timedelta(minutes=15),),
            ),
        ),
        # The same probe on the other rail's zero-retry rule. INSTRUMENT_DEAD is
        # 0.27 of the registered UPI mix against card_expired's 0.05 of §3d's,
        # so the flagship claim is tested at a weight that can move it.
        upi_rule=_when(
            FailureClass.INSTRUMENT_DEAD,
            lambda rule: replace(
                rule,
                retry_budget=1,
                retry_intervention=InterventionType.SILENT_RETRY,
                retry_delays=(timedelta(minutes=15),),
            ),
        ),
    ),
    Arm(
        name="A4",
        kind=ArmKind.ABLATION,
        rationale=(
            "§8 — the guard chain short-circuits on the first refusal, in the "
            "design spec's cheapest-first order, which registry.py already "
            "uses. Session 3's own correction, put to the test: whether "
            "all-then-resolve-by-severity actually mattered, or whether it "
            "fixed a bug that never fired. The visible difference is in the "
            "receipts — one cited clause rather than every violated one."
        ),
        rules=RULES,
        resolve=first_refusal,
    ),
    Arm(
        name="A5",
        kind=ArmKind.ABLATION,
        rationale=(
            "§8 — no escalation: every row keeps its retry budget and loses "
            "its post-retry action, so retries exhaust and the episode stops "
            "with no link. Tests whether the 'then a link' half of §4's "
            "escalation rows earns its place or whether the retries do all the "
            "work. This is also the arm that makes EXHAUSTED reachable — "
            "taxonomy §9.11 records that no registered row ends without an "
            "escalation, so the state is unreachable through the real "
            "classifier and its zero means 'cannot happen'."
        ),
        rules={key: replace(rule, post_retry=None) for key, rule in RULES.items()},
        upi_rule=lambda rule: replace(rule, post_retry=None),
    ),
)

ALL_ARMS: tuple[Arm, ...] = (VASOOL, *BASELINES, *ABLATIONS)

_BY_NAME = {arm.name: arm for arm in ALL_ARMS}


def arm_named(name: str) -> Arm:
    """Look an arm up by the name it is reported under.

    Raises rather than returning None: an unknown arm name in a driver or a
    report means a comparison silently did not happen, which is worse than a
    crash.
    """
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a registered arm — EVALUATION.md §5 and §8 "
            f"register {sorted(_BY_NAME)}"
        ) from None
