"""Template-bound sending. The second line behind DLTTemplateGuard, not a
replacement for it.

vasool/policy/guards/dlt_template.py already blocks any proposal whose
template is missing or unregistered before it ever reaches the executor — on
the normal path, this module's checks never fire. They exist for the caller
that skips the chain: a hand-built proposal, a bug in wiring, a future
executor method that forgets to gate. The module that actually sends a
message must not be able to send an unregistered one even then, or the guard
is a convention rather than an invariant.

This module does not know how a message reaches a phone or an inbox — that is
`deliver`, injected by the caller. Keeping the transport out of comms.py means
its own tests never need a Razorpay mock (CLAUDE.md: "no real API calls in
tests"), and the actual channel integration is free to change without
touching the one thing that must never change: a message with no template, or
one the merchant hasn't registered, does not go out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from vasool.diagnosis.proposal import Proposal

Deliverer = Callable[[Proposal, dict], dict]
"""Actually reaches the channel, given the proposal and any extra params
(e.g. a payment-link URL). Wired in vasool/actions/executor.py, the only
module allowed to know Razorpay is on the other end of some of these calls.

# VERIFY: no SMS/WhatsApp/email provider is integrated for a message that
carries no payment link (NUDGE, PRE_DEBIT_NOTICE). Razorpay's own API has no
generic transactional-message endpoint — `payment_link.notifyBy` only covers
links, so a real deliverer for those two roles is later work this session
does not build. executor.py's default deliverer says so explicitly rather
than pretending to have sent something.
"""


class CommsRefused(Exception):
    """comms.py declined to attempt a send at all. Distinct from a delivery
    failure — `deliver` raising is a channel problem; this is comms.py doing
    its one job."""


@dataclass
class CommsSender:
    deliver: Deliverer

    def send(
        self,
        *,
        proposal: Proposal,
        registered_templates: frozenset[str],
        params: dict | None = None,
    ) -> dict:
        if proposal.template_id is None:
            raise CommsRefused(
                f"{proposal.proposal_id}: no template_id — comms.py sends "
                "template-bound messages only"
            )
        if proposal.template_id not in registered_templates:
            raise CommsRefused(
                f"{proposal.proposal_id}: template {proposal.template_id!r} is "
                "not registered to this merchant — second line behind "
                "DLTTemplateGuard"
            )
        if proposal.channel is None:
            raise CommsRefused(f"{proposal.proposal_id}: no channel set")
        return self.deliver(proposal, params or {})
