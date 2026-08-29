"""Everything a guard needs to know about the world, as one frozen value.

Nine of the thirteen guards need history: has this already run, how many
attempts are spent, when did we last message this customer, is consent live.
The design spec requires guards to be pure anyway, and both things are true at
once only if the impurity is moved somewhere else.

So it is moved here. A FactStore reads the world once, before the chain runs,
and hands the guards a PolicyFacts snapshot they can only read. Three things
follow:

  - **Guards are property-testable without fakes.** Hypothesis generates a
    PolicyFacts directly; there is no database to stand up and no store to mock.
  - **The chain becomes a pure function of (facts, proposal, effective_at).**
    Stage 5 can record the snapshot's digest in the receipt and replay can
    re-run the whole compliance decision from the ledger without touching a
    store at all — which is what makes "same seed, byte-identical ledger" a
    property of the policy plane rather than a hope.
  - **Fact-loading bugs cannot silently disable a guard.** A fact that is
    missing rather than absent blocks; see `requires` on the Guard base class.

That last one needs the distinction spelled out, because `None` means two
different things in this module and conflating them is how a compliance guard
quietly stops working:

  - `consent is None` — we have no consent record. **Unknown.** Block.
  - `dnd_listed is None` — the registry was never scrubbed. **Unknown.** Block.
  - `promise_to_pay is None` — the customer made no promise. **Known, absent.**
    Nothing to honour, so allow.

A guard declares the first kind in `requires`. It must not declare the second,
or it would refuse every action for want of a promise nobody made.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from vasool.diagnosis.proposal import Proposal
from vasool.events.schemas import FailureEvent

CONSENT_PURPOSE_RECOVERY = "payment_recovery"
"""The DPDP purpose an outbound recovery message is processed under. Consent
granted for something else does not cover it — purpose limitation is the point
of the regime, and a consent record listing only "marketing" must not authorise
a dunning message."""


DEFAULT_DAILY_RETRY_CAP_PAISE = 50_000_000
"""₹5,00,000 — design spec §5's default merchant blast-radius limit. Self-imposed,
not regulation. Configurable per merchant."""

DEFAULT_HUMAN_APPROVAL_PAISE = 5_000_000
"""₹50,000.

# VERIFY: this number is ours and it is arbitrary. The spec says
# "> threshold -> human queue" without naming one. It wants calibrating against
# a real merchant's distribution of payment sizes — set too low, every recovery
# queues and the agent does nothing; too high and it never catches anything.
"""


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """DPDP consent as we hold it. Pseudonymised subject (see
    schemas.derive_customer_id)."""

    granted_at: datetime
    purposes: frozenset[str]
    withdrawn_at: datetime | None = None

    def covers(self, purpose: str, at: datetime) -> bool:
        if self.withdrawn_at is not None and at >= self.withdrawn_at:
            return False
        return at >= self.granted_at and purpose in self.purposes

    def is_withdrawn(self, at: datetime) -> bool:
        return self.withdrawn_at is not None and at >= self.withdrawn_at


@dataclass(frozen=True, slots=True)
class MerchantPolicy:
    """Per-merchant configuration. Not regulation — every number here is
    self-imposed and belongs to the merchant."""

    merchant_id: str
    daily_retry_cap_paise: int = DEFAULT_DAILY_RETRY_CAP_PAISE
    human_approval_threshold_paise: int = DEFAULT_HUMAN_APPROVAL_PAISE
    kill_switch: bool = False
    """Honoured mid-flight by the state machine, not by a guard. It is not one
    of the thirteen: a kill switch is an operability control, and rendering it
    as a compliance verdict would put "merchant switched us off" in a column of
    statute citations."""


@dataclass(frozen=True, slots=True)
class PolicyFacts:
    """One read of the world, frozen. Guards may only read this."""

    merchant: MerchantPolicy

    # -- ledger
    executed_keys: frozenset[str] = frozenset()
    """Idempotency keys already executed. Known-absent when empty."""

    spent_today_paise: int = 0
    """Value already re-presented for this merchant today, IST."""

    # -- episode
    attempts_used: int = 0
    """Instrument re-presentations already spent on this entity. Counted across
    the episode, not per event — Razorpay counts consecutive failures against a
    subscription, and so must we."""

    episode_contacts: int = 0
    """Messages already sent in this recovery episode."""

    contact_history: tuple[datetime, ...] = ()
    """Every contact to this customer inside the frequency window, ascending.
    Known-absent when empty: a customer we have never messaged."""

    # -- customer
    consent: ConsentRecord | None = None
    """None means no record — unknown, therefore blocking."""

    dnd_listed: bool | None = None
    dnd_checked_at: datetime | None = None
    """None means never scrubbed — unknown, therefore blocking. In production
    this is a network call, so it also has an age; DNDGuard blocks on a stale
    one rather than trusting a scrub from last month."""

    promise_to_pay: date | None = None
    """None means no promise exists — known-absent, therefore permissive."""

    # -- mandate
    is_mandate: bool = False
    """Whether this is a recurring debit. Not on FailureEvent: no subscription
    payload has ever been observed on this account (docs/VERIFIED.md), so the
    field would have been invented. It arrives here, where a simulator can set
    it honestly and production reads it from the mandate record."""

    pre_debit_notice_sent_at: datetime | None = None
    """None means not yet sent — known-absent. The guard's job is then to
    require one, not to refuse the debit forever."""

    # -- comms
    registered_templates: frozenset[str] = field(default_factory=frozenset)
    """DLT template ids registered to this merchant."""


@dataclass(frozen=True, slots=True)
class GuardContext:
    """What a guard sees. Two times, deliberately.

    `now` is when we are deciding. `effective_at` is when the action would
    actually reach the world, and it is the one every time-of-day rule must
    read.

    The spec's §6.3 property test asserts against `ctx.now`, which is adversary
    attack A04 written into the test that is supposed to catch it: an SMS gated
    at 18:58 and executed at 19:02 passes a contact-window check against the
    decision time and lands outside the window. Keeping both times on the
    context makes the distinction impossible to overlook, and the state machine
    closes the gap from the other side by gating immediately before execution
    rather than at propose time.
    """

    now: datetime
    effective_at: datetime
    event: FailureEvent
    proposal: Proposal
    facts: PolicyFacts


class FactStore(Protocol):
    """The one impure thing in the policy plane.

    SQLite in production, a dict in the simulator. It is deliberately a single
    method: the snapshot must be one consistent read, not thirteen guards each
    querying at a slightly different moment.
    """

    def snapshot(
        self, *, event: FailureEvent, proposal: Proposal, now: datetime
    ) -> PolicyFacts: ...


# `build_context` lived here and was removed 2026-08-29. It built a
# GuardContext straight from a FactStore snapshot, which looks like the
# sanctioned way to make one and is not: it omits the episode-counter merge
# that `PolicyMachine._context` performs (attempts_used, episode_contacts,
# executed_keys). A caller using it would hand RetryCapGuard and
# FrequencyCapGuard zeroed counters and get a silent under-count on exactly
# the two caps §2a scans. Nothing called it. `PolicyMachine._context` is the
# only way a GuardContext should be built.
