"""`make demo`: one recovery episode, end to end, printed for a screen recording.

Loads a single `payment.failed` payload from data/observed_payloads/ or
data/stubbed_payloads/, drives it through the receiver's own primitives
(signature check, event-store dedupe), then a real PolicyMachine wired to a
real RazorpayExecutor, and narrates every stage on stdout: webhook received ->
signature verified -> deduped -> classified -> proposal(s) -> each guard
verdict with its statute -> decision -> receipt with hash.

**Replay vs. live.** Replay is the default: an in-process fake Razorpay
client, deterministic, no network — `git clone && make demo` has to work
with zero setup, and that bare command is the first thing anyone who clones
this repo will type. `--live` opts in to a real RazorpayClient against
Razorpay's test-mode API (RAZORPAY_KEY_ID/SECRET from .env); its caveat
prints before anything else runs, not after, and it falls back to the same
fake client automatically, with a printed note, if those credentials aren't
configured. `--replay` still works as an explicit no-op — the documented
money-shot command and the golden fixtures in data/golden/ pin it, and
neither had to change for the flip.

Either way the *triggering* event comes from disk, not from an actual live
checkout: nothing in this repo drives Razorpay's mock bank page, and
docs/VERIFIED.md records that every "Error Scenario" test card returns the
identical generic `payment_failed` regardless of which one you pick, so a
live checkout could not select a scenario even if this script drove one. What
`--live` changes is whether actions/executor.py's calls are real: it makes a
REAUTH_LINK genuinely create a Payment Link on the merchant's test-mode
account (VERIFIED.md: Payment Links work pre-activation), and a SILENT_RETRY
genuinely call `payment.createRecurring` against a payment with no real
recurring token behind it, which fails at Razorpay's boundary exactly the way
actions/executor.py already expects a downstream call to fail. Only
`payment_failed` is a failure Razorpay itself has ever produced live
(docs/VERIFIED.md); every other --scenario is played from a hand-built
_SIMULATED payload regardless of --live, in both modes. Live mode therefore
demonstrates the pipeline and the guard chain, not a successful recovery —
see the --help text below, which says this before the run rather than
leaving someone to discover it on camera.

This module never reads the real wall clock — the demo clock is a
VirtualClock seeded from the scenario's own captured date and --time
(vasool/clock.py; tests/test_no_wallclock.py enforces this file too).
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import pathlib
import sys
import textwrap
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from vasool.actions.executor import RazorpayExecutor
from vasool.clock import VirtualClock
from vasool.diagnosis.proposal import Proposal, template_ids
from vasool.diagnosis.rules import IST
from vasool.diagnosis.taxonomy import InterventionType, known_reasons
from vasool.events.receiver import verify_signature
from vasool.events.schemas import FailureEvent, from_webhook
from vasool.events.settlement import (
    amount_paise_from_payment_captured,
    amount_paise_from_payment_link_paid,
    entity_id_from_payment_captured,
    entity_id_from_payment_link_paid,
)
from vasool.events.store import EventStore
from vasool.ledger.receipts import Outcome, Receipt, build_from_transitions
from vasool.ledger.tracing import trace_id_for
from vasool.policy.episode import State
from vasool.policy.facts import (
    CONSENT_PURPOSE_RECOVERY,
    ConsentRecord,
    MerchantPolicy,
    PolicyFacts,
)
from vasool.policy.machine import PolicyMachine
from vasool.policy.transitions import Transition
from vasool.policy.verdict import Decision, Verdict

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED_DIR = REPO_ROOT / "data" / "observed_payloads"
STUBBED_DIR = REPO_ROOT / "data" / "stubbed_payloads"

WIDTH = 78
INDENT = "    "


# ---------------------------------------------------------------------------
# loading a scenario off disk — never a typed-in error string (the project rules)
# ---------------------------------------------------------------------------
def _payload_paths() -> list[tuple[pathlib.Path, bool]]:
    observed = [(p, False) for p in sorted(OBSERVED_DIR.glob("payment_failed__*.json"))]
    stubbed = [(p, True) for p in sorted(STUBBED_DIR.glob("SIMULATED__payment_failed__*.json"))]
    return observed + stubbed


def load_scenario(scenario: str, *, pepper: str) -> tuple[dict, bool, FailureEvent]:
    """The fixture dict, whether it's a _SIMULATED stub, and the FailureEvent
    it decodes to — for the first payload on disk whose error_reason matches.
    """
    for path, simulated in _payload_paths():
        fixture = json.loads(path.read_text())
        event = from_webhook(
            event_id=fixture["headers"]["x-razorpay-event-id"],
            body=fixture["body"],
            pepper=pepper,
        )
        if event.error_reason == scenario:
            return fixture, simulated, event
    raise LookupError(
        f"no payload on disk for scenario {scenario!r} — see docs/taxonomy.md "
        "§4 for the reasons this demo can classify"
    )


# ---------------------------------------------------------------------------
# the demo's world: permissive facts, and a fake Razorpay client for --replay
# ---------------------------------------------------------------------------
class _DemoFacts:
    """Nothing is wrong in this world except what the scenario itself and the
    time of day imply — the same "known-good baseline" tests/policy/
    strategies.py::permissive_facts uses, redefined here rather than imported
    from tests/, which this production entrypoint must not depend on."""

    def snapshot(self, *, event: FailureEvent, proposal: Proposal, now: datetime) -> PolicyFacts:
        return PolicyFacts(
            merchant=MerchantPolicy(merchant_id=event.merchant_id),
            consent=ConsentRecord(
                granted_at=now.replace(year=now.year - 1),
                purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
            ),
            dnd_listed=False,
            dnd_checked_at=now,
            registered_templates=template_ids(),
        )


class _HostileDemoFacts:
    """A world built to say no, not yes.

    The pitch is "thirteen guards stand between the LLM and any money
    movement", but under _DemoFacts only ContactWindowGuard can ever fire —
    every other guard is handed a world constructed so it agrees. This
    profile changes exactly one fact from the permissive baseline: no
    consent record on file at all. `consent=None` is ConsentGuard's declared
    `requires` fact, so the guard base class (vasool/policy/guards/base.py)
    fails closed before ConsentGuard.check() ever runs, exactly as it would
    for a real customer nobody ever obtained consent from. Nothing about the
    guard or the diagnosis plane changes — only this fixture."""

    def snapshot(self, *, event: FailureEvent, proposal: Proposal, now: datetime) -> PolicyFacts:
        return PolicyFacts(
            merchant=MerchantPolicy(merchant_id=event.merchant_id),
            consent=None,
            dnd_listed=False,
            dnd_checked_at=now,
            registered_templates=template_ids(),
        )


class _HostileDLTDemoFacts:
    """A second world built to say no, through a different statute and a
    different mechanism than _HostileDemoFacts (item 5).

    DNDGuard was the other obvious candidate, but it structurally cannot
    fire against anything the rules classifier currently emits: every
    contact this system sends is tagged MessageCategory.TRANSACTIONAL
    (vasool/diagnosis/proposal.py), and DNDGuard.applies_to only ever returns
    True for MessageCategory.PROMOTIONAL — see that guard's own docstring.
    Choosing it would demo a guard that never actually runs, not one that
    blocks. DLTTemplateGuard is the real second guard: it applies to any
    contact-carrying proposal, and blocks in `check()` on a normal set
    lookup rather than failing closed on a missing `requires` fact the way
    ConsentGuard does — a genuinely different mechanism, not just a
    different statute string.

    This profile changes exactly one fact from the permissive baseline: an
    empty registered_templates set, i.e. a merchant that has registered
    nothing on TRAI's DLT platform at all. Nothing about the guard or the
    diagnosis plane changes — only this fixture.
    """

    def snapshot(self, *, event: FailureEvent, proposal: Proposal, now: datetime) -> PolicyFacts:
        return PolicyFacts(
            merchant=MerchantPolicy(merchant_id=event.merchant_id),
            consent=ConsentRecord(
                granted_at=now.replace(year=now.year - 1),
                purposes=frozenset({CONSENT_PURPOSE_RECOVERY}),
            ),
            dnd_listed=False,
            dnd_checked_at=now,
            registered_templates=frozenset(),
        )


class _FakeRazorpayClient:
    """No network. Deterministic responses, for --replay and for the
    zero-setup fallback when live credentials aren't configured."""

    def create_payment_link(self, **kwargs) -> dict:
        return {"id": "plink_demo0000001", "short_url": "https://rzp.io/l/demo0001"}

    def notify_payment_link(self, **kwargs) -> dict:
        return {"success": True}

    def retry_payment(self, **kwargs) -> dict:
        return {"id": "pay_demo_retry001"}


