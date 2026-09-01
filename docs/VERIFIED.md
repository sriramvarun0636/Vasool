# Verified against live Razorpay test mode

All findings below were observed directly, not taken from documentation.
Account: unactivated merchant (no KYC completed), test mode, 2026-08-21.

---

## Account access

- **Test-mode API works on an unactivated account.** No KYC required.
- **The dashboard is gated** behind onboarding/video-KYC; the API is not.
  All work below was done via the API.
- Webhook registration works via API on an unactivated account.

---

## FINDING: Error Scenario cards do not work in test mode

Razorpay documents 7 "Error Scenario" cards, each of which should produce a
specific `error_reason`. **None of them do.**

Tested via both Payment Links checkout and standard Checkout.js:

| Card (Visa)         | Docs claim        | Observed       |
|---------------------|-------------------|----------------|
| 4100 2800 0009 0000 | payment_timed_out | payment_failed |
| 4100 2800 0008 0001 | insufficient_fund | payment_failed |

Every failure returns identically, regardless of card or flow:

error_reason : payment_failed
error_code : BAD_REQUEST_ERROR
error_source : gateway
error_step : payment_authorization


**Root cause (inferred):** the mock bank page fails the payment at the gateway
layer, which overwrites the reason the card was meant to encode. The evidence
is `error_source: gateway` rather than `bank` or `customer`.

**Consequence:** only ONE failure reason is reproducible live. All other
taxonomy classes are exercised with hand-built payloads in
`data/stubbed_payloads/`, every file marked `_SIMULATED: true`.

---

## FINDING: duplicate webhook delivery is normal

**Every** webhook observed (~8 events) was delivered **twice**, with an
identical `x-razorpay-event-id`, from two Razorpay IPs
(52.66.75.174 and 52.66.76.63), within the same millisecond.

First observed: event-id `TSLcxIwEqbIaWt` at 13:36:39.625 IST.

Duplicate delivery is normal operation, not an edge case.
**Idempotency on `event_id` is required, not defensive.**

---

## FINDING: subscriptions unavailable pre-activation

`POST /v1/webhooks` rejects every subscription event:

subscription.authenticated / activated / charged / pending / halted / cancelled
-> "Invalid event name/names: ..."


Consistent with the first webhook registration response, which enumerated 40+
supported events and included no `subscription.*` events at all. Subscriptions
are not enabled on an unactivated merchant account.

**Consequence:** the failed-subscription recovery loop cannot be exercised
live. `subscription.pending` and `subscription.halted` payloads are hand-built
in `data/stubbed_payloads/`, and the 4-consecutive-failure halt rule is taken
from documentation rather than observation.

Session 0B closed here. Not pursued further.

---

## API quirks

- **`POST /v1/webhooks` needs `events` as an object, not an array.**
  `{"events": ["payment.failed"]}` → `Invalid event name/names: 1, 2, 3, 4`
  `{"events": {"payment.failed": 1}}` → success
- **There is no `PATCH /v1/webhooks/{id}`.**
  Returns `no Route matched with those values`. Register a new webhook instead.
- **Payment Links reject contacts with repeated digits.**
  `+919999999999` → `Recurring digits in customer contact are disallowed`.
  Use a varied number, e.g. `+919876543210`.

---

## Confirmed schema facts

- `x-razorpay-event-id` **is present** on every webhook. It is the dedupe key.
- A successful payment via Payment Links fires three events in sequence:
  `payment.captured` → `order.paid` → `payment_link.paid`.
  The agent must recognise out-of-band success and stop any pending recovery.

---

## NOTE: future captures should store raw_body_b64, not just parsed JSON

`tools/catch.py` stores `await request.json()` (the parsed dict) rather than
the raw request bytes. HMAC-SHA256 signature verification has to run over the
exact bytes Razorpay sent — and it turns out `json.dumps(body,
separators=(",", ":"))` reproduces those bytes exactly for every payload in
`data/observed_payloads/`, confirmed by recomputing the signature against
every recorded `x-razorpay-signature` with the real `RAZORPAY_WEBHOOK_SECRET`
(see `tests/test_receiver.py::test_real_captured_signature_verifies`).

