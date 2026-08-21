# Failure taxonomy

How Vasool classifies a failed payment, and what it does about each class.

This is the intellectual core of the system. Everything downstream — the policy
state machine, the guards, the evaluation — is mechanical once this is right.
Get this wrong and a correct implementation still recovers nothing.

**Provenance.** Every `error_reason` below has a payload in
`data/observed_payloads/` (captured live) or `data/stubbed_payloads/`
(documentation-derived, marked `_SIMULATED: true`). Per `CLAUDE.md`, a reason in
neither directory does not exist. See `docs/VERIFIED.md` for why only
`payment_failed` is reproducible live.

---

## 1. The governing insight

**A failed payment is not one thing.** The naive agent treats every failure as
"retry with backoff," which is wrong in three separate ways:

1. Some failures have **zero** probability of succeeding on retry, not merely a
   low one. Retrying them is not a long shot — it is arithmetic that cannot
   come out.
2. Retry attempts are a **finite budget**. Razorpay halts a subscription after
   four consecutive failures. Every futile attempt spends a slot that a
   different intervention needed. Retrying is not free just because it costs no
   money.
3. Some failures make an automated response **actively harmful** rather than
   merely useless.

So the first question is never "when do we retry?" It is "what kind of failure
is this, and does retrying even have a mechanism by which it could work?"

---

## 2. The five classes

| Class | What it means | Retry the instrument? | Contact the customer? |
|---|---|---|---|
| `TRANSIENT` | Rail, bank, or gateway hiccup. Nothing is wrong with the customer or the instrument. | **Yes** — backoff | **No** |
| `LIQUIDITY` | The instrument is fine; the money isn't there *right now*. | **Yes** — time-shifted | Yes, one soft nudge |
| `INSTRUMENT_DEAD` | The payment method cannot succeed again, ever, in its current state. | **Never** | Yes — must obtain a new instrument |
| `CUSTOMER_ACTION` | A human has to do something: enter an OTP, fix a typo, complete a step. | **Never** blind-retry | Yes — send a re-attempt link |
| `RISK_BLOCK` | A fraud or risk engine declined this. | **Never** | **Never** — escalate to a human |

Two of these deserve their reasoning spelled out, because they are where the
system earns its keep.

### Why `TRANSIENT` gets no customer contact

Counterintuitive but important: when a gateway blips, the customer did nothing
wrong and often doesn't know anything failed. Messaging them converts an
invisible, self-healing problem into a visible one — and burns a contact against
`FrequencyCapGuard`'s cap, which we may need later for a real problem. Retry
silently. Escalate to contact only if the retries exhaust.

### Why `RISK_BLOCK` gets nothing at all

This is the sharpest rule in the taxonomy and the easiest one to get wrong.

A risk-check failure means someone's fraud system declined the transaction.
Automating a response is wrong on four independent grounds:

1. **Card network rules** generally restrict retrying a declined authorisation.
   Doing it programmatically at scale is worse than doing it once by hand.
2. **Merchant risk profile.** Decline ratio is an input to how the acquirer
   prices and monitors the merchant. Automated retries on risk declines degrade
   a number the merchant cannot easily repair.
3. **If it was actually fraud**, a retry loop is a tool for the fraudster, not
   the merchant.
4. **If it was a false positive**, an automated "your payment failed, click here
   to pay" message to a customer whose card may be compromised is structurally
   identical to a phishing attack. We would be training the customer to click
   payment links arriving unexpectedly.

Hard stop. Human queue. Zero outbound. This is the one class where doing nothing
is unambiguously the correct product decision.

---

## 3. Classification keys on `(error_reason, error_source)`

Not on `error_reason` alone. This is the most important structural decision in
the file, and it came out of testing rather than design.

**The finding.** Card failures in test mode return `error_source: gateway`. A
netbanking failure returned `error_source: bank` — same `error_reason`
(`payment_failed`), same `error_code`, same `error_step`. Recorded in
`docs/VERIFIED.md`.

`error_source` is therefore not a constant. It reflects where in the stack the
failure occurred, and it carries signal **even when `error_reason` carries
none.** For the generic `payment_failed` case — the only failure reproducible
live, and the one a real merchant sees most often — the source field is the
*only* discriminating information available.

Two caveats, both from the same testing:

- Source is **noisy**, not clean. A later attempt on the netbanking rail also
  returned `gateway`. Source narrows the hypothesis space; it doesn't determine
  the answer.
