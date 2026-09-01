"""Real envelopes off disk, stamped with simulated identity.

the project's working agreement is absolute about this: every `error_reason`
must come from `data/observed_payloads/` or `data/stubbed_payloads/`, and a
reason in neither directory does not exist. A simulator that generates
hundreds of thousands of events cannot honour that by care alone, so it is
honoured structurally — nothing here builds a payload, everything here copies
one and overwrites the fields that are ours.

**Which fields are ours.** Identity and context: who the payment was for, how
much, when, and its ids. The four error fields are never touched, in either
direction, and `tests/windtunnel/test_payloads.py` asserts it. That split is
the whole discipline: the failure is Razorpay's to describe and the customer
is the simulator's to invent.

**Why stamping is necessary at all.** Every file in `data/stubbed_payloads/`
was derived from a single real capture by `tools/make_stubs.py`, which edits
only the error fields — so they all share one `payment.entity.id`, one
contact, one amount and one `x-razorpay-event-id`. Used unmodified they would
produce 500 customers who are the same customer, one episode that shadows
every other in the EpisodeStore, and a receipt-id collision of exactly the
kind `vasool/ledger/receipts.py::_receipt_id` documents. §2a's "every receipt
id unique across the run" would fail for a fixture artifact rather than for a
defect.

**The one pair that has to be assembled: `payment_failed` / `business`.**
§3d's 70/25/5 split needs it, and no single file carries it — every
`payment_failed` envelope on disk is `gateway` or `bank`, and the only
`business` ever written down sits on the risk-check stub, hand-set from
documentation and never observed (docs/taxonomy.md §9.7). Both strings exist
in `data/`; only their combination does not. So it is assembled by reading
each off disk — the same move `tests/payloads.py::event_for(reason, source)`
already makes, and the reason `NEVER_OBSERVED_SOURCE` exists there. A source
string that appears on no payload at all cannot be requested: `_source_on_disk`
raises rather than accepting one.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import pathlib
from datetime import datetime
from typing import Any

from vasool.events.schemas import FailureEvent, from_webhook
from vasool.events.settlement import RetryIndex

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OBSERVED_DIR = REPO_ROOT / "data" / "observed_payloads"
STUBBED_DIR = REPO_ROOT / "data" / "stubbed_payloads"

LINK_PAID_CAPTURE = OBSERVED_DIR / "payment_link_paid__none__12b6f2.json"
PAYMENT_CAPTURED_CAPTURE = OBSERVED_DIR / "payment_captured__none__0ced11.json"
"""The only two settlement envelopes this account has ever captured live. The
same two vasool/demo.py replays, for the same reason: they are what a real
settlement looks like, and nothing else on disk is."""


class NoSuchPayload(LookupError):
    """A (reason, source) pair that cannot be built from anything on disk.

    Raised rather than defaulted, because a default here is precisely the
    "helpfully add a plausible one" the project rules forbids. If this fires, either
    the registered mix named something that does not exist, or a payload
    directory changed.
    """


def _envelope_paths() -> list[pathlib.Path]:
    return sorted(OBSERVED_DIR.glob("payment_failed__*.json")) + sorted(
        STUBBED_DIR.glob("SIMULATED__payment_failed__*.json")
    )


@functools.cache
def _by_pair() -> dict[tuple[str, str], dict[str, Any]]:
    """Every (error_reason, error_source) pair that exists on disk, mapped to
    the first envelope carrying it. Cached: the simulator asks per episode,
    and re-reading twenty files per draw would dominate the run."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for path in _envelope_paths():
        fixture = json.loads(path.read_text())
        entity = fixture["body"]["payload"]["payment"]["entity"]
        index.setdefault((entity["error_reason"], entity["error_source"]), fixture)
    return index


def available_pairs() -> frozenset[tuple[str, str]]:
    """Every (reason, source) pair with a payload of its own."""
    return frozenset(_by_pair())


@functools.cache
def _sources_on_disk() -> frozenset[str]:
    return frozenset(source for _reason, source in _by_pair())


def _template(reason: str, source: str) -> dict[str, Any]:
    """The envelope to stamp, for a (reason, source) pair.

    An exact match is used as-is. Otherwise the reason's own envelope is used
    with the source overwritten — but only with a source string that appears
    on some payload in data/. That is the whole of the concession, and it is
    the one `payment_failed`/`business` needs.
    """
    exact = _by_pair().get((reason, source))
    if exact is not None:
        return copy.deepcopy(exact["body"])

    for (on_disk_reason, _), fixture in _by_pair().items():
        if on_disk_reason != reason:
            continue
        if source not in _sources_on_disk():
            raise NoSuchPayload(
                f"error_source {source!r} appears on no payload in data/ — "
                "the project rules: a value in neither directory does not exist"
            )
        body = copy.deepcopy(fixture["body"])
        body["payload"]["payment"]["entity"]["error_source"] = source
        return body

    raise NoSuchPayload(
        f"no payload in data/ for error_reason {reason!r} — add one to "
        "data/stubbed_payloads/ and a row to docs/taxonomy.md rather than "
        "generating a reason that does not exist"
    )


def _event_id_for(entity_id: str, sequence: int) -> str:
    """A stable, distinct dedupe key per simulated webhook.

    Every payload on disk carries one captured `x-razorpay-event-id`, so
    without this the whole run would be a single event as far as
    vasool/events/store.py is concerned. Derived rather than counted, so it
    does not depend on generation order (see windtunnel/rng.py).

    `sequence` distinguishes the successive failures of one recovery —
    vasool/policy/episode.py: "a single recovery spans several
    `payment.failed` webhooks — one per attempt". Each of those carries a
    different payment id, since `createRecurring` creates a new payment every
    time, so the id alone would already separate them; `sequence` is kept
    because it costs nothing and does not depend on that remaining true.
    """
    return "evt_" + hashlib.sha256(f"{entity_id}|{sequence}".encode()).hexdigest()[:14]


