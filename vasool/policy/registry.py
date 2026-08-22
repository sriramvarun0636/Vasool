"""The thirteen, and how they resolve into one decision.

**Every guard runs.** The design spec short-circuits on the first refusal and
orders the chain cheapest-first "so a blocked action short-circuits before you
spend an API call". No guard spends an API call — they are dictionary lookups
over a snapshot materialised in one pass before the chain starts — so the saving
is imaginary. The cost is not: the spec's own ordering places DNDGuard,
DLTTemplateGuard and SpendCapGuard *after* four deferring guards, so a 19:30 SMS
carrying an unregistered DLT template is deferred to 08:02, woken, and only then
refused. An action that was never going to be allowed gets scheduled, and the
report card shows a deferral that reads like a compliance save.

Running all thirteen fixes that and buys three things:

  - A receipt names every violated clause rather than whichever guard happened
    to be first. For a system whose headline artefact maps guards to statutes,
    that is the difference between "this action violated four rules" and "this
    action violated the one we checked."
  - Chain order becomes presentation only, which makes order-independence a
    property test rather than a convention (tests/test_registry.py).
  - Adding a guard can no longer silently change which statute a refusal is
    attributed to.

The order below is the spec's, kept because it reads well in a receipt:
cheapest and most absolute first, escalations last. Nothing depends on it.
"""
from __future__ import annotations

from vasool.policy.facts import GuardContext
from vasool.policy.guards.afa_threshold import AFAThresholdGuard
from vasool.policy.guards.base import Guard
from vasool.policy.guards.consent import ConsentGuard
from vasool.policy.guards.contact_window import ContactWindowGuard
from vasool.policy.guards.dlt_template import DLTTemplateGuard
from vasool.policy.guards.dnd import DNDGuard
from vasool.policy.guards.frequency_cap import FrequencyCapGuard
from vasool.policy.guards.human_approval import HumanApprovalGuard
from vasool.policy.guards.idempotency import IdempotencyGuard
from vasool.policy.guards.pre_debit_notice import PreDebitNoticeGuard
from vasool.policy.guards.promise_to_pay import PromiseToPayGuard
from vasool.policy.guards.retry_cap import RetryCapGuard
from vasool.policy.guards.risk_block import RiskBlockGuard
from vasool.policy.guards.spend_cap import SpendCapGuard
from vasool.policy.verdict import ChainResult

GUARD_CHAIN: tuple[Guard, ...] = (
    IdempotencyGuard(),       # have we already done this?
    RiskBlockGuard(),         # absolute prohibition
    ConsentGuard(),           # DPDP
    RetryCapGuard(),          # the platform's attempt ceiling
    PromiseToPayGuard(),      # the spec's QuietPeriodGuard, renamed
    DNDGuard(),               # TRAI scrub
    FrequencyCapGuard(),      # anti-harassment, two caps
    ContactWindowGuard(),     # RBI 08:00-19:00
    PreDebitNoticeGuard(),    # RBI 24h notice
    AFAThresholdGuard(),      # ₹15,000
    DLTTemplateGuard(),       # TRAI registered template
    SpendCapGuard(),          # merchant blast radius
    HumanApprovalGuard(),     # last: a person decides
)


def evaluate_all(ctx: GuardContext, chain: tuple[Guard, ...] = GUARD_CHAIN) -> ChainResult:
    """Run every guard and resolve by severity."""
    return ChainResult.of([guard.evaluate(ctx) for guard in chain])