- Source is only load-bearing for uninformative reasons. Where the reason is
  specific (`card_expired`, `insufficient_fund`), it already determines the
  class and source adds nothing.

So: **the lookup is keyed on the pair, but only the generic reason branches on
source.** Everything else ignores it.

---

## 4. The mapping

`payment_failed` is the only reason reproducible against live test mode.
Everything else is exercised through `data/stubbed_payloads/`.

| `error_reason` | `error_source` | Class | Intervention | Timing |
|---|---|---|---|---|
| `payment_failed` | `gateway` | `TRANSIENT` → escalate | `SILENT_RETRY` ×1 → `REATTEMPT_LINK` | 15m, then nudge |
| `payment_failed` | `bank` | `INSTRUMENT_DEAD` | `SILENT_RETRY` ×1 → `REAUTH_LINK` | +6h, then link |
| `payment_failed` | *other* | `TRANSIENT` → escalate | `SILENT_RETRY` ×1 → `HUMAN_QUEUE` | 30m, then queue |
| `gateway_technical_error` | — | `TRANSIENT` | `SILENT_RETRY` ×3 | 5m → 30m → 4h |
| `payment_timed_out` | — | `TRANSIENT` → escalate | `SILENT_RETRY` ×1 → `REATTEMPT_LINK` | 10m, then nudge |
| `insufficient_fund` | — | `LIQUIDITY` | `TIMED_RETRY` + soft nudge | Salary-aware (§6) |
| `payment_cancelled` | — | `CUSTOMER_ACTION` | `REATTEMPT_LINK` ×1 | +2h, in-window |
| `card_declined` | — | `INSTRUMENT_DEAD` | `SILENT_RETRY` ×1 → `REAUTH_LINK` | +6h, then link |
| `card_disabled_for_online_payments` | — | `INSTRUMENT_DEAD` | `REAUTH_LINK` + explain | Immediate, in-window |
| `card_number_invalid` | — | `CUSTOMER_ACTION` | `REATTEMPT_LINK` | Immediate, in-window |
| `card_expired` | — | `INSTRUMENT_DEAD` | `REAUTH_LINK` | Immediate, in-window |
| `payment_risk_check_failed` | — | `RISK_BLOCK` | `HUMAN_QUEUE` | Never automated |
| *unmapped* | — | `TRANSIENT` (fail-safe) | `SILENT_RETRY` ×1 → `HUMAN_QUEUE` | 30m, then queue |

`—` means source is ignored for that reason.

---

## 5. Reasoning, reason by reason

### `payment_failed` — the only one reproducible live

Generic. No information about *why* the payment failed, because the mock bank
page fails the transaction and overwrites whatever the card encoded
(`docs/VERIFIED.md`).

This is the interesting case, not the boring one. An unclassifiable failure is
the normal condition in production, not an edge case — real gateways return
generic errors constantly.

**`payment_failed` + `gateway`** — the rail failed on our side. Classified
`TRANSIENT`. **One** silent retry, then a customer re-attempt link.

One, not three. An uninformative error deserves *less* budget than a specific
one, not more. With `gateway_technical_error` the failure is explicitly a
gateway problem and three retries are justified by knowing what's wrong. Here we
have a weak prior and nothing else. Spending three of four attempts on a
hypothesis this thin is how the budget gets wasted — one attempt tests it, and
if that fails, hand the decision to the customer, who has information we don't.

**`payment_failed` + `bank`** — the issuer declined. A different situation
entirely: the failure is downstream of us, at the institution holding the money.
This behaves like `card_declined` and is classified the same way — one retry
after six hours to cover a soft decline, then treat the instrument as dead.

**`payment_failed` + anything else** — an unfamiliar source on an uninformative
reason. One silent retry (least harmful possible action), then a human. Log the
source value; an unfamiliar source is itself operational signal.

### `gateway_technical_error` — the clean transient

Explicitly a gateway problem. Nothing about the customer or the instrument is
implicated. Gateway blips clear in minutes. Three retries on exponential
backoff, no customer contact — the customer likely never noticed.

The contrast with `payment_failed/gateway` is deliberate: same class, three
times the budget, because here we actually know what broke.

### `payment_timed_out` — ambiguous by construction

A timeout means we don't know what happened. The authorisation may have
succeeded and the response been lost. The customer may have walked away
mid-flow. The bank may be slow.