def build_executor(*, live: bool) -> tuple[RazorpayExecutor, str]:
    """The executor, and one line describing which mode it's actually in —
    live can silently degrade to replay, and that degrading has to be on
    screen, not just in a log line nobody's recording.

    Goes through RazorpayExecutor.from_env() rather than constructing a
    RazorpayClient here: tests/test_actions_boundary.py restricts importing
    razorpay_client.py to executor.py itself, so this module has no other
    legal way to reach a live client.
    """
    if live:
        try:
            return (
                RazorpayExecutor.from_env(registered_templates=template_ids()),
                "live — calling Razorpay's real test-mode API for every allowed action",
            )
        except RuntimeError as exc:
            return (
                RazorpayExecutor.build(client=_FakeRazorpayClient(), registered_templates=template_ids()),
                f"live requested, but {exc} -- falling back to a fake client "
                "(pass --replay to make that the intended path)",
            )

    return (
        RazorpayExecutor.build(client=_FakeRazorpayClient(), registered_templates=template_ids()),
        "replay — deterministic, no network calls",
    )


# ---------------------------------------------------------------------------
# the demo clock
# ---------------------------------------------------------------------------
def clock_start(event: FailureEvent, time_str: str | None) -> datetime:
    """The scenario's own captured calendar date, IST, at --time (default
    noon). Anchoring to the payload's own date rather than a hardcoded one
    keeps every scenario's "today" honestly tied to something on disk.

    PolicyMachine.observe() refuses to schedule from a clock it doesn't
    believe (MAX_CLOCK_SKEW, vasool/policy/machine.py) whenever the event's
    own `occurred_at` reads as being in the future relative to `now` — real
    protection against a corrupted timestamp, adversary attack A18. A demo
    clock built from --time alone can trip that by accident: several
    payloads on disk were captured in the late afternoon IST, so the default
    --time of noon would read as "now" arriving before the webhook it's
    processing. Rolling forward to the next day whenever that would happen
    keeps every --scenario x --time combination honest without weakening the
    guard the demo exists to show off.
    """
    occurred_ist = event.occurred_at.astimezone(IST)
    hour, minute = 12, 0
    if time_str is not None:
        parts = time_str.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"--time must be HH:MM, got {time_str!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"--time out of range: {time_str!r}")

    local_date = occurred_ist.date()
    candidate = occurred_ist.replace(
        year=local_date.year, month=local_date.month, day=local_date.day, hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate < occurred_ist:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------
class _Stages:
    def __init__(self) -> None:
        self._n = 0

    def next(self, title: str) -> None:
        self._n += 1
        print()
        print(f"[{self._n}] {title}")


def _rule(char: str = "=") -> None:
    print(char * WIDTH)


def _kv(label: str, value: object, *, width: int = 13) -> None:
    print(f"{INDENT}{label:<{width}}: {value}")


def _block(label: str, text: str, *, width: int = 13) -> None:
    wrapped = textwrap.wrap(text, width=WIDTH - width - len(INDENT) - 2) or [""]
    print(f"{INDENT}{label:<{width}}: {wrapped[0]}")
    for line in wrapped[1:]:
        print(f"{INDENT}{'':<{width}}  {line}")


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f} ({paise} paise)"


