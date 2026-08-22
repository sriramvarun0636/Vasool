"""Anti-harassment. Two caps, and they resolve differently.

**Three contacts per customer per seven days: DEFER.** This condition expires.
The oldest contact ages out of the window at a computable instant, so there is a
real time to name and the recovery survives.

**Two contacts per recovery episode: BLOCK.** This condition does not expire. An
episode does not get shorter by waiting, so a deferral here would be a deferral
forever — the exact pathology the defer-vs-block rule exists to prevent.

Both caps appear in both stopping-rule tables (design spec §5, taxonomy §7) and
in none of the spec's thirteen guards. They live together here because they
count the same thing over different windows, and because a reader looking for
"how often do we message someone" should find one file.

The episode cap is two because §4's insufficient_fund row spends exactly two —
one soft nudge, then a re-attempt link once three timed retries have failed. The
row was built to sit at the cap rather than to breach it.
"""
from __future__ import annotations

from datetime import timedelta

from vasool.policy.facts import GuardContext
from vasool.policy.guards.base import Guard
from vasool.policy.verdict import Verdict

FREQUENCY_CAP_WINDOW = timedelta(days=7)
FREQUENCY_CAP_COUNT = 3
"""Design spec §5 and taxonomy §7: at most three outbound contacts per customer
per seven days. Self-imposed, in the spirit of the Fair Practices Code rather
than in satisfaction of a numbered clause."""

EPISODE_CONTACT_CAP = 2
"""At most two contacts in one recovery episode."""


class FrequencyCapGuard(Guard):
    name = "FrequencyCapGuard"
    statute = "RBI Fair Practices Code (anti-harassment)"

    def applies_to(self, ctx: GuardContext) -> bool:
        return ctx.proposal.is_contact

    def check(self, ctx: GuardContext) -> Verdict:
        # Checked first: one verdict per guard, and the cap that can never
        # expire has to win, or we would schedule an action that is already
        # dead and call it a compliance save.
        if ctx.facts.episode_contacts >= EPISODE_CONTACT_CAP:
            return self.block(
                f"{ctx.facts.episode_contacts} contacts already sent in this "
                f"episode (cap {EPISODE_CONTACT_CAP}) — one is a reminder, two "
                "is pressure, and an episode does not get shorter by waiting"
            )

        window_opens = ctx.effective_at - FREQUENCY_CAP_WINDOW
        in_window = sorted(
            t for t in ctx.facts.contact_history if window_opens < t <= ctx.effective_at
        )
        if len(in_window) < FREQUENCY_CAP_COUNT:
            return self.allow()

        # The oldest contact leaves the window exactly one window-length after
        # it was sent, and that is the first instant a further contact is
        # permitted. Strictly after effective_at by construction, since every
        # member of in_window is strictly after window_opens.
        return self.defer(
            in_window[0] + FREQUENCY_CAP_WINDOW,
            f"{len(in_window)} contacts in the last {FREQUENCY_CAP_WINDOW.days}d "
            f"(cap {FREQUENCY_CAP_COUNT})",
        )