Two consequences:

1. **Retry exactly once**, then stop. Repeatedly retrying a transaction whose
   outcome is unknown is how double-charges happen.
2. **Watch for out-of-band success.** If `payment.captured` or `order.paid`
   arrives for the same order, cancel any pending recovery immediately. This is
   adversary attack A07, and it's the failure mode I'd most fear in production.

### `insufficient_fund` — the highest-value class

Note the singular. Razorpay emits `insufficient_fund`, not `insufficient_funds`.
`normalise()` aliases it. Trusting the plural would silently route every one of
these to the unknown path — a whole class of the most recoverable failures, lost
to a typo.

The instrument works. The customer intends to pay. The money isn't there
*today*. This is the most recoverable failure in the taxonomy, and timing does
the work rather than persistence — see §6.

One soft nudge is warranted, unlike other transients, because the customer can
act (move money, use another account) and probably wants to.

### `payment_cancelled` — the customer said no

Classified `CUSTOMER_ACTION`, not `INSTRUMENT_DEAD` — nothing is broken. But
this is the one reason where the customer expressed something close to intent,
and it deserves restraint.

**Exactly one** re-attempt link, delayed two hours.

Not zero, because accidental cancellation is common — wrong button, app switch,
interrupted flow — and a single low-pressure reminder recovers those without
harming anyone. Not two, because one is a reminder and two is pressure. And not
immediate, because someone who just cancelled and instantly receives "complete
your payment" experiences that as being chased, which is precisely the conduct
the RBI Fair Practices Code exists to prevent.

The two-hour delay is the whole design here: it converts a nudge that feels like
surveillance into one that feels like a service.

### `card_declined` — the honestly ambiguous one

Issuer-side decline with no stated reason. Some issuers use this for soft,
recoverable declines; others for hard ones. The data does not distinguish them.

**One retry after six hours, then treat as `INSTRUMENT_DEAD`.** The single retry
costs one slot and covers the soft-decline case. If it fails again, two
consecutive issuer declines is strong evidence the instrument is the problem, so
stop retrying and ask for a new one.

This row is the least certain in the file. Two consecutive declines is a
heuristic, not a fact, and a merchant with real decline data could tune it.

### `card_disabled_for_online_payments` — dead, but fixable by the customer

Indian banks commonly ship cards with online payments switched off, and
customers enable it per-card in their banking app. So the instrument is dead
*right now*, but the customer can revive it in about a minute — if they know
that's the problem.

This makes message content matter more than mechanism. "Payment failed, try
again" is useless here. "Your bank has online payments disabled for this card —
enable it in your banking app, or pay another way" is actionable, and it is the
difference between a recovered payment and an annoyed customer.

Never retry. The card declines identically every time until the customer changes
a setting we cannot see or touch.

### `card_number_invalid` — a typo

`CUSTOMER_ACTION`. Retrying the same wrong digits produces the same failure
forever. Re-attempt link immediately, within the contact window. No explanation
needed beyond "check the card number."

### `card_expired` — the flagship zero

The clearest case in the taxonomy.

An expired card has a **zero percent** chance of succeeding. Not low — zero.
There is no state of the world in which the same expired card authorises on the
third attempt. A retry here has *exactly zero expected value* while consuming
exactly one of the four attempts before Razorpay halts the subscription.

That is the argument for the whole taxonomy in one row: retrying isn't free.
It's paid for out of a budget the re-auth link needed.

### `payment_risk_check_failed` — the one that gets nothing

Reasoning in §2. Hard stop, human queue, zero outbound. The system's most
important action here is to do nothing, deliberately and traceably.

Worth stating explicitly: this is the only path where the agent's correct
behaviour is indistinguishable from the agent being broken. That's why it writes
a receipt recording the decision *not* to act — an audit trail needs to show
restraint, not just action.

### Unmapped reasons — fail safe, and loudly

A reason we've never seen. Razorpay's error list is longer than what we've
observed, and it will change.

**Classify as `TRANSIENT`, one silent retry, then `HUMAN_QUEUE`.** A single
silent retry is the least harmful possible action — no customer contact, no
assumption about the instrument, minimal budget spent. If it fails, a human
decides.

Log at `WARN` with the full reason string. Unknown reasons should surface as
operational signal rather than vanishing into a default branch. The unknown
bucket filling up is how we learn the API changed.

---

## 6. Retry timing