def _fmt_ist(when: datetime) -> str:
    return f"{when.astimezone(IST):%Y-%m-%d %H:%M} IST"


def _print_verdict(v: Verdict) -> None:
    name_w, decision_w = 20, 15
    line = f"{INDENT}{v.guard:<{name_w}}{v.decision.value:<{decision_w}}"
    if v.decision is Decision.DEFER and v.defer_until is not None:
        line += f"-> {_fmt_ist(v.defer_until)}"
    print(line.rstrip())
    pad = " " * (name_w + decision_w)
    wrap_width = WIDTH - len(INDENT) - len(pad)
    if v.statute:
        for line in textwrap.wrap(v.statute, width=wrap_width):
            print(f"{INDENT}{pad}{line}")
    if v.reason:
        for line in textwrap.wrap(v.reason, width=wrap_width):
            print(f"{INDENT}{pad}{line}")


_LIVE_CAVEAT = (
    "you asked for --live: this run will call Razorpay's real test-mode API "
    "for every allowed action. A REAUTH_LINK/REATTEMPT_LINK creates a "
    "genuine Payment Link on the merchant's test-mode account; a "
    "SILENT_RETRY/TIMED_RETRY calls createRecurring against a payment with "
    "no real recurring token behind it, which fails at Razorpay's own "
    "boundary (docs/VERIFIED.md). Only payment_failed is a failure Razorpay "
    "itself has ever produced live -- every other --scenario is still "
    "played from a stubbed payload regardless. Omit --live (or pass "
    "--replay) for a fully offline, deterministic run."
)


