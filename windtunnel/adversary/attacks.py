"""Twenty-two attacks.

**Where they come from.** The first nine are the repository's own recorded
weaknesses — docs/taxonomy.md §9's limits, docs/VERIFIED.md's findings, and
the check-then-act dedupe race already on the postmortem list. They are first
on purpose: they are the attacks with a known answer, and until the harness
scores them the way the documentation already says they should be scored,
nothing the rest of the list reports means anything. An adversary that only
tested things nobody worried about would be measuring the wrong surface.

The remainder are the design spec §9 list and four seams found by reading
`vasool/policy/`. A15, A16, A18 and A19 now survive: later failure evidence
supersedes stale queued work, retry quiet hours are enforced at final gating,
and a promise never delays a human risk handoff.

**The ids are the reviewed ids, with gaps.** A17, A21 and A25 were registered
and then cut before implementation — a prompt-injection probe that could not
fail (the LLM is on no path that writes a ledger, and
tests/test_shadow_boundary.py already proves it by import graph), a DLT
template check, and a forged-signature check. The gaps are left rather than
closed up so that this list and the list that was reviewed are the same list.

**No attack scores itself.** There is no `assert` in this file and nothing
here names the scoring function. Each attack declares `evidence` — a tuple of
requirements the criterion evaluates *in addition to* its three universal
clauses, never instead of them — and `run` returns None. What survival means
was registered in `criterion.py` before any of this was written.

**No attack authors a payload.** Every failure comes off disk through
`windtunnel/payloads.py`, which copies a real envelope and stamps only
identity. CLAUDE.md's rule against inventing an `error_reason` holds at full
force here; adversarial framing is not an exception to it.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from vasool.ledger.receipts import Outcome
from vasool.policy.guards.pre_debit_notice import PRE_DEBIT_NOTICE_LEAD
from windtunnel.adversary.arena import Arena
from windtunnel.adversary.criterion import (
    Attack,
    ContactsPerHumanWithin,
    ExecutedAtMost,
    ExecutedCount,
    Expectation,
    NoContactOutsideCustomerWindow,
    NoContactToDndListed,
    NoDebitBeforeNoticeMatures,
    NoExecutionOnEntityAfter,
    NoRetryExecutedBetweenIST,
    ReceiptCount,
    ReceiptNoLaterThan,
    ReceiptWithOutcome,
)

FAILS = Expectation.FAILS
SURVIVES = Expectation.SURVIVES

NEW_YORK = timezone(timedelta(hours=-4))
"""A customer four and a half hours' offset behind the merchant. Any zone far
enough west of IST would do; this one makes A08's arithmetic legible."""


# ---------------------------------------------------------------------------
# Group A — the attacks with a known answer
# ---------------------------------------------------------------------------
def a01_out_of_band_mid_ladder(arena: Arena) -> None:
    """The customer pays through a channel the agent cannot see, mid-ladder.

    docs/taxonomy.md §9.10 states the outcome in advance: §7's "hard stop on
    out-of-band success" can never fire for a genuinely out-of-band payment,
    because the `payment.captured` it produces carries no `vasool_entity_id`
    and appears in no `RetryIndex`, so the receiver correctly declines to
    attribute it and the episode stays open. The consequence is not a missed
    recovery — it is the agent going on chasing money the merchant already
    has, which is the more expensive direction to be wrong in.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "gateway_technical_error", entity_id="pay_a01")
    arena.advance_by(timedelta(minutes=6))
    arena.fail_last_retry("pay_a01")
    arena.mark("money_arrived")
    arena.pay_out_of_band("pay_a01")
    arena.advance_by(timedelta(hours=6))


def a02_link_paid_closes_the_episode(arena: Arena) -> None:
    """The control for A01: the settlement path that *does* have a join key.

    `_link` tags every payment link it creates with `notes.vasool_entity_id`,
    which is merchant-supplied metadata rather than something Razorpay
    authors, so the `payment_link.paid` webhook carries the episode's own id
    back. Without this attack, A01's failure would read as a defect in the
    state machine; with it, A01 is a statement about attribution.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "card_expired", entity_id="pay_a02")
    arena.advance_by(timedelta(minutes=5))
    arena.mark("paid")
    arena.pay_link("pay_a02")
    arena.advance_by(timedelta(hours=2))


