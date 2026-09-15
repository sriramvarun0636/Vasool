"""What a failed UPI Autopay debit gets: one rule per outcome.

Two vocabularies reach here. Razorpay's 61 documented reasons
(vasool/diagnosis/razorpay_upi.py), which is what a Razorpay merchant is sent,
and NPCI's 225 codes (vasool/diagnosis/npci.py), which reach a merchant only
through a provider that passes the rail's own code on — the `RailCodeSource`
port (vasool/events/rail_codes.py), whose default passes nothing. Both map to
the same outcomes, and an outcome decides the rule, so the two cannot drift
into different treatment of the same failure. docs/taxonomy.md §12 argues each
rule (docs/EVALUATION.md §10, 2026-09-15).

**Nothing unmapped is retried.** §4's fail-safe for an unknown card reason is
one silent retry. That is exactly wrong on UPI: a third of these reasons say
money may already have moved. So an undocumented UPI reason, and every
`Unmapped` outcome, goes to a human — except `RECONCILE`, which gets a status
check, because asking the rail is the one thing that is both safe and useful
when a debit may have happened.

**The budgets are §4's, fitted to NPCI's cap.** A UPI mandate allows "1 attempt
and 3 retries per mandate (per sequence number)" (OC-215A row 5), which
`RetryCapGuard` enforces whatever a row asks for. LIQUIDITY's three-rung salary
ladder fits it exactly.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from vasool.diagnosis import npci, razorpay_upi
from vasool.diagnosis.npci import Unmapped
from vasool.diagnosis.taxonomy import RULES, FailureClass, InterventionType, Rule

__all__ = ["UPI_FAILSAFE", "rule_for_outcome", "rule_for_rail_code", "rule_for_reason"]

STATUS_CHECK_AFTER = timedelta(seconds=90)
"""NPCI OC-215 ¶3: the first status check "after 90 seconds from the
initiation/authentication of the original transaction"."""

_BY_CLASS: dict[FailureClass, Rule] = {
    FailureClass.TRANSIENT: Rule(
        failure_class=FailureClass.TRANSIENT,
        retry_budget=1,
        retry_intervention=InterventionType.SILENT_RETRY,
        retry_delays=(timedelta(minutes=30),),
        post_retry=InterventionType.REATTEMPT_LINK,
        rationale=(
            "A bank or PSP failed before any debit. One retry tests a weak prior — on a "
            "mandate it waits for its own pre-debit notice and NPCI's off-peak hours "
            "anyway — then the customer is asked to pay."
        ),
    ),
    # §4's insufficient_fund row, whole: the salary ladder is a claim about a
    # bank balance, and a UPI account's balance keeps the same calendar.
    FailureClass.LIQUIDITY: replace(
        RULES[("insufficient_fund", "*")],
        rationale=(
            "The account is short today. Time the retry for payday, three rungs — "
            "exactly NPCI's three retries — with one soft nudge, then a link."
        ),
    ),
    FailureClass.INSTRUMENT_DEAD: Rule(
        failure_class=FailureClass.INSTRUMENT_DEAD,
        retry_budget=0,
        post_retry=InterventionType.REAUTH_LINK,
        rationale=(
            "This mandate cannot be debited as it stands. A retry has no chance and "
            "spends one of NPCI's three; the customer is asked for a new mandate."
        ),
    ),
    FailureClass.CUSTOMER_ACTION: Rule(
        failure_class=FailureClass.CUSTOMER_ACTION,
        retry_budget=0,
        post_retry=InterventionType.REATTEMPT_LINK,
        rationale=(
            "Something only the customer can do — set a PIN, resume a paused mandate. "
            "Presenting the debit again cannot do it for them; they are asked to."
        ),
    ),
    FailureClass.RISK_BLOCK: Rule(
        failure_class=FailureClass.RISK_BLOCK,
        retry_budget=0,
        post_retry=InterventionType.HUMAN_QUEUE,
        rationale=(
            "A risk engine declined. Nothing automated, ever — no retry and no message; "
            "a human decides."
        ),
    ),
}

_RECONCILE = Rule(
    # The class is TRANSIENT only because a Diagnosis must carry one: the
    # receipt's `failure_class` says nothing moved it to a customer or an
    # instrument. What the rule does is the point, and it does not retry.
    failure_class=FailureClass.TRANSIENT,
    retry_budget=0,
    post_retry=InterventionType.STATUS_CHECK,
    post_retry_delay=STATUS_CHECK_AFTER,
    rationale=(
        "Money may already have moved. A retry could be a second debit, and a message "
        "could ask for money already paid; the rail is asked what happened instead "
        "(NPCI OC-215)."
    ),
)

_TO_A_HUMAN = Rule(
    failure_class=FailureClass.TRANSIENT,
    retry_budget=0,
    post_retry=InterventionType.HUMAN_QUEUE,
    rationale=(
        "This failure fits none of the five classes, and no automated response to it "
        "is safe to assume: a person decides."
    ),
)

UPI_FAILSAFE = replace(
    _TO_A_HUMAN,
    rationale=(
        "A UPI reason nobody has documented. §4's one silent retry is not taken: on UPI "
        "an unknown failure may have moved money, so a person decides."
    ),
)
"""An undocumented UPI reason. Not §4's fail-safe, which retries."""


def rule_for_outcome(outcome: FailureClass | Unmapped, why: str) -> Rule:
    """The rule for one outcome, carrying `why` — the reason's own evidence —
    ahead of the rule's argument, so the receipt says both."""
    evidence = f"{why[:1].upper()}{why[1:]}."
    if isinstance(outcome, FailureClass):
        return replace(_BY_CLASS[outcome], rationale=f"{evidence} {_BY_CLASS[outcome].rationale}")
    # An Unmapped outcome has no class, and a Diagnosis must carry one of five:
    # the proposal says TRANSIENT as a placeholder, so the rationale names the
    # outcome first and a receipt cannot be read as calling it transient
    # (docs/taxonomy.md §12, known limits).
    base = _RECONCILE if outcome is Unmapped.RECONCILE else _TO_A_HUMAN
    return replace(base, rationale=f"Unmapped ({outcome.value}). {evidence} {base.rationale}")


def rule_for_reason(reason: str) -> tuple[str, Rule]:
    """The rule for a Razorpay UPI reason, and the reason as matched.

    Case and surrounding whitespace are folded, as §4's `normalise` folds them.
    A reason the page does not document returns `UPI_FAILSAFE` under the
    sentinel `unknown`, the same sentinel §4 uses, so the unknown bucket stays
    countable across both rails."""
    folded = reason.strip().lower()
    mapping = razorpay_upi.BY_REASON.get(folded)
    if mapping is None:
        return "unknown", UPI_FAILSAFE
    return folded, rule_for_outcome(mapping.outcome, mapping.why)


def rule_for_rail_code(section: str, code: str) -> tuple[str, Rule]:
    """The rule for an NPCI code that reached us through a `RailCodeSource`."""
    mapping = npci.BY_KEY.get((section, code))
    if mapping is None:
        return "unknown", UPI_FAILSAFE
    return f"npci:{section}:{code}", rule_for_outcome(mapping.outcome, mapping.why)
