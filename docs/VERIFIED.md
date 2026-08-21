# Verified against live Razorpay test mode

## 2026-08-21

### Account
- Test-mode API works on an unactivated account (no KYC). Dashboard is gated
  behind onboarding, API is not.
- Webhook registration works via API on an unactivated account.

### Webhooks API quirk
- POST /v1/webhooks rejects `events` as a JSON array:
  `{"events": ["payment.failed"]}` -> "Invalid event name/names: 1, 2, 3, 4"
- It requires an object keyed by event name:
  `{"events": {"payment.failed": 1}}` -> success
- Registered webhook id: TSLRoqjLMLJTmf

### Observed error_reason strings
(to be filled as payloads are captured)

### More API quirks (2026-08-21)
- PATCH /v1/webhooks/{id} -> "no Route matched with those values".
  No update endpoint; register a new webhook instead.
- Payment Links reject contacts with repeated digits:
  +919999999999 -> "Recurring digits in customer contact are disallowed".
  Use a varied number, e.g. +919876543210.

### First live capture (2026-08-21, 13:36 IST)
- x-razorpay-event-id IS present on payment.failed. Dedupe key confirmed.
- Plain test card yields generic error_reason "payment_failed", not a
  specific reason. Specific reasons need the Error Scenario cards.
- DUPLICATE DELIVERY OBSERVED: identical event-id TSLcxIwEqbIaWt delivered
  twice at 13:36:39.625 from two Razorpay IPs (52.66.75.174, 52.66.76.63).
  Duplicate webhook delivery is normal operation, not an edge case.
  Idempotency on event_id is REQUIRED, not defensive.

### Error Scenario cards — docs vs observed (2026-08-21)
| Card (Visa)          | Docs claim         | Observed                          |
|----------------------|--------------------|-----------------------------------|
| 4100 2800 0009 0000  | payment_timed_out  | payment_failed / gateway /
                                              payment_authorization           |
NOTE: Error Scenario cards did NOT produce the documented specific reason
via the Payment Links checkout flow. Confirming across more cards.

### FINDING: Error Scenario cards do not work in test mode (2026-08-21)

Razorpay documents 7 "Error Scenario" cards that should each produce a
specific error_reason. None of them do.

Tested via Payment Links checkout AND standard Checkout.js:
| Card (Visa)          | Docs claim          | Observed (all flows)  |
|----------------------|---------------------|-----------------------|
| 4100 2800 0009 0000  | payment_timed_out   | payment_failed        |
| 4100 2800 0008 0001  | insufficient_fund   | payment_failed        |

Every failure returns identically:
  error_reason : payment_failed
  error_code   : BAD_REQUEST_ERROR
  error_source : gateway
  error_step   : payment_authorization

Root cause (inferred): the mock bank page fails the payment at the gateway
layer, which overwrites the reason the card was meant to encode. `source:
gateway` rather than `bank` or `customer` is the evidence.

CONSEQUENCE: only ONE failure reason is reproducible live. The remaining
taxonomy classes must be exercised with hand-built payloads derived from
the observed schema, kept in data/stubbed_payloads/ and labelled SIMULATED.

Also confirmed across ~8 events: EVERY webhook is delivered twice with an
identical x-razorpay-event-id, from two Razorpay IPs. Duplicate delivery is
normal operation. Idempotency is required, not defensive.