def a03_escalation_without_end(arena: Arena) -> None:
    """Failures that keep arriving after the retry budget is spent.

    docs/taxonomy.md §9.11 records that `EXHAUSTED` is unreachable through the
    rules classifier, because every §4 row escalates rather than ending in
    silence. So what stops an unbounded escalation is not the budget: it is
    that a post-retry action keeps its attempt number (a link is not a
    re-presentation and spends no budget), so every further failure mints the
    same proposal with the same idempotency key.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=8))
    arena.fail(alice, "gateway_technical_error", entity_id="pay_a03")
    arena.advance_by(timedelta(minutes=10))
    arena.fail_last_retry("pay_a03")
    arena.advance_by(timedelta(minutes=40))
    arena.fail_last_retry("pay_a03")
    arena.advance_by(timedelta(hours=5))
    arena.fail_last_retry("pay_a03")
    arena.advance_by(timedelta(hours=1))
    for _ in range(4):
        arena.fail(alice, "gateway_technical_error", entity_id="pay_a03")
        arena.advance_by(timedelta(hours=1))


def a04_duplicate_delivery(arena: Arena) -> None:
    """The same webhook twice, identical `x-razorpay-event-id`.

    docs/VERIFIED.md: every webhook observed live was delivered twice, from
    two Razorpay IPs inside the same millisecond, and the attribution between
    platform redelivery and duplicate registration was never resolved. Both
    the failure and the settlement are duplicated here, because the settlement
    is where a second delivery would claim a second closure receipt — and a
    closure receipt is keyed on (entity, to_state), so two of them would break
    §2a's "every receipt id unique across the run".
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "card_expired", entity_id="pay_a04", event_id="evt_a04_failed")
    arena.fail(alice, "card_expired", entity_id="pay_a04", event_id="evt_a04_failed")
    arena.advance_by(timedelta(minutes=5))
    arena.pay_link("pay_a04", event_id="evt_a04_paid")
    arena.pay_link("pay_a04", event_id="evt_a04_paid")
    arena.advance_by(timedelta(hours=1))


def a05_fresh_event_id_same_payment(arena: Arena) -> None:
    """The harder half of the pair: the same payment under a new event id.

    Design spec §9's A02, and the reason `Proposal.idempotency_key` is keyed
    on the payment rather than on the spec's `(event_id, intervention)` — the
    event plane has nothing to match on here, so the refusal has to come from
    the policy plane instead.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "card_expired", entity_id="pay_a05")
    arena.fail(alice, "card_expired", entity_id="pay_a05")
    arena.advance_by(timedelta(minutes=5))


def a06_check_then_act_race(arena: Arena) -> None:
    """The dedupe race, won by the adversary.

    `EventStore.has_event` is poisoned to answer "never seen it", always —
    which is exactly what a check-then-act dedupe would observe when two
    deliveries arrive inside the same millisecond, as docs/VERIFIED.md records
    they do. If the receiver's dedupe is the atomic INSERT rather than a read
    followed by a write, poisoning the read changes nothing.

    The signature that distinguishes the two is in the ledger. Under
    check-then-act both deliveries reach `observe()` and the second proposal
    is refused by `IdempotencyGuard`, leaving a BLOCKED receipt. Under an
    atomic insert the duplicate never reaches the policy plane at all, so
    there is nothing to block — which is why this attack requires *zero*
    BLOCKED receipts rather than one.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.poison_dedupe_oracle()
    arena.fail(alice, "card_expired", entity_id="pay_a06", event_id="evt_a06_race")
    arena.fail(alice, "card_expired", entity_id="pay_a06", event_id="evt_a06_race")
    arena.advance_by(timedelta(minutes=5))


def a07_identity_split(arena: Arena) -> None:
    """One human, two email addresses, four messages in ninety minutes.

    `vasool/events/schemas.py::derive_customer_id` records this as a KNOWN
    LIMITATION in its own docstring: the id is an HMAC over contact+email, so
    the same person reaching us with a second email is a second customer to
    every guard in the chain, and `FrequencyCapGuard`'s per-customer cap is
    counted against the wrong unit. Design spec §9's A13.
    """
    one = arena.person("rahul", email="rahul@example.invalid")
    two = arena.person("rahul", email="r.kumar@example.invalid", contact=one.contact)
    arena.advance_to(arena.ist(hour=10))
    for index, who in enumerate((one, one, two, two)):
        arena.fail(who, "card_expired", entity_id=f"pay_a07_{index}")
        arena.advance_by(timedelta(minutes=30))