def _print_live_caveat() -> None:
    """Printed first, before load_dotenv() or any stage -- the whole point
    of item 1 is that this is on screen before anything acts, not tucked
    into the `mode:` line after the run is already under way."""
    _rule("-")
    print("LIVE MODE")
    for line in textwrap.wrap(_LIVE_CAVEAT, width=WIDTH):
        print(line)
    _rule("-")
    print()


def _print_receipt(r: Receipt) -> None:
    _kv("outcome", r.outcome.value)
    _kv("receipt_id", r.receipt_id)
    _kv("hash", r.hash)
    _kv("prev_hash", r.prev_hash)
    if r.amount_recovered_paise:
        _kv("recovered", _rupees(r.amount_recovered_paise))
    print()


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def _print_banner(
    args: argparse.Namespace, event: FailureEvent, simulated: bool, start: datetime, *, rolled: bool
) -> None:
    _rule()
    print("  VASOOL -- recovery episode demo")
    _rule()
    _kv("scenario", args.scenario)
    _kv("world", args.world)
    clock_note = " (rolled +1d -- see --help time)" if rolled else ""
    _kv("clock", f"{_fmt_ist(start)}  [--time {args.time or '12:00 (default)'}]{clock_note}")
    _kv("provenance", "SIMULATED stub payload" if simulated else "captured live payload")


def _stage_webhook(stages: _Stages, fixture: dict, event: FailureEvent) -> None:
    stages.next("webhook received")
    _kv("event", fixture["body"].get("event", "unknown"))
    _kv("event_id", fixture["headers"]["x-razorpay-event-id"])
    _kv("entity_id", event.entity_id)
    _block("reason", f"{event.error_reason} / source: {event.error_source}")
    _kv("amount", _rupees(event.amount_paise))


def _stage_signature(stages: _Stages, fixture: dict, simulated: bool) -> None:
    stages.next("signature")
    if simulated:
        note = (
            "skipped -- _SIMULATED payload; its signature was copied from the "
            "real envelope it was derived from and does not cover this body "
            "(see docs/VERIFIED.md). Nothing here was ever sent by Razorpay, "
            "so there is nothing to authenticate. The HMAC scheme itself is "
            "verified against nine signatures Razorpay actually computed and "
            "sent -- tests/test_receiver.py, from data/observed_payloads/."
        )
        for line in textwrap.wrap(note, width=WIDTH - len(INDENT)):
            print(f"{INDENT}{line}")
        return

    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        print(f"{INDENT}skipped -- RAZORPAY_WEBHOOK_SECRET not set")
        return

    raw_body = json.dumps(fixture["body"], separators=(",", ":")).encode()
    ok = verify_signature(raw_body, fixture["headers"]["x-razorpay-signature"], secret)
    _kv("hmac-sha256", "verified" if ok else "FAILED")


def _stage_dedupe(stages: _Stages, fixture: dict, event: FailureEvent, at: datetime) -> None:
    stages.next("deduped")
    store = EventStore()
    event_id = fixture["headers"]["x-razorpay-event-id"]
    first = store.append(
        event_id=event_id, event_name="payment.failed", received_at=at, raw_body=fixture["body"], failure_event=event
    )
    second = store.append(
        event_id=event_id, event_name="payment.failed", received_at=at, raw_body=fixture["body"], failure_event=event
    )
    _kv("1st delivery", "stored" if first else "duplicate (unexpected)")
    _kv("2nd delivery", "duplicate, ignored" if not second else "stored (unexpected)")
    print(f"{INDENT}Razorpay delivers every webhook at least twice (docs/VERIFIED.md);")
    print(f"{INDENT}dedupe on x-razorpay-event-id is required, not defensive.")


