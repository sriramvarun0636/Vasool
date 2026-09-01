"""The Guard contract.

A guard is a pure function from a GuardContext to a Verdict. No I/O, no clock
beyond the two times on the context, no store — everything it needs was
snapshotted before the chain ran (vasool/policy/facts.py). That is what makes
the thirteen property-testable over a generated input space rather than over
three hand-written cases, and it is the claim the whole compliance argument
rests on.

`evaluate()` is final and does three things the individual guards must not be
trusted to remember:

  1. **Jurisdiction.** A guard with nothing to say returns NOT_APPLICABLE, not
     ALLOW. "The DLT template rule does not apply to a silent retry" and "the
     DLT template rule passed this message" are different claims and the report
     card must not render both as a tick.

  2. **Fail closed on missing facts.** A guard that declares a fact in
     `requires` never runs when that fact is None; the action is blocked
     instead. Without this, a bug in fact loading silently disables a compliance
     guard while every test still passes — the worst failure mode available to
     a system like this, because it is invisible.

  3. **Deferral progress.** A DEFER whose `defer_until` is not strictly after
     `effective_at` is a livelock, so it raises rather than returning. The
     Verdict model already refuses a DEFER with no time at all; this catches the
     subtler version where a guard computes one that has already passed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar, final

from vasool.policy.facts import GuardContext
from vasool.policy.verdict import Decision, Obligation, Verdict


class Guard(ABC):
    """One compliance rule. Pure."""

    name: ClassVar[str]
    statute: ClassVar[str | None] = None
    """The clause enforced, printed in the report card. None where the rule is
    self-imposed — a merchant's spend ceiling is not regulation and must not be
    dressed up as one in the single artefact a compliance reader will read
    closely."""

    requires: ClassVar[frozenset[str]] = frozenset()
    """PolicyFacts fields this guard cannot judge without.

    Name only facts whose None means *unknown*. `promise_to_pay` is None when
    the customer made no promise — known, and absent — so a guard that declared
    it would refuse every action for want of a promise nobody made.

    A guard reading a fact it did not declare is a bug this cannot catch, so
    declare honestly. tests/test_guard_properties.py checks that every declared
    name is a real field, which catches the typo but not the omission.
    """

    def applies_to(self, ctx: GuardContext) -> bool:
        """Whether this guard has jurisdiction over this proposal."""
        return True

    @abstractmethod
    def check(self, ctx: GuardContext) -> Verdict:
        """The rule. Called only when applies_to and every required fact is
        present."""

    @final
    def evaluate(self, ctx: GuardContext) -> Verdict:
        if not self.applies_to(ctx):
            return self.not_applicable()

        missing = sorted(name for name in self.requires if getattr(ctx.facts, name) is None)
        if missing:
            return self.block(
                f"cannot judge: {', '.join(missing)} unavailable. A fact we have "
                "not established is not the same as one we have established to "
                "be harmless, so this fails closed."
            )

        verdict = self.check(ctx)
        if verdict.guard != self.name:
            raise ValueError(f"{type(self).__name__} returned a verdict for {verdict.guard!r}")
        if verdict.defer_until is not None and verdict.defer_until <= ctx.effective_at:
            raise ValueError(
                f"{self.name} deferred to {verdict.defer_until}, which is not after "
                f"{ctx.effective_at} — a deferral that does not advance is a livelock"
            )
        return verdict

    # -- verdict constructors, so that `guard` and `statute` are never
    # -- transcribed by hand at a call site
    def allow(self, reason: str | None = None) -> Verdict:
        """Permitted. `reason` is optional and exists for the guards whose
        ALLOW is surprising to read.

        `RiskBlockGuard` is the case that motivated it: on a risk decline the
        taxonomy has already chosen `HUMAN_QUEUE`, so the guard has nothing left
        to refuse and allows — which, printed in a chain next to the words
        "Card network norms", reads as the guard failing. It is the opposite.
        A receipt that cannot be read correctly is a worse audit trail than a
        terse one.
        """
        return Verdict(
            guard=self.name, decision=Decision.ALLOW, reason=reason, statute=self.statute
        )

    def not_applicable(self) -> Verdict:
        return Verdict(guard=self.name, decision=Decision.NOT_APPLICABLE)

    def block(self, reason: str) -> Verdict:
        return Verdict(
            guard=self.name, decision=Decision.BLOCK, reason=reason, statute=self.statute
        )

    def escalate(self, reason: str) -> Verdict:
        return Verdict(
            guard=self.name, decision=Decision.ESCALATE, reason=reason, statute=self.statute
        )

    def defer(
        self, until: datetime, reason: str, obligations: tuple[Obligation, ...] = ()
    ) -> Verdict:
        return Verdict(
            guard=self.name,
            decision=Decision.DEFER,
            reason=reason,
            statute=self.statute,
            defer_until=until,
            obligations=obligations,
        )