def a08_customer_timezone(arena: Arena) -> None:
    """A message inside the window in IST, and the middle of the night where
    the customer actually lives.

    docs/taxonomy.md §9.3 records this as a known open failure and design spec
    §9 registers it as A05. `ContactWindowGuard` evaluates 08:00-19:00 in the
    merchant's timezone and carries a VERIFY note saying exactly that: "a
    customer in another timezone is protected by our clock rather than by
    theirs." The overnight failure defers to the opening of the IST window,
    which is where the customer's own night is.
    """
    nyc = arena.person("nyc_customer", zone=NEW_YORK)
    arena.advance_to(arena.ist(hour=3))
    arena.fail(nyc, "payment_cancelled", entity_id="pay_a08")
    arena.advance_to(arena.ist(hour=12))


def a09_dnd_never_fires(arena: Arena) -> None:
    """A message to a customer on the DND registry.

    `DNDGuard` has jurisdiction only over PROMOTIONAL messages, and every
    proposal this system builds is TRANSACTIONAL. `MessageCategory`'s own
    VERIFY note says the categorisation of a payment-recovery message under
    TCCCPR is genuinely unsettled and that "if they do [categorise otherwise],
    DNDGuard becomes load-bearing overnight". Today it is one of the thirteen
    that never fires — which this attack makes visible rather than argues
    about.
    """
    bob = arena.person("dnd_bob", dnd_listed=True)
    arena.advance_to(arena.ist(hour=10))
    arena.fail(bob, "card_expired", entity_id="pay_a09")
    arena.advance_by(timedelta(minutes=5))


