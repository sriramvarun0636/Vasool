"""One execution per action. Not a compliance rule — a correctness one.

Razorpay delivers every webhook at least twice with an identical event id
(docs/VERIFIED.md), so this is normal operation rather than a defensive
nicety. The event plane dedupes on `x-razorpay-event-id`; this is the backstop
for the delivery that gets past it, and for adversary attack A02, where the same
payment arrives under a *fresh* event id and the event-plane check has nothing
to match on.

The design spec keys this on `(event_id, intervention)`, which is wrong twice:

  - It collides across attempts. `gateway_technical_error` earns three silent
    retries for one event; under that key, retry 2 is a duplicate of retry 1 and
    a three-retry row silently becomes a one-retry row.
  - It misses A02 entirely, which is the harder half of the attack pair and the
    one the spec itself flags as "semantic idempotency (harder)".

Proposal.idempotency_key keys on the payment instead — see there.
"""
from __future__ import annotations

from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict


class IdempotencyGuard(Guard):
    name = "IdempotencyGuard"
    statute = None
    """Deliberately none. Deduplication is engineering, and listing it beside
    the RBI and DPDP citations would pad the compliance table with a row that
    enforces no clause."""

    def check(self, ctx: GuardContext) -> Verdict:
        key = ctx.proposal.idempotency_key
        if key in ctx.facts.executed_keys:
            return self.block(f"already executed: {key}")
        return self.allow()
