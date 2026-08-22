"""TRAI: commercial messages go out on a registered template, or not at all.

Blocks rather than defers. A registration could in principle be added — but not
on any schedule anyone can name, and the defer-vs-block rule in this package is
that a deferral requires a concrete instant at which the condition expires. "A
human might register this template eventually" is not one.

The template a proposal carries is what *we* would send. This checks it against
what the *merchant* has registered. A template we can emit and the merchant has
not registered is exactly the case this exists to catch, so the two sets are
deliberately kept apart rather than derived from each other.
"""
from __future__ import annotations

from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict


class DLTTemplateGuard(Guard):
    name = "DLTTemplateGuard"
    statute = "TRAI TCCCPR — DLT template registration (Feb 2025 amendment)"

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.is_contact

    def check(self, ctx: GuardContext) -> Verdict:
        template = ctx.proposal.template_id
        if template is None:
            return self.block("outbound message carries no DLT template id")
        if template not in ctx.facts.registered_templates:
            return self.block(f"template {template!r} is not registered to this merchant")
        return self.allow()
