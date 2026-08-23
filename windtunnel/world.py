"""The FactStore the simulator hands the guards, and the bookkeeping behind it.

`vasool/policy/facts.py` calls the FactStore "the one impure thing in the
policy plane" and says it is "SQLite in production, a dict in the simulator".
This is that dict.

**Everything here is a fact about the world, never a decision about the
agent.** The guards read this snapshot and rule on it; nothing in this module
knows what a verdict is. The split matters because a simulator that could
reach into the compliance decision would be able to arrange the answers, which
is exactly what EVALUATION.md §1 says a reader should suspect.

**Why it has to record executions as they happen.** Three guards read history
this store owns rather than history the episode owns:

  - `FrequencyCapGuard` counts contacts to the *customer* across episodes in a
    rolling 7-day window — §3a's whole reason for randomising at the customer
    level.
  - `SpendCapGuard` counts value re-presented for the *merchant* today.
  - `PreDebitNoticeGuard` holds a mandate debit until a notice has been served,
    and will keep deferring until one has been. If the world never records the
    notice going out, every mandate retry defers five times and lands in
    BLOCKED — the guard would look broken when it was working.

So the runner records each execution into this store at the moment it happens,
before anything else can be gated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from vasool.diagnosis.proposal import Proposal, template_ids
from vasool.events.schemas import FailureEvent
from vasool.policy.facts import MerchantPolicy, PolicyFacts
from vasool.policy.guards.frequency_cap import FREQUENCY_CAP_WINDOW
from windtunnel.universe import Customer, PlannedEpisode, Universe

MERCHANT_ID = "acc_TSJqWjzP2eEdM3"
"""The merchant every simulated payment belongs to — the real `account_id` on
every envelope in data/, not an invented one.

One merchant, not several. A second merchant would only change SpendCapGuard's
daily ceiling arithmetic, which is self-imposed configuration rather than a
compliance rule, and splitting the universe across merchants would weaken
every per-merchant number for no claim anybody makes.
"""


def _ist_day(when: datetime) -> date:
    """The IST calendar day a moment falls in.

    IST because SpendCapGuard's own reset is IST midnight
    (vasool/policy/guards/spend_cap.py::_next_reset) — a UTC day here would
    put the simulator's accounting boundary five and a half hours away from
    the guard's, and the two would disagree every evening.
    """
    from vasool.diagnosis.rules import IST

    return when.astimezone(IST).date()


@dataclass
class WorldFactStore:
    """One read of the world, per gate. Implements vasool.policy.facts.FactStore."""

    universe: Universe
    merchant: MerchantPolicy = field(
        default_factory=lambda: MerchantPolicy(merchant_id=MERCHANT_ID)
    )

    _by_customer_id: dict[str, Customer] = field(default_factory=dict, init=False)
    _by_entity_id: dict[str, PlannedEpisode] = field(default_factory=dict, init=False)
    _contacts: dict[str, list[datetime]] = field(default_factory=dict, init=False)
    _spent: dict[tuple[str, date], int] = field(default_factory=dict, init=False)
    _notices: dict[str, datetime] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._by_customer_id = {c.customer_id: c for c in self.universe.customers}
        self._by_entity_id = {e.entity_id: e for e in self.universe.episodes}

    # -- the FactStore protocol -------------------------------------------
    def snapshot(
        self, *, event: FailureEvent, proposal: Proposal, now: datetime
    ) -> PolicyFacts:
        customer = self._by_customer_id[event.customer_id]
        episode = self._by_entity_id[event.entity_id]

        return PolicyFacts(
            merchant=self.merchant,
            # executed_keys is left empty on purpose: PolicyMachine merges the
            # episode's own executed keys into this snapshot, and an
            # idempotency key is scoped to one entity anyway
            # (vasool/diagnosis/proposal.py), so a store-level set would be a
            # second copy of a record the episode already holds.
            executed_keys=frozenset(),
            spent_today_paise=self._spent.get((self.merchant.merchant_id, _ist_day(now)), 0),
            # attempts_used and episode_contacts are overwritten by
            # PolicyMachine._context from the episode itself. Left at their
            # defaults rather than guessed at here.
            contact_history=self._contact_history(customer.customer_id, now),
            consent=customer.consent,
            dnd_listed=customer.dnd_listed,
            dnd_checked_at=now,
            promise_to_pay=episode.promise_to_pay,
            is_mandate=customer.is_mandate,
            pre_debit_notice_sent_at=self._notices.get(event.entity_id),
            registered_templates=template_ids(),
        )

    # -- what the runner records ------------------------------------------
    def record_execution(self, proposal: Proposal, *, at: datetime) -> None:
        """Fold one executed action back into the world.

        Called synchronously, from inside the executor seam, so that a second
        proposal gated later in the same tick sees the first one's effect.
        Deferring this to the end of a tick would let two contacts to the same
        customer both pass FrequencyCapGuard on a snapshot neither of them
        appeared in.
        """
        if proposal.is_contact:
            self._contacts.setdefault(proposal.customer_id, []).append(at)
        if proposal.role.value == "PRE_DEBIT_NOTICE":
            self._notices[proposal.entity_id] = at
        if proposal.is_retry:
            key = (proposal.merchant_id, _ist_day(at))
            self._spent[key] = self._spent.get(key, 0) + proposal.amount_paise

    def _contact_history(self, customer_id: str, now: datetime) -> tuple[datetime, ...]:
        """Contacts inside FrequencyCapGuard's window, ascending.

        Filtered to the window here because that is what PolicyFacts documents
        the field to be. The guard filters again — it has to, since it reads
        `effective_at` rather than `now` — so this is about honouring the
        field's contract, not about saving the guard the work.
        """
        opens = now - FREQUENCY_CAP_WINDOW
        return tuple(sorted(t for t in self._contacts.get(customer_id, ()) if t >= opens))

    def episode_for(self, entity_id: str) -> PlannedEpisode:
        """The plan behind a live episode. The runner needs it to build the
        follow-up `payment.failed` a failed retry produces — same payment,
        same reason, a new webhook."""
        return self._by_entity_id[entity_id]

    def contacts_to(self, customer_id: str) -> tuple[datetime, ...]:
        """Every contact ever sent to this customer. For the ledger scan §2a
        needs, which is about the whole run rather than a rolling window."""
        return tuple(self._contacts.get(customer_id, ()))