def _stage_classified_and_proposed(stages: _Stages, scheduled: list[Transition]) -> None:
    stages.next("classified")
    first = scheduled[0].proposal
    assert first is not None
    _kv("failure_class", first.failure_class.value)
    _block("rationale", first.rationale)

    stages.next(f"proposal{'s' if len(scheduled) > 1 else ''}")
    for n, t in enumerate(scheduled):
        p = t.proposal
        assert p is not None
        if n:
            print()
        _kv("intervention", f"{p.intervention.value} ({p.role.value})")
        _kv("execute_at", _fmt_ist(p.execute_at))
        if p.channel is not None:
            _kv("channel", f"{p.channel.value} -> {p.template_id}")


def _gate_cycle(
    stages: _Stages, transitions: list[Transition], i: int, cycle: int
) -> int:
    """Render one GATED transition and the outcome that immediately follows
    it. Returns the index of the next unconsumed transition."""
    gated = transitions[i]
    assert gated.to_state is State.GATED and gated.chain is not None and gated.proposal is not None

    stages.next(f"guard chain -- cycle {cycle} ({_fmt_ist(gated.at)})")
    _kv("proposal", f"{gated.proposal.intervention.value} ({gated.proposal.role.value})")
    print()
    for v in gated.chain.verdicts:
        _print_verdict(v)

    outcome = transitions[i + 1]
    stages.next(f"decision -- cycle {cycle}")
    line = f"{gated.chain.decision.value}"
    if gated.chain.decision is Decision.DEFER:
        line += f" -> {_fmt_ist(gated.chain.defer_until)}"
    _kv("resolved", line)
    if gated.chain.decision is Decision.ALLOW:
        n = len(gated.chain.verdicts)
        _block(
            "clause",
            f"all {n} guards evaluated; none blocked, deferred, or escalated it "
            "-- no statute bars this action",
        )
    else:
        clauses = sorted({v.statute for v in gated.chain.deciding() if v.statute}) or ["no statute cited"]
        _block("clause", ", ".join(clauses))

    consumed = 2
    if outcome.to_state is State.EXECUTING:
        print(f"{INDENT}executing {gated.proposal.intervention.value}...")
        consumed = 3  # EXECUTING is immediately followed by AWAITING
    elif outcome.to_state is State.DEFERRED:
        assert outcome.proposal is not None
        print(f"{INDENT}re-queued for {_fmt_ist(outcome.proposal.execute_at)}")
    elif outcome.to_state is State.BLOCKED:
        print(f"{INDENT}blocked -- no action will be taken")
    elif outcome.to_state is State.ESCALATED:
        print(f"{INDENT}escalated -- handed to a human queue, executor never called")

    return i + consumed


def _stage_settlement(stages: _Stages, event: FailureEvent, machine: PolicyMachine) -> None:
    """Simulate the payment_link.paid webhook that closes this episode.

    Loads the one payment_link.paid payload this project has ever captured
    live (data/observed_payloads/) and sets its `notes` to what
    vasool/actions/executor.py::RazorpayExecutor._link actually stamps on
    every link it creates. `notes` is merchant-supplied metadata, not
    something Razorpay decides, so setting it here supplies the one part of
    the envelope that is genuinely ours rather than inventing a payload
    Razorpay never sent — see docs/VERIFIED.md and
    vasool/events/settlement.py, whose real correlation functions this reads
    the result back through rather than hand-computing it. Everything else
    in the envelope — the event name, the amount, the shape — is the real
    capture, unmodified.

    Item 3: this was never sent by Razorpay, and stage 2 already goes out of
    its way to say that about a _SIMULATED payload's signature. Saying
    nothing here, on the headline metric, was the asymmetry item 3 exists to
    fix -- so this prints its own provenance line rather than reading like a
    webhook that arrived.
    """
    stages.next("settlement webhook received")
    fixture = json.loads((OBSERVED_DIR / "payment_link_paid__none__12b6f2.json").read_text())
    body = copy.deepcopy(fixture["body"])
    body["payload"]["payment_link"]["entity"]["notes"] = {"vasool_entity_id": event.entity_id}

    entity_id = entity_id_from_payment_link_paid(body)
    assert entity_id == event.entity_id  # the notes tag just set, read back for real
    amount = amount_paise_from_payment_link_paid(body)

    _kv("event", body.get("event", "unknown"))
    _kv("entity_id", entity_id)
    _kv("provenance", "constructed")
    _block(
        "note",
        "never sent by Razorpay -- the one payment_link.paid envelope this "
        "account has ever captured live, with notes.vasool_entity_id "
        "injected to stand in for a link this run actually created. "
        "Whether Razorpay echoes notes back unmodified on this event has "
        "never been observed live (docs/VERIFIED.md).",
    )
    _block(
        "correlation",
        "notes.vasool_entity_id -- the same field executor.py tags every link it creates with",
    )
    _kv("amount", _rupees(amount))

    machine.settled(entity_id, reason="payment_link.paid", amount_paise=amount)