# ---------------------------------------------------------------------------
# Group B — the spec's list, and the seams under the state machine
# ---------------------------------------------------------------------------
def a10_window_boundary(arena: Arena) -> None:
    """Design spec §9's A04: queued at 17:05, due at 19:05.

    The attack the architecture was built around — gating happens immediately
    before execution rather than at propose time, and `ContactWindowGuard`
    reads `effective_at` rather than `now`. The spec's own §6.3 property test
    asserts against `ctx.now` and so encodes the bug it is meant to catch.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=17, minute=5))
    arena.fail(alice, "payment_cancelled", entity_id="pay_a10")
    arena.advance_to(arena.ist(day=2, hour=12))


def a11_frequency_cap_across_episodes(arena: Arena) -> None:
    """The control for A07: four episodes, one identity, four messages.

    Everything about this is A07 except that the world's human and the agent's
    customer are the same object. If the cap holds here and fails there, the
    defect is identity resolution rather than the cap.
    """
    bob = arena.person("bob")
    arena.advance_to(arena.ist(hour=10))
    for index in range(4):
        arena.fail(bob, "card_expired", entity_id=f"pay_a11_{index}")
        arena.advance_by(timedelta(minutes=30))
    arena.advance_to(arena.ist(day=9, hour=12))


def a12_afa_boundary(arena: Arena) -> None:
    """Design spec §9's A11: ₹15,000 and ₹15,001, both directions.

    A threshold is only tested by a pair. One paisa over the RBI e-mandate
    limit must reach a human; one paisa under must not be escalated *by that
    guard* — which is a different claim from "it executes", because a mandate
    debit is separately held by the pre-debit notice rule (see A23).
    """
    mandy = arena.person("mandy", is_mandate=True)
    arena.advance_to(arena.ist(hour=10))
    arena.fail(mandy, "gateway_technical_error", amount_paise=1_500_000, entity_id="pay_a12_at")
    arena.fail(mandy, "gateway_technical_error", amount_paise=1_500_001, entity_id="pay_a12_over")
    arena.advance_to(arena.ist(day=9, hour=12))


def a13_consent_withdrawn_mid_sequence(arena: Arena) -> None:
    """Design spec §9's A12: DPDP, and it is two things rather than one.

    Withdrawal has to purge the queue *and* close the episodes — an episode
    whose retry has already fired sits in AWAITING with nothing queued, so a
    queue-only purge would leave it open for the next failure webhook to
    restart the chase. The fresh failure after the withdrawal is the second
    half: `ConsentGuard` has to refuse it from the consent record, with no
    help from the state machine.
    """
    carol = arena.person("carol")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(carol, "insufficient_fund", entity_id="pay_a13_open")
    arena.mark("withdrawn")
    arena.withdraw_consent(carol)
    arena.fail(carol, "card_expired", entity_id="pay_a13_after")
    arena.advance_by(timedelta(hours=2))


def a14_future_dated_event_twice(arena: Arena) -> None:
    """Design spec §9's A18, delivered the way Razorpay actually delivers.

    An event two days ahead of our clock is either skew worth investigating or
    a corrupted payload, and scheduling from it would mean acting on something
    that has not happened. Delivered twice under different event ids because
    that is the case `PolicyMachine.observe` guards in its own comment: a
    closure receipt is keyed on (entity, to_state), so a second skew
    escalation for one payment would claim an id the first already has.
    """
    dave = arena.person("dave")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(
        dave,
        "gateway_technical_error",
        entity_id="pay_a14",
        occurred_at=arena.ist(day=3, hour=10),
    )
    arena.fail(
        dave,
        "gateway_technical_error",
        entity_id="pay_a14",
        occurred_at=arena.ist(day=3, hour=10),
    )
    arena.advance_by(timedelta(hours=2))


def a15_risk_decline_mid_ladder(arena: Arena) -> None:
    """A risk decline lands at the exact instant a queued retry falls due.

    The proposal was built from an earlier, benign diagnosis, so it carries
    `failure_class=TRANSIENT` — and `RiskBlockGuard.applies_to` keys on the
    proposal's own class, not on the episode's. Whether the escalation closes
    the episode before the retry gates is a race decided by arrival order:
    the same decline a minute earlier stops everything.

    Worth stating plainly, because it is the sharper half of the finding:
    §2a's `scan_risk_block` is keyed on the proposal's class too, so the
    ledger scan the project relies on cannot see this. Only evidence keyed on
    the *episode* — which the world knows was risk-declined — catches it.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "gateway_technical_error", entity_id="pay_a15")
    arena.jump_to(arena.ist(hour=10, minute=5))
    arena.fail(alice, "payment_risk_check_failed", entity_id="pay_a15")
    arena.advance_by(timedelta(minutes=1))


def a16_card_expires_mid_ladder(arena: Arena) -> None:
    """Design spec §9's A06: the instrument dies between two attempts.

    A new failure with a new reason reclassifies the episode and schedules
    what the new row says — but the proposal the *old* row scheduled is still
    on the queue, and nothing re-reads its classification. `PolicyMachine`'s
    docstring argues correctly that re-running `classify()` on wake is vacuous
    because it is a pure function of (event, attempt); what it does not cover
    is a queued proposal outliving the diagnosis that produced it.

    So the re-auth link goes out, and half an hour later the agent
    re-presents a card it has already been told is expired — taxonomy §5's
    flagship zero, spent anyway.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "gateway_technical_error", entity_id="pay_a16")
    arena.advance_by(timedelta(minutes=6))
    arena.fail_last_retry("pay_a16")
    arena.mark("card_reported_expired")
    arena.fail(alice, "card_expired", entity_id="pay_a16")
    arena.advance_by(timedelta(hours=2))


def a18_promise_releases_at_midnight(arena: Arena) -> None:
    """A promise to pay defers a re-presentation to exactly 00:00 IST.

    `PromiseToPayGuard` defers to midnight of the day after the promised date,
    and it has no `applies_to`, so it governs silent retries as well as
    messages. taxonomy §6 excludes 00:00-06:00 IST for re-presentations too —
    but `vasool/diagnosis/rules.py` applies that hold at *classify* time, and
    its own docstring notes that "a proposal that then waits in a queue is
    never re-checked, so only the guard is load-bearing". There is no guard
    for the retry half, so a deferral walks straight back into the quiet
    period the classifier moved it out of.

    Nothing is sent to anyone, so this costs efficacy rather than dignity —
    but §6 states the rule for both halves and this is the half that is not
    enforced anywhere.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "gateway_technical_error", entity_id="pay_a18")
    arena.promise("pay_a18", arena.ist_date(day=2))
    arena.advance_to(arena.ist(day=4, hour=12))


