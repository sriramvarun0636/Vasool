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