def _stage_settlement_retry(
    stages: _Stages, event: FailureEvent, machine: PolicyMachine, executor: RazorpayExecutor
) -> None:
    """Simulate the payment.captured webhook that closes a SILENT_RETRY/
    TIMED_RETRY-initiated episode (item 2).

    Unlike the link path there is no merchant-controlled notes field to
    inject -- the correlation is executor.py's own RetryIndex, keyed on the
    id retry_payment actually returned during this same run. Finds that real
    record on the executor's own journal (never fabricated) and stamps the
    one real payment.captured envelope this account has ever captured live
    with it. Same item-3 treatment as the link path: says on screen that
    this was constructed, and names what has never been observed.
    """
    retry_transition = next(
        t
        for t in machine.transitions
        if t.to_state is State.EXECUTING
        and t.proposal is not None
        and t.proposal.entity_id == event.entity_id
        and t.proposal.is_retry
    )
    record = executor.journal.get(retry_transition.proposal.proposal_id)
    assert record is not None and record.razorpay_request_id is not None

    stages.next("settlement webhook received")
    fixture = json.loads((OBSERVED_DIR / "payment_captured__none__0ced11.json").read_text())
    body = copy.deepcopy(fixture["body"])
    body["payload"]["payment"]["entity"]["id"] = record.razorpay_request_id
    body["payload"]["payment"]["entity"]["amount"] = event.amount_paise

    entity_id = entity_id_from_payment_captured(body, retry_index=executor.retry_index)
    assert entity_id == event.entity_id  # RetryIndex's own record, read back for real
    amount = amount_paise_from_payment_captured(body)

    _kv("event", body.get("event", "unknown"))
    _kv("entity_id", entity_id)
    _kv("provenance", "constructed")
    _block(
        "note",
        "never sent by Razorpay -- the one payment.captured envelope this "
        "account has ever captured live, with the payment id retry_payment "
        "actually returned this run stamped into payload.payment.entity.id. "
        "Whether createRecurring's returned id is the id that later appears "
        "captured has never been observed live (docs/VERIFIED.md).",
    )
    _block(
        "correlation",
        "RetryIndex -- executor.py's own record of the id retry_payment returned for this entity_id",
    )
    _kv("amount", _rupees(amount))

    machine.settled(entity_id, reason="payment.captured", amount_paise=amount)