def a19_risk_handoff_deferred_by_a_promise(arena: Arena) -> None:
    """A promise to pay delays the one handoff taxonomy §7 calls immediate.

    `PromiseToPayGuard` applies to every proposal including `HUMAN_QUEUE`, and
    DEFER outranks the ALLOW `RiskBlockGuard` returns for a handoff, so a
    risk-declined payment from a customer who has promised to pay waits for
    the promise to lapse before an operator hears about it. taxonomy §7 lists
    "hard stop on RISK_BLOCK" as immediate, and §2 is explicit that on this
    path doing nothing is the product decision — but doing nothing *later* is
    not the same decision.

    This is the same defect `vasool/diagnosis/rules.py` already fixed one
    plane up, where the quiet-hours hold was being applied to `HUMAN_QUEUE`
    and a 02:00 risk decline sat until 06:00. Nothing is sent to anyone on
    this path, so no rule about disturbing a person applies to holding it.
    """
    bob = arena.person("bob")
    arena.advance_to(arena.ist(hour=10))
    arena.mark("declined")
    arena.fail(bob, "payment_risk_check_failed", entity_id="pay_a19")
    arena.promise("pay_a19", arena.ist_date(day=2))
    arena.advance_to(arena.ist(day=4, hour=12))


def a20_retry_cap_race(arena: Arena) -> None:
    """Two failure webhooks racing for the last budgeted attempt.

    Registered as "drive the attempts past the cap", and the interesting part
    is how hard that turns out to be. `classify` computes the attempt as
    `attempts_used + 1`, and no §4 row has a retry budget above three — which
    is `ONETIME_ATTEMPT_CAP` — so a proposal past the budget is always the
    row's escalation and never another re-presentation. `RetryCapGuard` is
    therefore unreachable through a clean ladder.

    It is reachable exactly here: two webhooks for the same attempt in flight
    at once, minted while `attempts_used` still permitted a retry and gated
    after the first of them had spent the last slot. That is the concurrent
    case the guard is actually a backstop for, and it is not a hypothetical —
    duplicate delivery is normal operation on this account.
    """
    dave = arena.person("dave")
    arena.advance_to(arena.ist(hour=8))
    arena.fail(dave, "gateway_technical_error", entity_id="pay_a20")
    arena.advance_by(timedelta(minutes=10))
    arena.fail_last_retry("pay_a20")
    arena.advance_by(timedelta(minutes=40))
    arena.fail_last_retry("pay_a20")
    arena.fail_last_retry("pay_a20")
    arena.advance_by(timedelta(hours=6))


def a22_blast_radius(arena: Arena) -> None:
    """A signed webhook carrying an amount far past the merchant's ceilings.

    Two self-imposed limits, and they must resolve in the right order. A
    single retry larger than the whole daily cap has to BLOCK rather than
    defer — no reset makes it fit, so deferring it would defer it forever —
    while an amount over the unattended-action threshold escalates to a human.
    BLOCK outranks ESCALATE, so the receipt for the larger one names both
    guards and refuses outright.
    """
    whale = arena.person("whale")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(whale, "gateway_technical_error", amount_paise=60_000_000, entity_id="pay_a22_huge")
    arena.fail(whale, "gateway_technical_error", amount_paise=6_000_000, entity_id="pay_a22_big")
    arena.advance_by(timedelta(minutes=20))


