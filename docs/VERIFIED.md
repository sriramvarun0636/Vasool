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