That reconstruction is not a guarantee, though — it works today because
`json.loads`/`json.dumps` round-trips key order and number formatting
losslessly for these particular payloads, and because Razorpay's client sends
compact JSON with no whitespace. A payload shaped differently (non-ASCII
text, unusual number formatting) could silently break it without any test
here catching the drift.

A future capture pass should add `raw_body_b64` (base64 of the untouched
request body) alongside the parsed JSON, so signature verification never
depends on this round-trip continuing to hold.

---

## Registered webhooks

| id             | events                                              |
|----------------|-----------------------------------------------------|
| TSLRoqjLMLJTmf | payment.failed only (first registration)            |
| TSLTQxFbCjlrod | payment.failed, captured, authorized, order.paid, payment_link.paid/expired, invoice.expired, refund.processed |

Note: both are active, so events currently arrive from both registrations
**in addition to** Razorpay's own duplicate delivery.
### FINDING: UPI unavailable pre-activation (2026-08-21)

The Payment Links checkout on an unactivated account offers only Cards,
Netbanking, Wallet and Pay Later. UPI is absent, so `failure@razorpay` cannot
be exercised and no UPI payload could be captured.

CONSEQUENCE: the taxonomy is card-shaped by necessity. Any UPI-specific
error_reason handling would be pure speculation and is deliberately omitted.
Noted as an open question in docs/taxonomy.md §8.

### FINDING: error_source varies by rail (2026-08-21)

Netbanking failure returned:
  reason: payment_failed / code: BAD_REQUEST_ERROR / step: payment_authorization
  source: bank          <- cards return "gateway"

error_source is NOT constant — it reflects where in the stack the failure
occurred. This supports the inference that the mock bank page overwrites the
card's encoded reason at the gateway layer.

CONSEQUENCE: even when error_reason is uninformative, (reason, source) carries
signal. payment_failed/bank implies an issuer decline; payment_failed/gateway
implies a rail problem. The taxonomy should classify on the pair.

Also: UPI is unavailable pre-activation (Cards/Netbanking/Wallet/PayLater only).

### UPDATE: duplicate delivery attribution (2026-08-21, 16:20 IST)

Later captures (TSONnzuJtXefql, TSOPQFb6VsuJ5d) arrived EXACTLY ONCE each,
unlike the morning's paired deliveries. Since no registration was removed
(the API has no DELETE), the difference is unexplained — but single delivery
is clearly possible, which weakens the "Razorpay always double-delivers"
reading and strengthens the "two overlapping registrations" one.

Attribution remains unresolved and is now recorded as such.

CONCLUSION UNCHANGED: idempotency on event_id is required either way.
Duplicate registration is arguably the MORE likely production failure mode,
and the receiver handles both identically.

---

## DECISION: two events are wired to settle a recovery episode; `order.paid` never is

`vasool/events/receiver.py` calls `PolicyMachine.settled()` on two webhooks,
via `vasool/events/settlement.py`: `payment_link.paid`, for a
REAUTH_LINK/REATTEMPT_LINK, and `payment.captured`, for a
SILENT_RETRY/TIMED_RETRY this agent's own executor dispatched. All three
events named in the "successful Payment Links checkout" sequence above
(`payment.captured` -> `order.paid` -> `payment_link.paid`) are equally
truthful signals that money landed, but each can only be wired where
something actually on disk lets it be attributed to *which* recovery episode
it closes.

`payment.captured` and `order.paid` fire for every successful payment on the
account, including a customer's first-ever, never-failed checkout. Neither
payload carries a field marking it as closing a recovery on its own. The
only lead either offers is `order_id`, and it does not hold here: a
REAUTH_LINK/REATTEMPT_LINK this agent sends opens a brand-new Payment Link
with Razorpay's own newly allocated order, never the original failed
payment's `order_id`. Attempting to correlate by `order_id`, amount, or
customer would mean guessing a join key — exactly what the project's working
agreement says not to do.