def a23_pre_debit_notice(arena: Arena) -> None:
    """A mandate debit with no notice served, at 23:00 IST.

    RBI's e-mandate framework requires the customer to be notified before a
    recurring debit, and `PreDebitNoticeGuard` holds the debit until one has
    been. The safety claim is that the account is never touched inside the
    notice period; the attack asks whether an unnotified debit can reach the
    instrument by waiting.

    23:00 because the notice is itself a contact, and the hour is what makes
    that load-bearing: the notice cannot go out when it is owed, so it defers
    to the contact window and the debit waits on a notice that is itself
    waiting. Nothing about the notice short-circuits the chain — the whole
    argument in `vasool/policy/guards/pre_debit_notice.py` for describing it
    rather than performing it.

    Until docs/taxonomy.md §9.13 was fixed this attack survived for the wrong
    reason. Obligations were read only in `PolicyMachine._execute`, and a guard
    can only attach one to a `DEFER`, so the notice was never created, the
    debit deferred five times and landed in BLOCKED, and no mandate debit
    executed anywhere in the system. Safe, and inert. The evidence below is
    what the attack was always meant to assert, split so the two halves cannot
    be confused again: the debit waits for a matured notice (safety), *and*
    both of them actually happen (liveness).
    """
    mandy = arena.person("mandy", is_mandate=True)
    arena.advance_to(arena.ist(hour=23))
    arena.fail(mandy, "gateway_technical_error", entity_id="pay_a23")
    arena.advance_to(arena.ist(day=9, hour=12))


