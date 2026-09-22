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

`createRecurring` (then `RazorpayClient.retry_payment`) has no `notes`
parameter Session 0A ever observed, so a SILENT_RETRY/TIMED_RETRY had nothing
to tag the way `_link` tags a Payment Link. What it does have is Razorpay's own
response to the call: the debit returns the id of the payment it just created,
and `vasool/actions/executor.py::RazorpayExecutor._retry` records that id
against the entity_id that asked for it, in its own `RetryIndex`.
`vasool/events/settlement.py::entity_id_from_payment_captured`
reads a later `payment.captured`'s payment id back through that index — our
own record coming back, not a guessed join key.

**Updated 2026-09-15 — the call does take `notes`; the index is still the
path.** The premise above is what Session 0A could see, not what the API does:
0A never reached `createRecurring` at all. *Create Subsequent Payments*
documents an optional `notes`, and `RazorpayClient.create_recurring_payment` —
which replaced `retry_payment`, see below — stamps `vasool_entity_id` on it
exactly as `_link` stamps a payment link. It is not read on the way back yet.
The response id stays the settlement path because it is the rail's own answer
to "what did you just create", whereas whether a payment entity carries its
`notes` back on a webhook is unobserved — the same VERIFY the Payment Link tag
carries above (`docs/EVALUATION.md` §10, 2026-09-15).

**Updated 2026-09-22 — the index survives a restart.** As first written this
was a real, accepted gap, stated plainly rather than silently: nothing durably
stored the action plane's own call history, so a process restart between a
retry firing and its `payment.captured` arriving lost the mapping — that
capture was not recognised as ours, and the episode stayed in AWAITING rather
than reaching RECOVERED through this path.
`vasool/actions/retry_store.py::SqlRetryIndex` now persists it, with WAL and
`synchronous=FULL` read back as the event store does. It lives in the action
plane, in a database of its own, because the transition log deliberately never
carries Razorpay-shaped data (see `vasool/ledger/receipts.py`'s docstring on
why) and a payment id the rail minted is that data;
`vasool/runtime/composition.py::build` refuses to start if the executor and the
runtime hold different indexes. The simulator keeps the in-memory index:
nothing in windtunnel restarts a process. `ExecutionJournal` beside it is still
in memory, and `EventStore` is still scoped to *received* webhooks only
(`docs/EVALUATION.md` §10, 2026-09-22).

# VERIFY: whether `createRecurring`'s synchronous response id is the same id
that later appears on `payload.payment.entity.id` of a `payment.captured`
webhook has never been observed live. `RazorpayClient.create_recurring_payment`'s
own VERIFY note already flags that this call was never exercised at all — Session
0A never activated the merchant account, so the token-based recharge path it
wraps (subscriptions / e-mandates) was never reachable to test. A payment
entity's id being stable across its own lifecycle is standard Razorpay
behaviour, and it's the same shape `tests/test_executor.py`'s fake client
already assumes, but it remains a documented assumption, not an observed
fact, until a real `createRecurring` response is captured and matched
against a subsequent `payment.captured`.

---

## DOCUMENTED, NOT OBSERVED: Razorpay's UPI Autopay subsequent payments (2026-09-15)

Nothing in this section was observed. UPI and subscriptions are unavailable on
this account before activation (above), so no recurring payment has ever been
created here. It records what Razorpay's documentation says, read from the
markdown sources Razorpay serves, each pinned by the SHA-256 of the bytes read,
because §2.5's failure path (`docs/EVALUATION.md` §10, 2026-09-15) is built on
it and a reader should be able to check every field against the page.

| Page | SHA-256 |
|---|---|
| *Create Subsequent Payments* (UPI) — `docs/api/payments/recurring-payments/upi/create-subsequent-payments.md` | `6c26636bafa1b66801ecae4b44b8250a354d5269d3a3c2ee6baf0adb972d1bfe` |
| *Create Subsequent Payments* (cards) — `docs/api/payments/recurring-payments/cards/create-subsequent-payments.md` | `35a65cbe5c3ab0c83a46755700f2aea04fbce88f3c59c011aeb68149d636cc44` |
| *List of Errors* — `docs/build/llm-docs/errors/payments/list.md` | `5e6fb5795996400ddd55938c60b217dbe403995af45669b12ac02a5b51c4281d` |
| *Payment Method Error Parameters* — `docs/build/llm-docs/errors/payments/payment-methods-error-parameters.md` | `526b34fe6bbf0d75cfa6067daceb752fc99ee92ecaf2a24b936d52302267d490` |

**No NPCI code reaches the merchant.** A failed subsequent UPI payment is
reported in Razorpay's error envelope — `code`, `description`, `source`, `step`,
`reason` — and the UPI page lists 61 values of `reason`. None of the four pages
names an NPCI response code or a field that would carry one. So NPCI's
vocabulary (`docs/taxonomy.md` §11) classifies nothing a Razorpay webhook
sends; §12 maps Razorpay's 61 reasons instead, and NPCI's codes wait behind a
port for a provider that passes them on.

**No reason is paired with a source or a step.** *Payment Method Error
Parameters* lists the values UPI's `source` and `step` can take; no page says
which go with which reason. *List of Errors* files 19 of the 61 under "Bad
Request Errors" or "Gateway Errors", the only `code` any of them is given. The
UPI stubs carry exactly that and null otherwise.

**The debit call `retry_payment` made was not the documented one.** A
recurring payment is `createRecurring` with eight mandatory fields — `email`,
`contact`, `currency`, `amount`, `order_id`, `customer_id`, `token`,
`recurring` — and an optional `notes`. `RazorpayClient.retry_payment` sent
`amount`, `currency` and a `payment_id` the documentation does not name. It was
replaced (`create_recurring_payment`); a live call with the old body would have
been refused with a 4xx.

**The pre-debit notice is an order.** "You can use the notification object in
the request if you want to control pre-debit notifications and recurring
debits": an order created with `notification: {token_id, payment_after}`, after
which Razorpay delivers the notice and the order reports `notification.status`
and `delivered_at`. Without the object, "we will automatically try to debit 25
hours after the pre-debit notification is delivered", and Razorpay retries on
its own; with it, "We will not attempt any retry if the debit fails … You should
manually retry the debit attempt." Vasool always passes it, so it is the only
thing retrying and NPCI's one attempt and three retries are not exceeded.
`payment_after` defaults to 25 hours after delivery, an hour past RBI's 24 —
unobserved whether a debit presented between the two is refused.

**Check before you debit again.** "Do not create another subsequent payment
until you get the status of the previous one." It agrees with NPCI OC-215, and
it is why a debit whose response is lost now goes to a status check instead of
being re-sent.

**`createRecurring` takes `notes`.** The earlier entries here say it has
"nothing resembling `notes`", which was true only of what Session 0A observed.
Documented, it does; the documented debiter stamps `vasool_entity_id` on it,
which could give a retry the same restart-proof join key a payment link has —
not relied on until a `payment.captured` for a recurring payment has been seen
carrying it.