`order.paid` stays unwired for exactly that reason: it has no attributable
field of its own, ever — nothing about it is more traceable than
`payment.captured`, so wiring it would add nothing the other two paths don't
already cover. **`order.paid` is received and stored (EventStore does this
for every event name) but never calls `settled()`.** This is recorded as an
honest gap rather than papered over.

`payment.captured` used to be unwired for the same reason. It no longer is,
for the SILENT_RETRY/TIMED_RETRY case specifically — see the RetryIndex
entry below — but the gap still stands for every `payment.captured` that
isn't one of our own retries: an ordinary checkout, or a Payment Links
payment (whose payment id `_link` never hands to RetryIndex — see
`vasool/actions/executor.py`), correlates to nothing, exactly as before.

`payment_link.paid` is different because the `notes` field on a Payment Link
is merchant-supplied metadata, not something Razorpay decides.
`vasool/actions/executor.py::RazorpayExecutor._link` sets
`notes={"vasool_proposal_id": ..., "vasool_entity_id": ...}` on every link it
creates, so a `payment_link.paid` webhook for a link this agent made carries
its own entity_id back with no join key to guess — see
`vasool/events/settlement.py`.

# VERIFY: whether Razorpay actually echoes `notes` back unmodified on the
`payment_link.paid` webhook has never been observed live. The only
`payment_link.paid` capture on this account (`payment_link_paid__none__12b6f2.json`)
predates the `vasool_entity_id` tag entirely — that link was created by hand
during Session 0A, so its own `notes` is `null`. The Payment Links API
documents `notes` as pass-through metadata and echoes it on create/fetch
responses, so the assumption that a webhook echoes it too is reasonable, but
it remains documentation until a link created with `vasool_entity_id` in its
notes is observed coming back with it on a real webhook.

## DECISION: `payment.captured` settles a retry episode via RetryIndex, not a notes tag

`createRecurring` (`RazorpayClient.retry_payment`) has no `notes` parameter
Session 0A ever observed, so a SILENT_RETRY/TIMED_RETRY has nothing to tag
the way `_link` tags a Payment Link. What it does have is Razorpay's own
response to the call: `retry_payment` returns the id of the payment it just
created, and `vasool/actions/executor.py::RazorpayExecutor._retry` now
records that id against the entity_id that asked for it, in its own
in-memory `RetryIndex`. `vasool/events/settlement.py::entity_id_from_payment_captured`
reads a later `payment.captured`'s payment id back through that index — our
own record coming back, not a guessed join key.

**RetryIndex is process-local, on purpose and stated plainly, not silently.**
Nothing in this codebase durably stores the action plane's own call history
yet: `ExecutionJournal` beside it has exactly the same property, `EventStore`
is scoped to *received* webhooks only, and the transition log deliberately
never carries Razorpay-shaped data (see `vasool/ledger/receipts.py`'s
docstring on why). So a process restart between a retry firing and its
`payment.captured` arriving loses the mapping — that capture won't be
recognised as ours, and the episode stays in AWAITING rather than reaching
RECOVERED through this path. Not silently wrong; a real, accepted gap.

# VERIFY: whether `createRecurring`'s synchronous response id is the same id
that later appears on `payload.payment.entity.id` of a `payment.captured`
webhook has never been observed live. `RazorpayClient.retry_payment`'s own
VERIFY note already flags that this call was never exercised at all — Session
0A never activated the merchant account, so the token-based recharge path it
wraps (subscriptions / e-mandates) was never reachable to test. A payment
entity's id being stable across its own lifecycle is standard Razorpay
behaviour, and it's the same shape `tests/test_executor.py`'s fake client
already assumes, but it remains a documented assumption, not an observed
fact, until a real `createRecurring` response is captured and matched
against a subsequent `payment.captured`.