### Salary-aware retries for `LIQUIDITY`

Fixed exponential backoff is wrong for insufficient funds, because the
constraint isn't system state — it's the customer's bank balance, and that
follows a monthly cycle.

Indian salary credit clusters on the **last working day** and the **1st–7th**.
Retrying an insufficient-funds failure on the 25th is worse than useless: it
consumes one of four attempts at the point in the month when the balance is
least likely to cover it.

```
attempt 1:  now + 48h                 # cheap, covers short-term timing
attempt 2:  next salary window        # unless >12 days out, then +5 days
attempt 3:  next salary window + 6h   # let the credit settle
```

**Never retry between 00:00 and 06:00 IST.** Some issuers run batch maintenance
overnight, producing a spurious technical failure that consumes an attempt for
reasons unrelated to the customer.

### Backoff for `TRANSIENT`

`5m → 30m → 4h`. Gateway problems usually clear in minutes; if four hours hasn't
fixed it, more retries won't either.

---

## 7. Stopping rules

| Rule | Value | Source |
|---|---|---|
| Max attempts, mandate/subscription | **4**, then halted | Razorpay documentation |
| Max attempts, one-time payment | 3 | Self-imposed |
| Max contacts per customer per 7 days | 3 | Anti-harassment, RBI FPC |
| Max contacts per recovery episode | 2 | Self-imposed |
| Contacts after `payment_cancelled` | 1 | §5 |
| Hard stop on `RISK_BLOCK` | Immediate | §2 |
| Hard stop on out-of-band success | Immediate | A07 |
| Hard stop on consent withdrawal | Immediate, purge queue | DPDP |
| Hard stop on refund issued | Immediate | Don't chase refunded money |

The 4-retry halt is documented rather than observed — subscriptions are
unavailable pre-activation, so it could not be verified on this account
(`docs/VERIFIED.md`).

---

## 8. What the LLM is and isn't for

Every row in §4 is a **deterministic lookup**. `(reason, source)` → class →
intervention is a dictionary, not a judgment call, and a dictionary is faster,
cheaper, auditable, and cannot hallucinate a class that doesn't exist.

So the rules classifier owns the table. The LLM's candidate roles are:

- **Ambiguity resolution** where structured fields are uninformative —
  specifically the `payment_failed` branches, where free-text merchant notes or
  prior interaction history might carry signal the enum doesn't.
- **Outreach copy**, where tone and clarity matter and there is no lookup table.
  The `card_disabled_for_online_payments` message is the clearest example: the
  intervention is trivial, the wording decides whether it works.

Session 7 measures whether the LLM actually beats the rules baseline on the
first of those. **If it doesn't, ship the rules and publish the comparison.**
The measurement is the deliverable, not the LLM.

---

## 9. Known limits

Listing these is more useful than pretending otherwise.

1. **`card_declined` soft-vs-hard is a heuristic.** One retry then dead. A
   merchant with real decline data could tune it; we can't.
2. **`error_source` is noisy.** One netbanking failure returned `bank`, a later
   one returned `gateway`. The source-based branch narrows the hypothesis space
   rather than determining the answer.
3. **Contact-window timezone.** Currently merchant TZ, not customer TZ. This is
   adversary attack A05 and is a known open failure.
4. **The 4-retry halt is documented, never observed** on this account.
5. **UPI is unavailable pre-activation**, so the taxonomy is card-shaped by
   necessity. A UPI-heavy merchant would need different rows, and inventing them
   without a single observed UPI payload would be speculation.
6. **Nine of ten reasons are documentation-derived**, not captured. The
   envelopes are real; the four error fields are set from Razorpay's error-code
   docs. `tools/make_stubs.py` records which is which.

---

## 10. The five sentences

If someone remembers nothing else about this file:

1. An expired card has a zero percent chance of succeeding, and every futile
   retry burns one of the four attempts you get.
2. A risk-declined payment gets no automated action at all — retrying it helps
   fraud, and messaging the customer is indistinguishable from phishing.
3. When the reason is uninformative, `error_source` is the only signal left:
   `payment_failed/gateway` is our rail, `payment_failed/bank` is the issuer,
   and they deserve different interventions.
4. Insufficient funds is a timing problem, not a persistence problem — retry on
   payday, not on backoff.
5. Unknown reasons fail safe to one silent retry and a human, logged loudly,
   because the unknown bucket filling up is how you learn the API changed.