def run(args: argparse.Namespace) -> int:
    # This script's own printed stages are the narrative; the operational
    # log.warning() calls scattered through vasool/ (unregistered templates,
    # unmapped reasons, no comms transport wired) are real signal in
    # production but debug spam on a screen recording, so they're raised
    # above WARNING rather than left to print to stderr mid-narrative.
    logging.getLogger("vasool").setLevel(logging.ERROR)

    live = args.live and not args.replay
    if live:
        _print_live_caveat()

    load_dotenv()
    pepper = os.environ.get("VASOOL_ID_PEPPER")
    if not pepper:
        print("error: VASOOL_ID_PEPPER is not set -- see .env.example", file=sys.stderr)
        return 1

    try:
        fixture, simulated, event = load_scenario(args.scenario, pepper=pepper)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        start = clock_start(event, args.time)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    rolled = start.astimezone(IST).date() != event.occurred_at.astimezone(IST).date()

    clock = VirtualClock(start)
    executor, mode = build_executor(live=live)

    _print_banner(args, event, simulated, start, rolled=rolled)
    _kv("mode", mode)

    stages = _Stages()
    _stage_webhook(stages, fixture, event)
    _stage_signature(stages, fixture, simulated)
    _stage_dedupe(stages, fixture, event, start)

    _WORLDS = {
        "permissive": _DemoFacts,
        "hostile": _HostileDemoFacts,
        "hostile_dlt": _HostileDLTDemoFacts,
    }
    facts = _WORLDS[args.world]()
    machine = PolicyMachine(clock=clock, facts=facts, executor=executor)
    machine.observe(event)

    all_transitions = list(machine.transitions)
    scheduled = [t for t in all_transitions if t.to_state is State.SCHEDULED]
    _stage_classified_and_proposed(stages, scheduled)

    seen = len(all_transitions)
    cycle = 0
    MAX_CYCLES = 25
    for _ in range(MAX_CYCLES):
        machine.tick()
        transitions = list(machine.transitions)
        i = seen
        while i < len(transitions):
            t = transitions[i]
            if t.to_state is State.GATED:
                cycle += 1
                i = _gate_cycle(stages, transitions, i, cycle)
            else:
                i += 1
        seen = len(transitions)

        pending = machine.pending()
        if not pending:
            break
        next_time = min(item.proposal.execute_at for item in pending)
        if next_time > clock.now():
            print()
            print(f"{INDENT}-- clock fast-forwarded to {_fmt_ist(next_time)} --")
            clock.advance_to(next_time)
    else:
        print("warning: hit the demo's cycle cap with work still pending", file=sys.stderr)

    chain = build_from_transitions(machine.transitions, call_journal=executor.journal, trace_id_of=trace_id_for)
    stages.next("receipt" + ("s" if len(chain) != 1 else ""))
    for r in chain:
        _print_receipt(r)

    receipts = list(chain)
    if args.settle:
        executing = [
            t for t in machine.transitions if t.to_state is State.EXECUTING and t.proposal is not None
        ]
        retried = any(t.proposal.is_retry for t in executing)
        if retried:
            _stage_settlement_retry(stages, event, machine, executor)
        else:
            _stage_settlement(stages, event, machine)
        receipts = list(
            build_from_transitions(machine.transitions, call_journal=executor.journal, trace_id_of=trace_id_for)
        )
        new_receipts = receipts[len(chain) :]
        if new_receipts:
            stages.next("receipt" + ("s" if len(new_receipts) != 1 else ""))
            for r in new_receipts:
                _print_receipt(r)

    _print_summary(stages, machine, event, receipts)
    return 0


_VERB_FOR_DECISION = {
    Decision.ALLOW: "ALLOWED",
    Decision.DEFER: "DEFERRED",
    Decision.BLOCK: "BLOCKED",
    Decision.ESCALATE: "ESCALATED",
}

_EPILOGUE_FOR_FINAL_STATE = {
    State.AWAITING: "then EXECUTED",
    State.BLOCKED: "and stayed BLOCKED",
    State.ESCALATED: "and stayed with a human, executor never called",
    State.DEFERRED: "and is still waiting to be re-gated",
    State.RECOVERED: "then RECOVERED",
}


def _verb_for(first_gate: Transition) -> str:
    """What actually happened, not the raw Decision.

    RiskBlockGuard correctly returns ALLOW for a HUMAN_QUEUE proposal — it is
    permitting the *escalation*, not a money movement (docs/taxonomy.md §2's
    hard-stop-and-queue path). But vasool/policy/machine.py::_gate then
    routes that ALLOW straight to ESCALATED without ever calling the
    executor, so printing the literal "ALLOWED" on the one path whose entire
    correctness is inaction reads as its own opposite. The fix is here, in
    the prose: the Verdict and the guard's ALLOW are untouched.
    """
    decision = first_gate.chain.decision
    handoff = (
        decision is Decision.ALLOW
        and first_gate.proposal.intervention is InterventionType.HUMAN_QUEUE
    )
    return "ESCALATED" if handoff else _VERB_FOR_DECISION[decision]