def a24_late_failure_after_settlement(arena: Arena) -> None:
    """Design spec §9's A03: the outcome arrives out of order.

    taxonomy §5 on `payment_timed_out` names the situation exactly — "the
    authorisation may have succeeded and the response been lost" — so a
    payment that captures and then reports failing is the ambiguity that row
    exists for. The capture settles the episode through the executor's own
    `RetryIndex`; the late failure names the same payment and must be
    absorbed by the terminal state rather than reopening the chase.
    """
    alice = arena.person("alice")
    arena.advance_to(arena.ist(hour=10))
    arena.fail(alice, "payment_timed_out", entity_id="pay_a24")
    arena.advance_by(timedelta(minutes=11))
    arena.capture_last_retry("pay_a24")
    arena.mark("settled")
    arena.fail_last_retry("pay_a24")
    arena.advance_by(timedelta(hours=2))


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------
ATTACKS: tuple[Attack, ...] = (
    Attack(
        id="A01",
        title="out-of-band payment mid-ladder",
        targets="double collection: §7's hard stop on out-of-band success",
        source="docs/taxonomy.md §9.9 and §9.10; design spec §9 A07",
        expectation=FAILS,
        evidence=(NoExecutionOnEntityAfter("pay_a01", mark="money_arrived"),),
        run=a01_out_of_band_mid_ladder,
    ),
    Attack(
        id="A02",
        title="a link we sent is paid",
        targets="the one settlement path with a join key that was not guessed",
        source="docs/VERIFIED.md's settlement DECISION; the control for A01",
        expectation=SURVIVES,
        evidence=(
            ReceiptCount("pay_a02", Outcome.RECOVERED, 1),
            NoExecutionOnEntityAfter("pay_a02", mark="paid"),
        ),
        run=a02_link_paid_closes_the_episode,
    ),
    Attack(
        id="A03",
        title="escalation without end",
        targets="unbounded escalation once the retry budget is spent",
        source="docs/taxonomy.md §9.11 (EXHAUSTED is unreachable)",
        expectation=SURVIVES,
        evidence=(
            ExecutedCount("pay_a03", 3, is_retry=True),
            ExecutedCount("pay_a03", 1, is_contact=True),
            ReceiptWithOutcome("pay_a03", Outcome.BLOCKED, guard="IdempotencyGuard"),
        ),
        run=a03_escalation_without_end,
    ),
    Attack(
        id="A04",
        title="every webhook delivered twice",
        targets="event-plane idempotency, and closure-receipt uniqueness",
        source="docs/VERIFIED.md: duplicate delivery is normal operation",
        expectation=SURVIVES,
        evidence=(
            ExecutedCount("pay_a04", 1, is_contact=True),
            ReceiptCount("pay_a04", Outcome.RECOVERED, 1),
        ),
        run=a04_duplicate_delivery,
    ),
    Attack(
        id="A05",
        title="same payment, fresh event id",
        targets="semantic idempotency, where the event plane has nothing to match on",
        source="design spec §9 A02",
        expectation=SURVIVES,
        evidence=(
            ExecutedCount("pay_a05", 1, is_contact=True),
            ReceiptWithOutcome("pay_a05", Outcome.BLOCKED, guard="IdempotencyGuard"),
        ),
        run=a05_fresh_event_id_same_payment,
    ),
    Attack(
        id="A06",
        title="the check-then-act dedupe window, won",
        targets="dedupe as an atomic insert rather than a read then a write",
        source="docs/VERIFIED.md (two IPs, same millisecond); the postmortem list",
        expectation=SURVIVES,
        evidence=(
            ExecutedCount("pay_a06", 1, is_contact=True),
            ReceiptCount("pay_a06", Outcome.BLOCKED, 0),
        ),
        run=a06_check_then_act_race,
    ),
    Attack(
        id="A07",
        title="one human, two customer ids",
        targets="the per-customer frequency cap, counted against the wrong unit",
        source="vasool/events/schemas.py::derive_customer_id KNOWN LIMITATION; spec A13",
        expectation=FAILS,
        evidence=(ContactsPerHumanWithin(cap=3, window=timedelta(days=7)),),
        run=a07_identity_split,
    ),
    Attack(
        id="A08",
        title="the contact window in the wrong timezone",
        targets="RBI FPC 08:00-19:00, evaluated in the merchant's zone",
        source="docs/taxonomy.md §9.3; ContactWindowGuard's own VERIFY; spec A05",
        expectation=FAILS,
        evidence=(NoContactOutsideCustomerWindow(),),
        run=a08_customer_timezone,
    ),
    Attack(
        id="A09",
        title="a message to a DND-listed customer",
        targets="the TRAI scrub, which no proposal this system builds can reach",
        source="vasool/diagnosis/proposal.py::MessageCategory VERIFY",
        expectation=FAILS,
        evidence=(NoContactToDndListed(),),
        run=a09_dnd_never_fires,
    ),
    Attack(
        id="A10",
        title="queued at 17:05, due at 19:05",
        targets="the compliance check happening at execute time, on effective_at",
        source="design spec §9 A04",
        expectation=SURVIVES,
        evidence=(ExecutedCount("pay_a10", 1, is_contact=True),),
        run=a10_window_boundary,
    ),
    Attack(
        id="A11",
        title="four episodes, one identity",
        targets="the frequency cap when identity resolution is not the problem",
        source="the control for A07",
        expectation=SURVIVES,
        evidence=(
            ContactsPerHumanWithin(cap=3, window=timedelta(days=7)),
            ExecutedCount("pay_a11_3", 1, is_contact=True),
        ),
        run=a11_frequency_cap_across_episodes,
    ),
    Attack(
        id="A12",
        title="a mandate debit at the AFA threshold, and one paisa over",
        targets="RBI's ₹15,000 e-mandate limit, both directions",
        source="design spec §9 A11",
        expectation=SURVIVES,
        evidence=(
            ReceiptWithOutcome("pay_a12_over", Outcome.ESCALATED, guard="AFAThresholdGuard"),
            ExecutedCount("pay_a12_over", 0),
            ReceiptCount("pay_a12_at", Outcome.ESCALATED, 0),
        ),
        run=a12_afa_boundary,
    ),
    Attack(
        id="A13",
        title="consent withdrawn with work in flight",
        targets="DPDP: purge the queue, close the episodes, refuse what arrives next",
        source="design spec §9 A12; taxonomy §7",
        expectation=SURVIVES,
        evidence=(
            ReceiptWithOutcome("pay_a13_open", Outcome.CONSENT_WITHDRAWN),
            NoExecutionOnEntityAfter("pay_a13_open", mark="withdrawn"),
            ReceiptWithOutcome("pay_a13_after", Outcome.BLOCKED, guard="ConsentGuard"),
            ExecutedCount("pay_a13_after", 0),
        ),
        run=a13_consent_withdrawn_mid_sequence,
    ),
    Attack(
        id="A14",
        title="a future-dated event, delivered twice",
        targets="clock-skew sanity bounds, and the closure receipt they write",
        source="design spec §9 A18; PolicyMachine.observe's own terminal check",
        expectation=SURVIVES,
        evidence=(
            ReceiptCount("pay_a14", Outcome.CLOCK_SKEW, 1),
            ExecutedCount("pay_a14", 0),
        ),
        run=a14_future_dated_event_twice,
    ),
    Attack(
        id="A15",
        title="a risk decline in the same instant as a due retry",
        targets="a queued proposal outliving the diagnosis that built it",
        source="taxonomy §2's hardest rule; found reading PolicyMachine.tick",
        expectation=SURVIVES,
        evidence=(
            ExecutedCount("pay_a15", 0),
            ReceiptWithOutcome("pay_a15", Outcome.ESCALATED),
        ),
        run=a15_risk_decline_mid_ladder,
    ),
    Attack(
        id="A16",
        title="the card expires between attempt 2 and 3",
        targets="stale classification: the queued retry is never reclassified",
        source="design spec §9 A06; taxonomy §5's card_expired",
        expectation=SURVIVES,
        evidence=(
            NoExecutionOnEntityAfter("pay_a16", mark="card_reported_expired", is_retry=True),
            ExecutedCount("pay_a16", 1, is_retry=True),
        ),
        run=a16_card_expires_mid_ladder,
    ),
    Attack(
        id="A18",
        title="a promise to pay releases a retry at midnight",
        targets="taxonomy §6's quiet period on the retry half",
        source="vasool/diagnosis/rules.py's own note that the hold is classify-time only",
        expectation=SURVIVES,
        evidence=(NoRetryExecutedBetweenIST(0, 6),),
        run=a18_promise_releases_at_midnight,
    ),
    Attack(
        id="A19",
        title="a promise to pay defers the human handoff",
        targets="taxonomy §7's immediate hard stop on RISK_BLOCK",
        source="PromiseToPayGuard has no applies_to; the same defect rules.py already fixed",
        expectation=SURVIVES,
        evidence=(
            ReceiptNoLaterThan(
                "pay_a19", Outcome.ESCALATED, mark="declined", within=timedelta(hours=1)
            ),
            ExecutedCount("pay_a19", 0),
        ),
        run=a19_risk_handoff_deferred_by_a_promise,
    ),
    Attack(
        id="A20",
        title="two webhooks racing for the last attempt",
        targets="the platform attempt ceiling under concurrent delivery",
        source="taxonomy §7's 4-retry halt; docs/VERIFIED.md's duplicate delivery",
        expectation=SURVIVES,
        evidence=(
            ExecutedAtMost("pay_a20", 3, is_retry=True),
            ReceiptWithOutcome("pay_a20", Outcome.BLOCKED, guard="RetryCapGuard"),
        ),
        run=a20_retry_cap_race,
    ),
    Attack(
        id="A22",
        title="an amount past both merchant ceilings",
        targets="blast radius: the daily spend cap and the unattended threshold",
        source="design spec §5; SpendCapGuard's own argument against deferring forever",
        expectation=SURVIVES,
        evidence=(
            ReceiptWithOutcome("pay_a22_huge", Outcome.BLOCKED, guard="SpendCapGuard"),
            ExecutedCount("pay_a22_huge", 0),
            ReceiptWithOutcome("pay_a22_big", Outcome.ESCALATED, guard="HumanApprovalGuard"),
            ExecutedCount("pay_a22_big", 0),
        ),
        run=a22_blast_radius,
    ),
    Attack(
        id="A23",
        title="a mandate debit with no notice served",
        targets="RBI e-mandate: the account is not touched inside the notice period",
        source="design spec §10's compliance row; docs/taxonomy.md §9.13",
        expectation=SURVIVES,
        evidence=(
            NoDebitBeforeNoticeMatures("pay_a23", PRE_DEBIT_NOTICE_LEAD),
            ExecutedCount("pay_a23", 1, is_contact=True),
            ExecutedCount("pay_a23", 1, is_retry=True),
        ),
        run=a23_pre_debit_notice,
    ),
    Attack(
        id="A24",
        title="the failure arrives after the capture",
        targets="event ordering: a terminal episode absorbs what comes late",
        source="design spec §9 A03; taxonomy §5 on payment_timed_out",
        expectation=SURVIVES,
        evidence=(
            ReceiptCount("pay_a24", Outcome.RECOVERED, 1),
            NoExecutionOnEntityAfter("pay_a24", mark="settled"),
        ),
        run=a24_late_failure_after_settlement,
    ),
)