def source_on_disk(reason: str) -> str:
    """The `error_source` the payload for `reason` actually carries.

    So that a caller which only cares about the reason does not have to
    transcribe a source string — the one field pairing that has ever been
    observed for a reason is the one on its own envelope, and typing it out at
    a call site is how a pair that exists nowhere in data/ gets invented by
    accident. Raises rather than defaulting, exactly as `_template` does.
    """
    for on_disk_reason, on_disk_source in sorted(_by_pair()):
        if on_disk_reason == reason:
            return on_disk_source
    raise NoSuchPayload(
        f"no payload in data/ for error_reason {reason!r} — add one to "
        "data/stubbed_payloads/ and a row to docs/taxonomy.md rather than "
        "generating a reason that does not exist"
    )


def failure_body(
    *,
    reason: str,
    source: str | None = None,
    entity_id: str,
    contact: str,
    email: str,
    amount_paise: int,
    occurred_at: datetime,
) -> dict[str, Any]:
    """The `payment.failed` webhook body itself, identity stamped on.

    Split out of `failure_event` below for one caller: `windtunnel/adversary/`
    delivers webhooks through the real receiver rather than handing decoded
    events to the machine, so it needs the envelope a route would receive —
    signature, headers, dedupe and all. Same stamping, same untouched error
    fields; `failure_event` is now this plus the production decode.

    `source` defaults to whatever the reason's own payload carries, which is
    the only pairing that has ever existed for it.
    """
    body = _template(reason, source if source is not None else source_on_disk(reason))
    entity = body["payload"]["payment"]["entity"]

    entity["id"] = entity_id
    entity["contact"] = contact
    entity["email"] = email
    entity["amount"] = amount_paise
    entity["created_at"] = int(occurred_at.timestamp())
    body["created_at"] = int(occurred_at.timestamp())
    return body


def failure_event(
    *,
    reason: str,
    source: str,
    entity_id: str,
    contact: str,
    email: str,
    amount_paise: int,
    occurred_at: datetime,
    pepper: str,
    sequence: int = 0,
    retry_index: RetryIndex | None = None,
) -> FailureEvent:
    """A real `payment.failed` envelope with this episode's identity stamped
    on it, decoded through the production `from_webhook`.

    Decoded rather than constructed: `FailureEvent(...)` with typed-in strings
    would bypass both the envelope and the field mapping, and the point of
    this module is that the simulator and production read the same shape.

    `sequence` is 0 for a recovery's original failure and rises with each
    retry that fails after it — a new payment, the same episode, a new
    webhook.

    `entity_id` is what Razorpay stamps on the payload, which for a failed
    retry is the *new* payment `createRecurring` created, not the episode's
    original. Passing `retry_index` is what resolves the one to the other,
    through the same `from_webhook` the receiver calls — see
    Runner._deliver_retry_failures.
    """
    body = failure_body(
        reason=reason,
        source=source,
        entity_id=entity_id,
        contact=contact,
        email=email,
        amount_paise=amount_paise,
        occurred_at=occurred_at,
    )
    return from_webhook(
        event_id=_event_id_for(entity_id, sequence),
        body=body,
        pepper=pepper,
        retry_index=retry_index,
    )


def link_paid_body(*, entity_id: str, amount_paise: int) -> dict[str, Any]:
    """The `payment_link.paid` webhook a link this agent sent would fire.

    `notes.vasool_entity_id` is what `vasool/actions/executor.py::_link`
    stamps on every link it creates, and `notes` is merchant-supplied
    metadata rather than something Razorpay authors — so setting it here
    supplies the one part of the envelope that is genuinely ours. Everything
    else is the real capture.

    # VERIFY: whether Razorpay echoes `notes` back unmodified on this webhook
    # has never been observed live (docs/VERIFIED.md). The simulator assumes
    # it does, exactly as vasool/events/settlement.py does; if that assumption
    # is wrong, every link-settled recovery in this evaluation is optimistic.
    """
    body = copy.deepcopy(json.loads(LINK_PAID_CAPTURE.read_text())["body"])
    body["payload"]["payment_link"]["entity"]["notes"] = {"vasool_entity_id": entity_id}
    body["payload"]["payment"]["entity"]["amount"] = amount_paise
    return body


def capture_body(*, payment_id: str, amount_paise: int) -> dict[str, Any]:
    """The `payment.captured` webhook a retry would fire.

    `payment_id` is whatever `retry_payment` returned for this proposal, read
    back off the executor's own journal — never invented here, because the
    whole correlation depends on it being Razorpay's own id rather than a
    guessed join key (vasool/events/settlement.py).

    The same envelope carries out-of-band settlements, with a payment id no
    RetryIndex knows. That is not a special case in this function and must
    not become one: an out-of-band payment genuinely is an ordinary capture
    that correlates to nothing (docs/taxonomy.md §9.9).
    """
    body = copy.deepcopy(json.loads(PAYMENT_CAPTURED_CAPTURE.read_text())["body"])
    body["payload"]["payment"]["entity"]["id"] = payment_id
    body["payload"]["payment"]["entity"]["amount"] = amount_paise
    return body