def _print_summary(stages: _Stages, machine: PolicyMachine, event: FailureEvent, receipts: list[Receipt]) -> None:
    gated = [t for t in machine.transitions if t.to_state is State.GATED]
    final_state = machine.state_of(event.entity_id)

    print()
    _rule()
    if gated:
        first_gate = gated[0]
        assert first_gate.chain is not None and first_gate.proposal is not None
        verb = _verb_for(first_gate)
        clauses = sorted({v.statute for v in first_gate.chain.deciding() if v.statute})
        by_clause = f" by {clauses[0]}" if clauses else ""
        epilogue = _EPILOGUE_FOR_FINAL_STATE.get(final_state, "") if final_state else ""
        if final_state is State.RECOVERED and receipts and receipts[-1].outcome is Outcome.RECOVERED:
            epilogue += f" -- {_rupees(receipts[-1].amount_recovered_paise)} recovered"
        summary = (
            f"SUMMARY: {first_gate.proposal.intervention.value} for {event.entity_id} "
            f"-- {verb}{by_clause}" + (f", {epilogue}." if epilogue else ".")
        )
    else:
        summary = f"SUMMARY: no action was gated for {event.entity_id}."
    for line in textwrap.wrap(summary, width=WIDTH):
        print(line)
    if receipts:
        print(f"Receipt: {receipts[-1].hash}")
    _rule()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_HELP_INTRO = (
    "Run one recovery episode end to end: webhook -> signature -> dedupe -> "
    "classify -> propose -> gate (13 guards) -> execute -> receipt."
)

_HELP_LIVE_MODE_NOTE = (
    "REPLAY IS THE DEFAULT: no flag needed, fully offline, deterministic --  "
    "this is the command a fresh `git clone` should run first. Pass --live "
    "to opt in to Razorpay's real test-mode API instead; its caveat prints "
    "before anything else runs, not after. READ BEFORE RECORDING WITH "
    "--live: the only failure reason Razorpay's own test mode has ever "
    "produced live is payment_failed (docs/VERIFIED.md) -- every Error "
    "Scenario test card returns it regardless of which one you pick. Every "
    "other --scenario, live or not, is played from a hand-built _SIMULATED "
    "payload on disk. What --live changes is whether the ACTIONS are real: "
    "a REAUTH_LINK creates a genuine Razorpay Payment Link, and a "
    "SILENT_RETRY calls createRecurring against a payment with no real "
    "recurring token behind it, which fails at Razorpay's boundary. --live "
    "therefore demonstrates the pipeline and the guard chain, not a "
    "successful recovery."
)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vasool-demo",
        description=(
            textwrap.fill(_HELP_INTRO, width=WIDTH)
            + "\n\n"
            + textwrap.fill(_HELP_LIVE_MODE_NOTE, width=WIDTH)
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scenario",
        default="card_expired",
        choices=sorted(known_reasons()),
        help="which failure to demo (default: card_expired)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "opt in to a real RazorpayClient against Razorpay's test-mode API "
            "(RAZORPAY_KEY_ID/SECRET from .env) instead of the offline fake. "
            "Prints its caveat before anything else runs. Falls back to the "
            "fake automatically, with a printed note, if credentials aren't "
            "configured."
        ),
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help=(
            "explicit, redundant now that replay is the default -- kept so "
            "the documented money-shot command still works unchanged. If "
            "passed alongside --live, --replay wins and nothing live happens."
        ),
    )
    parser.add_argument(
        "--time",
        default=None,
        metavar="HH:MM",
        help=(
            "override the demo clock's start time, IST (default: 12:00). If "
            "this is earlier in the day than the scenario's own captured "
            "timestamp, the clock rolls forward one day so it never "
            "predates the webhook it is processing -- see clock_start()."
        ),
    )
    parser.add_argument(
        "--world",
        choices=("permissive", "hostile", "hostile_dlt"),
        default="permissive",
        help=(
            "permissive (default): fresh consent, clean DND, every template "
            "registered -- the known-good baseline every guard agrees with. "
            "hostile: no consent record on file at all, so ConsentGuard "
            "fails closed and BLOCKS (DPDP). hostile_dlt: no DLT template "
            "registered at all, so DLTTemplateGuard blocks instead (TRAI) -- "
            "a second guard, a different statute, a different mechanism."
        ),
    )
    parser.add_argument(
        "--settle",
        action="store_true",
        help=(
            "after the run, simulate the payment_link.paid webhook that "
            "closes this episode -- shows the RECOVERED receipt with a "
            "real, non-zero amount_recovered_paise (docs/VERIFIED.md: only "
            "payment_link.paid can be attributed to an episode)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """`run()` already turns the two expected failure modes (an unknown
    scenario, a malformed --time) into a clean `error:` line and exit 1 —
    nothing is caught here, so a genuine bug still surfaces as a real
    traceback rather than being misreported as one of those two."""
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
