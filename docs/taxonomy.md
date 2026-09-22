# Failure taxonomy

How Vasool classifies a failed payment, and what it does about each class.

This is the intellectual core of the system. Everything downstream — the policy
state machine, the guards, the evaluation — is mechanical once this is right.
Get this wrong and a correct implementation still recovers nothing.

**Provenance.** Every fact here carries one of three tiers
(`vasool/events/provenance.py`): **observed**, captured live in
`data/observed_payloads/`; **cited**, published by the operator of a rail, in
`data/cited_payloads/`; or **simulated**, hand-built from documentation in
`data/stubbed_payloads/` and marked `_SIMULATED: true`. Every Razorpay
`error_reason` below has a payload in the first or the last — per `the project
rules`, a reason in neither does not exist — and see `docs/VERIFIED.md` for why
only `payment_failed` is reproducible live. The cited tier holds NPCI's UPI
codes, which §11 maps.

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
| `INSTRUMENT_DEAD` | The payment method cannot succeed again, ever, in its current state. | **Never**, beyond a single soft-decline probe | Yes — must obtain a new instrument |
| `CUSTOMER_ACTION` | A human has to do something: enter an OTP, fix a typo, complete a step. | **Never** blind-retry | Yes — send a re-attempt link |
| `RISK_BLOCK` | A fraud or risk engine declined this. | **Never** | **Never** — escalate to a human |

The `INSTRUMENT_DEAD` probe is the single exception in that column, and it is
narrow: some issuers return a generic decline for a *soft*, recoverable failure
and others return the same string for a hard one, and exactly one attempt is
what distinguishes them. One probe, never a ladder — see `card_declined` in §5.

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
| `payment_failed` | `business` | `RISK_BLOCK` | `HUMAN_QUEUE` | Never automated |
| `payment_failed` | *other* | `TRANSIENT` → escalate | `SILENT_RETRY` ×1 → `HUMAN_QUEUE` | 30m, then queue |
| `gateway_technical_error` | — | `TRANSIENT` → escalate | `SILENT_RETRY` ×3 → `REATTEMPT_LINK` | 5m → 30m → 4h, then nudge |
| `payment_timed_out` | — | `TRANSIENT` → escalate | `SILENT_RETRY` ×1 → `REATTEMPT_LINK` | 10m, then nudge |
| `insufficient_fund` | — | `LIQUIDITY` → escalate | `TIMED_RETRY` ×3 + soft nudge → `REATTEMPT_LINK` | Salary-aware (§6), then link |
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

**`payment_failed` + `business`** — routed to `RISK_BLOCK` and a human queue.
The argument for this row is precautionary, not evidential, and it matters to
state it correctly because the obvious version of it is circular.

Our only risk-decline payload carries `error_source: business`. But that value
was *hand-set* by `tools/make_stubs.py` from Razorpay's documentation — it was
never observed. So "`business` means a risk decline" is not a finding. It is our
own stub read back to us, and offering it as evidence would be arguing in a
circle.

The argument that does hold is about asymmetry, not meaning. Suppose `business`
merely *might* indicate a risk decline. If we route it to a human and we are
wrong, the cost is one recoverable failure that waited for an operator. If we
retry it automatically and we are wrong, the cost is an automated
re-presentation of a declined authorisation — the exact hazard §2 rules out on
four independent grounds, one of which is that we would be handing a tool to a
fraudster. Those two costs are not the same size, and nothing about the
probability needs to be known to see that.

So the row is correct on expected harm even though our evidence for what
`business` means is worth nothing. **The asymmetry is the argument; the payload
is not.** If a live `business`-sourced failure is ever captured and turns out to
be benign, this row costs us a queue entry — that is the price, and it is the
cheap side of the trade.

**`payment_failed` + anything else** — an unfamiliar source on an uninformative
reason. One silent retry (least harmful possible action), then a human. Log the
source value; an unfamiliar source is itself operational signal.

### `gateway_technical_error` — the clean transient

Explicitly a gateway problem. Nothing about the customer or the instrument is
implicated. Gateway blips clear in minutes. Three retries on exponential
backoff, no customer contact while they run — the customer likely never noticed.

The contrast with `payment_failed/gateway` is deliberate: same class, three
times the budget, because here we actually know what broke.

**Then a re-attempt link.** Three retries and then nothing is a broken product.
If four hours of backoff hasn't cleared it, the self-healing-blip hypothesis is
dead, and someone whose payment keeps failing should be asked to pay another way
rather than dropped in silence. This is also what §2 already said — a
`TRANSIENT` escalates to contact once its retries exhaust — so the row now
agrees with the class table instead of contradicting it.

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

After three timed attempts, a re-attempt link — same reasoning as
`gateway_technical_error`. Three failures spanning two paydays means the timing
hypothesis has been tested and lost, and the customer should be offered another
method rather than left waiting on a fourth attempt that isn't coming. That is
two contacts in the episode, the nudge and the link, which is exactly the
per-episode cap in §7 rather than a step past it.

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
attempt 3:  next salary window + 6h   # unless >20 days out, then +5 days
```

**Attempt 3 is capped too, and the bound is asymmetric because the attempts
are.** Attempt 2 has a third attempt behind it, so waiting for payday is cheap
and twelve days is the right amount of patience. Attempt 3 is the last one —
landing it near money matters more than landing it soon — so it gets twenty days
before falling back.

Uncapped, attempt 3 produces a month-long gap, and not hypothetically. A failure
on 25 August schedules attempt 2 for the 31st, the last working day, correctly.
Attempt 2 fails there, and from the 31st the next salary window is 30 September
— so attempt 3 would land *thirty days* after attempt 2. A subscription customer
who hears nothing for a month has already churned; there is no balance to catch
on day 30, because the relationship ended around day 6.

Twenty days rather than twelve, because twelve removes that gap by flattening
the ladder instead. The two trajectories the bound has to satisfy:

```
failure 25 Aug (late month):  27 Aug → 31 Aug →  5 Sep
failure  8 Sep (mid month) :  10 Sep → 15 Sep → 30 Sep
```

The late-month episode loses its month-long gap: attempt 3 moves off 30
September onto the 5th, which is still inside the 1st–7th window, so nothing is
given up to gain it. The mid-month episode keeps its payday landing: attempt 2
falls back to the 15th because payday is twenty days out, and attempt 3 — now
only fifteen days from the window — waits for it. Under a twelve-day bound that
last attempt would fire on the 20th instead and the episode would never touch a
salary window at all, which is the one thing this section exists to prevent.

### Backoff for `TRANSIENT`

`5m → 30m → 4h`. Gateway problems usually clear in minutes; if four hours hasn't
fixed it, more retries won't either.

### The quiet period — two rules, not one

00:00–06:00 IST is excluded, but the two things we schedule are excluded for
different reasons and with different force. Writing it as a single rule hides
that one half is far better justified than the other.

**Outbound contact: never, absolutely.** A message at 2am is harassment whatever
it says. This is not a tuning parameter, it does not trade off against recovery
rate, and it holds for every class and every attempt. It is the half that would
survive an RBI Fair Practices Code argument on its own terms.

**Silent retries: also held, but for a weaker reason.** The justification is that
some issuers run batch maintenance overnight and return a spurious technical
failure that consumes an attempt for reasons unrelated to the customer. That is
a claim about issuer behaviour we have not verified (§9). We apply it anyway
because the cost is small and one-sided: holding a 5-minute gateway retry until
06:00 costs about four hours of recovery latency and disturbs nobody, since
nothing is sent. So it stands as a cheap hedge against an unverified claim
rather than as a principle — and it is the first thing to relax if that latency
ever turns out to matter.

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
   rather than determining the answer. Only `gateway` and `bank` have ever been
   observed on a `payment_failed`; the `business` branch is inferred, not seen.
3. **Contact-window timezone. Registered as open; fixed 2026-08-30.** The
   window was evaluated in the merchant's IST, so a customer elsewhere was
   protected by our clock rather than their own — adversary attack A05/A08 put a
   message at 22:30 customer-local by deferring an 03:00 IST failure to the
   opening of *our* window. `PolicyFacts.customer_zone` now carries the fact and
   `ContactWindowGuard` defers into the customer's own window, falling back to
   IST where no zone is known.

   Three things are worth recording about the closure rather than just the fix.
   **The suite went red when it was fixed** — a registered expectation of FAILS
   makes a repair break the build exactly as a regression does, which is the
   property that stops this list rotting. **Then it went red for a better
   reason:** the fix made A08 pass with zero receipts, because the message was
   deferred past the end of the scene, and a pass with nothing in the ledger is
   vacuous; the scene's horizon was extended until the contact actually lands.
   **And no evaluated number moved** — no universe customer carries a zone, so
   the fallback is IST and behaviour is unchanged, verified by recomputing 54
   (arm, seed) rows across all nine arms, 1,350 field comparisons, byte-identical
   to the shards. Recorded in `EVALUATION.md` §10.
4. **The 4-retry halt is documented, never observed** on this account.
5. **UPI is unavailable pre-activation**, so the taxonomy is card-shaped by
   necessity. A UPI-heavy merchant would need different rows, and inventing them
   without a single observed UPI payload would be speculation.
6. **Nine of ten reasons are documentation-derived**, not captured. The
   envelopes are real; the four error fields are set from Razorpay's error-code
   docs. `tools/make_stubs.py` records which is which.
7. **The `payment_failed`/`business` → `RISK_BLOCK` row is precautionary, not
   evidence-backed.** The `business` value on our risk stub was hand-set from
   documentation and has never been observed live. The row is justified by the
   cost asymmetry in §5, not by knowing what `business` means. If it turns out
   to be benign, this row sends recoverable failures to a human queue. One
   captured live payload would settle it either way.
8. **The overnight batch-maintenance claim is unverified.** The retry half of
   the quiet-period rule (§6) rests on issuers running maintenance windows that
   produce spurious failures. We have never observed one. It is kept because it
   is nearly free, not because it is proven. The contact half needs no such
   defence and does not depend on it.
9. **Out-of-band settlement detection is attribution-limited, not
   architecture-limited.** The state machine's own A07 handling (§7's hard
   stop) is unconditionally correct once `PolicyMachine.settled()` is called —
   the gap, such as it is, sits entirely upstream of that call, in which real
   settlement events the receiver can actually recognise. Two paths are
   wired: a REAUTH_LINK/REATTEMPT_LINK paid through the link this agent sent
   (`payment_link.paid`, correlated via the `notes.vasool_entity_id` it
   tagged), and a SILENT_RETRY/TIMED_RETRY captured as the same payment
   `createRecurring` created (`payment.captured`, correlated via the
   executor's own RetryIndex — process-local, so a restart between the retry
   firing and its capture arriving loses that particular join). **Not
   caught:** a link-intervention episode paid out-of-band through any *other*
   channel — a customer who pays directly rather than through the link we
   sent carries no `vasool_entity_id` anywhere and is invisible to us,
   indistinguishable from any other payment on the account. Also not caught,
   ever: `order.paid`, which stays unwired for want of any attributable field
   at all (docs/VERIFIED.md). See VERIFIED.md's two DECISION entries on
   settlement for the mechanism behind each wired path.

10. **§7's "hard stop on out-of-band success" can never fire for a genuinely
    out-of-band payment.** The rule is right and the state machine honours it
    the instant `settled()` is called — but for the case the rule is named
    after, nothing ever calls it. A customer who pays through some other
    channel produces a `payment.captured` carrying no `vasool_entity_id` and
    no RetryIndex entry, indistinguishable from any other payment on the
    account (§9.9), so the receiver correctly declines to attribute it and the
    episode stays open. What §7 actually stops is the *attributable* cases: a
    link we sent being paid, our own retry capturing, a refund. Those are
    worth stopping, and they are not the case the row's name evokes.

    The consequence is not a missed recovery. It is that the agent goes on
    chasing money the merchant already has — a double-collection hazard rather
    than a lost-revenue one, which is the more expensive direction to be wrong
    in. This is also why `EVALUATION.md` §4's out-of-band parameter produces no
    recovery in the evaluation: `windtunnel/` measures the exposure directly,
    as how often an action is taken on an episode after its money had already
    arrived, and reports it as a safety number. Out-of-band money is never
    counted as recovered in any arm, so every arm is undercounted by the same
    mechanism and the paired comparisons are unaffected.

    **Closed on 2026-09-21, and only for a deployment that opts in**
    (`EVALUATION.md` §10). Nothing above stops being true — the webhook still
    carries no join key and the receiver still correctly declines to attribute
    it. What changed is that the agent can now *ask*: `vasool/actions/
    reconcile.py` is a port whose implementation reports what the rail captured
    for this customer since the failure, the agent subtracts every payment id
    it originated, and an amount match inside the registered window stops the
    episode and hands it to a person. It is never recorded as recovered,
    because an amount match is evidence and not a join key — a second purchase
    at the same price collides with it exactly, and §9.9's whole point is that
    matching on amount and customer is what this system refuses to do for
    *attribution*. Stopping is a weaker claim than settling, and it is the one
    the evidence supports.

    Two limits travel with the fix. The port's default adapter answers nothing,
    so a deployment that has not wired a real lookup behaves exactly as this
    entry describes — that is attack **A27**, registered FAILS. And in the
    evaluated universe every out-of-band payment is for one episode's own
    amount, drawn from a continuous distribution, so the collision the design
    accepts never actually occurs there: 201 of 201 halts across five seeds
    were on episodes that genuinely had been paid. That is a property of the
    universe, not evidence about a real book.

    Closing this needs an attributable signal that does not exist today. The
    honest options are a merchant-side reconciliation feed, or correlating on
    the original `order_id` — which §9.9 rules out as a guessed join key, and
    which would miss a payment made against a new order anyway. Recorded as an
    open failure rather than designed around.

    **The window is wide rather than narrow, and that is measurable.** Every
    row above escalates to a link, and a link nobody clicks produces no
    signal at all — so an episode that runs its ladder out rests in AWAITING
    indefinitely rather than terminating. On seed 0 that is 304 of 888
    episodes, the single largest resting state, every one of them having
    executed exactly the retries §4 permits and then sent §4's link. An
    episode still open is an episode still exposed, so the double-collection
    hazard above is not a brief window between an out-of-band payment and a
    terminal state; for a link-intervention episode it runs to the horizon.

11. **`EXHAUSTED` is unreachable through the rules classifier, and that is a
    structural fact rather than a finding.** The state exists for the case §4
    contemplates in its last column — the retry budget is spent and the row
    names no escalation — and no row is actually like that: every one of them
    escalates, to a link or to a human. So `proposals_from` never returns
    empty for a rules-classified diagnosis, and the report card will show 0
    beside `EXHAUSTED` on every seed. Worth stating because a reader is
    entitled to know whether a zero means "never happened" or "cannot
    happen", and here it is the second. It becomes reachable the day a row
    ends without an escalation, or the day an LLM classifier (§8) proposes
    one that does.

12. **A queued proposal outlives the diagnosis that built it, and a deferral
    carries an action past the moment its reasoning was checked.** One defect,
    two halves, four demonstrations.

    The policy plane re-reads the *world* on every gate — consent, contacts
    already sent, whether the payment settled — and that is the right design,
    argued at length in `vasool/policy/machine.py`. What it never re-reads is
    the *classification*. The machine's own docstring says re-running
    `classify()` on wake would be vacuous, and for the case it addresses it is
    correct: `classify` is a pure function of (event, attempt), and neither
    changes while one action waits. The case it does not address is a
    *different* event arriving for the same episode. That mints a new proposal
    from the new row and retires nothing — the proposal the old row scheduled
    is still on the queue, still carrying the old row's `failure_class`, and it
    gates on its own terms when its time comes.

    The second half is the same fact reached from the other direction. A guard
    that defers moves an action to an instant at which nobody re-checks the
    classify-time reasoning, because nothing downstream re-applies it. §6's
    quiet-hours hold on retries is applied in `vasool/diagnosis/rules.py`,
    once, at classify time; a deferral steps over it and nothing puts it back.

    **This pattern has been fixed once already, one plane up.** `rules.py`'s
    `QUIET_HOURS_END_HOUR_IST` docstring records it: the 00:00–06:00 hold used
    to be applied to `HUMAN_QUEUE` as well, so a risk-declined payment arriving
    at 02:00 IST sat until 06:00 before reaching an operator queue — a hold
    applied at classify time to a path no rule governed. The fix was to move
    the rule to the plane that owns it and enforce it where the action actually
    happens. Every demonstration below is asking for that same move, and none
    of them has had it.

    Four attacks, from `windtunnel/adversary/attacks.py`:

    - **A15** — a risk decline arrives at the instant a queued retry falls
      due. The retry was built from the earlier, benign row and carries
      `failure_class: TRANSIENT`; `RiskBlockGuard.applies_to` keys on the
      proposal's class rather than the episode's, so it has no jurisdiction and
      the re-presentation goes out on a risk-declined payment. Arrival order
      decides it: the same decline a minute earlier escalates the episode
      first, and the queued retry is then dropped at its gate.
    - **A16** — the card expires between attempt 2 and 3 (design spec §9's
      A06). The re-auth link the new row calls for goes out, and thirty minutes
      later the stale `SILENT_RETRY` re-presents the expired card. §5's
      flagship zero, spent anyway.
    - **A18** — a promise to pay defers a re-presentation to exactly 00:00 IST,
      inside §6's quiet period. `PromiseToPayGuard` defers to midnight of the
      day after the promised date and has no `applies_to`, so it governs silent
      retries as well as messages, and nothing re-applies the classifier's
      hold. Nothing is sent to anyone, so this costs efficacy rather than
      dignity — but §6 states the rule for both halves, and this is the half
      that is enforced nowhere.
    - **A19** — the same guard defers a `HUMAN_QUEUE` handoff, because DEFER
      outranks the ALLOW `RiskBlockGuard` returns for one. §7 lists the
      RISK_BLOCK stop as immediate; the measured delay on the attack is 1 day
      14 hours. This is `rules.py`'s already-fixed bug, resurrected in the
      policy plane.

    **The ledger scan cannot see the first two.** `EVALUATION.md` §2a's two
    class-keyed claims — no automated action on a `RISK_BLOCK` episode, no
    retry on an `INSTRUMENT_DEAD` classification beyond the single probe — both
    key on `Proposal.failure_class`, which on a stale proposal is the *old*
    label. So A15 and A16 execute exactly the actions those two rows forbid and
    both rows still pass. §10 of that document already records the class-keyed
    scans' blind spot for an arm that declines to classify; this is the same
    blind spot reached with every arm classifying correctly. Catching it needs
    a scan keyed on the episode, which is what the adversary's own evidence
    does and what §2a does not.

    **Fixed 2026-08-25.** `PolicyMachine.observe()` now retires queued
    proposals when a later failure changes the reason or source that produced
    them; identical duplicate deliveries remain available to the idempotency
    path. The transition log records each supersession. `SpendCapGuard` performs the
    retry quiet-hours check at final gating, so a promise cannot release a
    retry at midnight. `PromiseToPayGuard` has no jurisdiction over
    `HUMAN_QUEUE`, so a risk handoff is immediate. A15, A16, A18 and A19 are
    registered as surviving controls and exercised by the red-team suite.

    **Verified 2026-08-29**, and not before: the sentence above was written on
    the strength of the fixes existing, and the suite that would test them had
    not been run on record. It has now — 18 of 22 survived at that date, all four of these
    among them — and the closure is recorded in `EVALUATION.md` §10 rather than
    here, because a status this paragraph asserts about itself is exactly the
    kind of claim `windtunnel/adversary/criterion.py` exists to refuse.

13. **The pre-debit notice was never sent, so no mandate debit ever executed.
    This was a liveness failure, the only one in this section, and it is fixed
    — the account below is kept because it shaped every evaluation number
    published before 2026-08-25.**

    Every limit above is about safety: something the agent might wrongly do, or
    evidence it cannot produce. This one was the opposite, and belongs in the
    list anyway — the agent correctly refused, forever, an action it was
    supposed to take.

    The loop was closed on itself. `PreDebitNoticeGuard` holds a mandate debit
    until a notice has been served, and returns `DEFER` carrying an
    `Obligation(SEND_PRE_DEBIT_NOTICE)`. `PolicyMachine._execute` was the only
    place obligations were read. A deferred proposal does not execute, so no
    notice proposal was ever built, so `pre_debit_notice_sent_at` stayed None,
    so the guard deferred again — five times, and then `MAX_DEFERRALS` blocked
    it. The one thing that could satisfy the guard was an execution the guard
    was blocking.

    **Measured before the fix, seed 0, full Vasool:** 888 episodes, of which
    275 are on a mandate. 707 retries executed across the run; **zero** of them
    on a mandate episode. 209 of the 275 mandate episodes ended in `BLOCKED`.
    That is 31% of the population whose retry ladder never fired at all — not a
    tail case, and not something the report card distinguished from a
    compliance save.

    **It had been shaping every evaluation number up to that point, including
    the recovery comparison.** `mandate_share` is 0.35
    (`windtunnel/parameters.py`, registered under `EVALUATION.md` §10), so
    roughly a third of every arm's population is affected and every absolute
    recovery figure is computed over it. The paired comparisons were not
    uniformly protected either, because the guard chain is exactly what differs
    between some arms: `vasool_ungated` runs with `chain=()`, so
    `PreDebitNoticeGuard` is not there and its mandate retries did fire. On
    seed 0 that arm executed 1050 retries to full Vasool's 707, and **306 of
    the 343-retry gap were mandate retries** — 89% of it. F5 is registered
    against `vasool_ungated` as the price of the guards, at 20 absolute
    percentage points.

    **Fixed 2026-08-25.** Obligations are honoured on the deferral path.
    `PolicyMachine._defer` calls `_honour`, which builds the notice proposal
    the guard asked for; the dead loop in `_execute` is gone, and its docstring
    now records why no obligation can reach it. The notice is a `Proposal` like
    any other and is gated like any other — contact window, DND scrub, DLT
    template and frequency cap all apply to it — and `_honour` runs *after*
    `_defer`'s `MAX_DEFERRALS` and `DEFER_HORIZON` bounds, so a debit we have
    just declined to reschedule does not warn a customer about a debit that is
    not coming.

    **Measured after the fix, seed 0, full Vasool**, against the same run
    above:

    | | before | after |
    |---|---|---|
    | pre-debit notices executed | 0 | **196** |
    | retries executed | 707 | **979** |
    | …of them on a mandate episode | 0 | **272** |
    | mandate episodes ending `BLOCKED` | 209 / 275 | **30 / 275** |

    The 196 notices land on 196 distinct mandate episodes, which is the shape
    the guard describes: one notice per episode, then the debit.
    `vasool_ungated` is unmoved at 1050 retries and 306 mandate retries, as it
    must be — it carries no guard chain, so there was nothing there to fix. The
    seed-0 retry gap between the two arms is now 71 rather than 343, and 34 of
    it is mandate rather than 306, so the arm difference is once again mostly
    the guards rather than mostly this defect.

    **What it moved in the evaluation**, recorded in full in `EVALUATION.md`
    §10's rows of 2026-08-25: Vasool's mean recovery rate 0.344341 → 0.490698,
    F5's gap 19.378 → 4.742 absolute percentage points against a threshold of
    20 — so about three quarters of the measured "price of the guards" was this
    defect and not compliance — and F1 against `retry_plus_contact` −0.310 →
    −0.164. `naive_retry` went −0.122 → +0.025, a flipped conclusion. The three
    arms that did not move are exactly the three with `chain=()`. Every figure
    now in `out/development/` is post-fix; §10 also records the stale-shard
    incident from the first re-run, when a resumed `make eval` re-emitted
    pre-fix rows in 5.5 seconds because a shard carries no fingerprint of the
    agent that produced it.

---

## 10. The five sentences

If someone remembers nothing else about this file:

1. An expired card has a zero percent chance of succeeding, and every futile
   retry burns one of the four attempts you get.
2. A risk-declined payment gets no automated action at all — retrying it helps
   fraud, and messaging the customer is indistinguishable from phishing.
3. When the reason is uninformative, `error_source` is the only signal left:
   `payment_failed/gateway` is our rail, `payment_failed/bank` is the issuer,
   and `payment_failed/business` might be a risk engine — that last one goes to
   a human not because we know what it means, but because being wrong about it
   is far more expensive in one direction than the other.
4. Insufficient funds is a timing problem, not a persistence problem — retry on
   payday, not on backoff.
5. Unknown reasons fail safe to one silent retry and a human, logged loudly,
   because the unknown bucket filling up is how you learn the API changed.

---

## 11. UPI: NPCI's vocabulary, mapped

Everything above classifies Razorpay's failure reasons. UPI had no vocabulary
here at all, and the mandate rail (§2.5 of the programme) cannot be built
without one. This section is that vocabulary and the judgement applied to it.

**Provenance: `CITED`, a third tier.** The codes are NPCI's, transcribed
verbatim into `data/cited_payloads/` from *Unified Payments Interface — Error
and Response Codes*, version 2.9 (17 January 2024), a document NPCI marks
"Public" on every page. Three sections, 225 codes: **§3.1**, the codes a
remitter or beneficiary bank returns on a debit or credit, mandate debits
included (printed pages 6–11); **§4.1**, the codes UPI itself returns on a
timeout (page 23); and **§4.4**, errors from the UPI service layer (pages
59–64). Those are the codes that end a payment. The rest of the document —
reversals, meta APIs, mandate registration, message-level validation, UIDAI —
describes other messages, and §2.5 extends the vocabulary when it needs them,
with its own amendment. No copy hosted by NPCI was found, so every file pins
the SHA-256 of the bytes it transcribes (`93584968…`), and
`tools/cite_npci.py` re-derives the files from any copy with that hash.
Registered in `docs/EVALUATION.md` §10, 2026-09-15.

**The mapping is this project's judgement**, in `vasool/diagnosis/npci.py`,
with every code's reason in the table below. The rules are that module's
docstring. The line between `CUSTOMER_ACTION` and `INSTRUMENT_DEAD` is the one
§4 already draws between `card_number_invalid` and
`card_disabled_for_online_payments`. And the mapping is anchored to NPCI's own
TD/BD column: no code marked TD lands in a class that blames the customer or
the instrument, and no code marked BD lands in `TRANSIENT` — both tested.

**It reaches a merchant only through a port.** Razorpay's documentation of a
failed UPI Autopay debit names no NPCI code (§12): a Razorpay merchant is told
Razorpay's reason, and §12 maps those. This vocabulary classifies a failure only
when a provider passes the rail's own code on, through `RailCodeSource`
(`vasool/events/rail_codes.py`), whose default passes nothing. The classifier
reaches the mapping since §2.5's failure path (`docs/EVALUATION.md` §10,
2026-09-15); none of these codes has been seen arriving anywhere.

### What the vocabulary says about the taxonomy

**92 of NPCI's 225 codes fit the five classes. 133 do not.** The rule was to
record a misfit as unmapped, never to widen a class until the code fits, and
the misfits are the most useful thing here. In order of consequence:

1. **Money in flight — 30 codes (`RECONCILE`).** Timeouts on or after the
   debit leg, pending, partial or failed reversals, duplicates, late responses,
   and `VE MANDATE IS ALREADY HONOURED`. A timeout in the authorisation step
   (`U09`, `U20`) comes before any debit and stays `TRANSIENT`. Every class above assumes a failed payment moved no money, but
   these codes say it may have. `TRANSIENT`'s retry would be a second debit,
   and so would the fifth sentence of §10: *unknown reasons fail safe to one
   silent retry*. That sentence is right for the Razorpay reasons it was
   written for and wrong for these thirty. The correct first action is a status
   check (NPCI settles these through UDIR, §2.1 of its document), never a
   retry. **§2.5 must not route an unmapped UPI code through the fail-safe.**
2. **Legal stops — 5 codes (`LEGAL_STOP`).** Death of the account holder,
   insolvency, incapacity, a court order, an attachment order.
   `INSTRUMENT_DEAD` would send a re-authorisation link to a deceased
   customer's phone, or into an insolvency moratorium. Like `RISK_BLOCK`, only
   a human should act, but for a different reason and with a different
   obligation, so these are not risk declines either.
3. **Two codes, not one — 6 codes (`NO_CAUSE`).** `U30 DEBIT HAS BEEN FAILED`
   reports an outcome and carries no cause. The cause is the bank's own code
   that travels with it: NPCI defines an ErrorCode populated by UPI and a
   RespCode populated by the bank (its §2.2 and §2.3). A UPI failure has to be
   classified on the pair, which is §3's lesson — classify on
   `(error_reason, error_source)`, not one field — arriving again on a new rail.
4. **Caps — 10 codes (`CAP`).** Frequency, per-transaction and first-time-user
   limits, the net-debit cap, a debit above its block. The instrument works and
   the money is there. The request succeeds below the limit or once its window
   resets: a time-shifted retry, as for `LIQUIDITY`, but timed to the cap's
   window rather than to payday.
5. **The merchant's own side — 26 codes (`PAYEE_SIDE`).** The beneficiary
   account frozen, dormant or nonexistent, the merchant blocked, the acquirer
   declining. Every class assumes the failure is the customer's or the rail's.
   A message asking the customer to fix these would be false.
6. **Integration faults — 50 codes (`INTEGRATION`).** Checksums, format and
   validation errors, mismatches with the original request, and debits that
   break their mandate's registered rules. An engineer's fault, found by an
   engineer.
7. **The rest — 4 `UNDESCRIBED`, 2 `NOT_A_DECLINE`.** NPCI's own catch-alls
   (`XB`, `XC`, `YI`) and a code that does not say whose address failed to
   resolve (`U29`). Then success (`00`), and `NO`, which NPCI reserves for
   status checks.

### What the document gets wrong, transcribed anyway

The transcription is verbatim, so the document's own defects are part of it
and are recorded here rather than silently fixed. **`U81` has two meanings**:
"REMITTER BANK DEEMED CHECK DECLINE" in §4.4 and "UIDAI AUTH RES INVALID/FORMAT
ERROR" in §4.5. That is why the vocabulary is keyed by `(section, code)`,
never by code. **`MB7` and `MQ7`**, which describe mandate validity and
merchant category, are printed inside §4.4's table under a repeated header.
**Seven codes have no TD/BD value** (`ZL` in §3.1, and `S95`–`S98` and
`HS1`–`HS3` in §4.4); they are left blank, not guessed. Typing errors such as
"ACQURIER" are kept as printed. The one repair the extraction needed, `FL`'s
flag wrapped past a page break, is recorded in `tools/cite_npci.py`.

### Every code

<!-- npci-table:start — generated by `python tools/cite_npci.py table`; do not edit -->

#### `TRANSIENT` (48)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `HS` | BANKS HSM IS DOWN(REMITTER) | TD | a bank, switch or PSP failed technically; nothing moved |
| 3.1 | `IR` | UNABLE TO PROCESS DUE TO INTERNAL EXCEPTION AT SERVER/CBS/ETC ON REMITTER SIDE | TD | a bank, switch or PSP failed technically; nothing moved |
| 3.1 | `LD` | UNABLE TO PROCESS DEBIT IN BANK’S POOL/BGL ACCOUNT | TD | a bank, switch or PSP failed technically; nothing moved |
| 3.1 | `UB` | UNABLE TO PROCESS DUE TO INTERNAL EXCEPTION AT SERVER/CBS/ETC ON BENEFICIARY SIDE | TD | the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong |
| 3.1 | `X7` | MERCHANT NOT REACHABLE (ACQURIER) | TD | the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong |
| 3.1 | `XT` | CUT-OFF IS IN PROCESS (REMITTER) | TD | the customer's bank is in its cut-off; nothing moved |
| 3.1 | `XU` | CUT-OFF IS IN PROCESS (BENEFICIARY) | TD | the merchant's bank is in its cut-off; nothing moved |
| 3.1 | `XY` | REMITTER CBS OFFLINE | TD | a bank, switch or PSP failed technically; nothing moved |
| 3.1 | `Y1` | BENEFICIARY CBS OFFLINE | TD | the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong |
| 3.1 | `ZC` | ACQUIRER/BENEFICIARY UNAVAILABLE (Reserved for future purpose) | TD | the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong |
| 3.1 | `ZJ` | BENEFICIARY OR ACQUIRING SWITCH IS INOPERATIVE/NODE OFFLINE (Reserved for future purpose) | TD | the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong |
| 3.1 | `ZK` | REMITTER SWITCH IS INOPERATIVE/NODE OFFLINE (Reserved for future purpose) | TD | a bank, switch or PSP failed technically; nothing moved |
| 4.1 | `21` | NO ACTION TAKEN (FULL REVERSAL) | TD | fully reversed: nothing moved, so a fresh attempt is safe |
| 4.4 | `U08` | SYSTEM EXCEPTION | TD | a system exception inside UPI; nothing moved |
| 4.4 | `U09` | REQAUTH TIME OUT FOR PAY | TD | the authorisation step timed out, before any debit |
| 4.4 | `U13` | EXTERNAL ERROR | TD | NPCI marks it TD and describes it no further |
| 4.4 | `U18` | REQUEST AUTHORISATION ACKNOWLEDGEMENT IS NOT RECEIVED | TD | the authorisation step went unacknowledged, before any debit |
| 4.4 | `U20` | REQUEST AUTHORISATION TIMEOUT | TD | the authorisation step timed out, before any debit |
| 4.4 | `U22` | CM REQUEST IS DECLINED | TD | NPCI marks it TD and describes it no further |
| 4.4 | `U23` | CM REQUEST TIMEOUT | TD | NPCI marks it TD and describes it no further |
| 4.4 | `U24` | CM REQUEST ACKNOWLEDGEMENT IS NOT RECEIVED | TD | NPCI marks it TD and describes it no further |
| 4.4 | `U27` | NO RESPONSE FROM PSP | TD | the PSP did not respond, before any debit |
| 4.4 | `U28` | REMITTER BANK NOT AVAILABLE | TD | a bank, switch or PSP failed technically; nothing moved |
| 4.4 | `U40` | IMPS PROCESSING FAILED IN UPI | TD | a bank, switch or PSP failed technically; nothing moved |
| 4.4 | `U41` | IMPS IS SIGNED OFF | TD | a bank, switch or PSP failed technically; nothing moved |
| 4.4 | `U44` | FORM HAS BEEN SIGNED OFF | TD | NPCI marks it TD and describes it no further |
| 4.4 | `U45` | FORM PROCESSING HAS BEEN FAILED IN UPI | TD | NPCI marks it TD and describes it no further |
| 4.4 | `U72` | VAE FAILED | TD | NPCI marks it TD and describes it no further |
| 4.4 | `U78` | BENEFICIARY BANK OFFLINE | TD | the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong |
| 4.4 | `U80` | PAYER PSP THROTTLE DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `U81` | REMITTER BANK DEEMED CHECK DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `U84` | BENEFICIARY BANK DEEMED CHECK DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `U85` | CONNECTION TIMEOUT IN REQPAY DEBIT | TD | the request was never delivered, so nothing was debited |
| 4.4 | `U86` | REMITTER BANK THROTTLING DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `U89` | BENEFICIARY BANK THROTTLING DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `U90` | REMITTER BANK DEEMED HIGH RESPONSE TIME CHECK DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `U91` | BENEFICIARY BANK DEEMED HIGH RESPONSE TIME CHECK DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `U92` | PAYER PSP NOT AVAILABLE | TD | a bank, switch or PSP failed technically; nothing moved |
| 4.4 | `U93` | PAYEE PSP NOT AVAILABLE | TD | the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong |
| 4.4 | `U94` | PAYEE PSP THROTTLE DECLINE | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `S93` | PAYEE_PSP_THROTTLE_DECLINE_OUTGOING_COUNT | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `S94` | PAYEE_PSP_THROTTLE_DECLINE_RESPONSE_TIME | TD | declined up front by a throttle or health check; nothing was sent |
| 4.4 | `S96` | REMITTER_DISPATCH_FAILED | — | the request was never delivered, so nothing was debited |
| 4.4 | `S97` | ADD_RESLN_DISPATCH_FAILED | — | address resolution was never dispatched, before any debit |
| 4.4 | `S98` | ISSUER_DISPATCH_FAILED | — | the request was never delivered, so nothing was debited |
| 4.4 | `HS1` | HSM_OFFINE | — | UPI's HSM was unavailable; nothing moved |
| 4.4 | `HS2` | HSM_TIMEOUT | — | UPI's HSM was unavailable; nothing moved |
| 4.4 | `HS3` | HSM_COMMUNICATION_ERROR | — | UPI's HSM was unavailable; nothing moved |

#### `LIQUIDITY` (2)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `IE` | ADEQUATE FUNDS NOT AVAILABLE IN THE ACCOUNT BECAUSE FUNDS HAVE BEEN BLOCKED FOR MANDATE | BD | the funds exist but another mandate has blocked them |
| 3.1 | `Z9` | INSUFFICIENT FUNDS IN CUSTOMER (REMITTER) ACCOUNT | BD | the account is short of funds right now |

#### `INSTRUMENT_DEAD` (25)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `15` | ISSUER NOT LIVE ON UPI | BD | the customer's bank is not live on UPI |
| 3.1 | `B1` | REGISTERED MOBILE NUMBER LINKED TO THE ACCOUNT HAS BEEN CHANGED/REMOVED | BD | the mobile number the UPI profile rests on changed; it must be re-registered |
| 3.1 | `B3` | TRANSACTION NOT PERMITTED TO THE ACCOUNT (EXAMPLE: MINOR ACCOUNT, PROPRIETOR ACCOUNT, LEGAL CASE AGAINST THIS ACCOUNT ETC., NRE (AS PER BANK’S POLICY)) | BD | the bank does not permit this account to be debited this way |
| 3.1 | `IC` | DEBIT AMOUNT IS NOT BLOCKED FOR THE CUSTOMER | BD | the block this debit draws on does not exist |
| 3.1 | `QU` | PAYER ACCOUNT HAS CHANGED(PAYER) | BD | the account behind the payer's address changed |
| 3.1 | `VA` | MANDATE HAS BEEN REVOKED | BD | the mandate was revoked |
| 3.1 | `VF` | UMN DOES NOT EXIST (REMITTER) | BD | the bank holds no mandate with this UMN |
| 3.1 | `VG` | PAYER VPA IS INCORRECT (REMITTER) | BD | the payer address on the mandate is wrong |
| 3.1 | `VJ` | PAYER ACCOUNT HAS CHANGED (REMITTER) | BD | the account behind the mandate changed |
| 3.1 | `VL` | MANDATE REGISTRATION NOT ALLOWED FOR CC PF PPF ACT (BANK'S POLICY) | BD | this account type cannot carry a mandate |
| 3.1 | `VM` | NATURE OF DEBIT NOT ALLOWED IN ACCOUNT TYPE | BD | this account type does not allow this kind of debit |
| 3.1 | `VU` | MANDATE HAS EXPIRED | BD | the mandate expired |
| 3.1 | `XH` | ACCOUNT DOES NOT EXIST (REMITTER) | BD | the customer's account does not exist |
| 3.1 | `XJ` | REQUESTED FUNCTION NOT SUPPORTED (REMITTER) | BD | the customer's bank does not support this function |
| 3.1 | `XL` | EXPIRED CARD, DECLINE (REMITTER) | BD | the card on record expired, as card_expired |
| 3.1 | `XN` | NO CARD RECORD (REMITTER) | BD | the bank holds no record of the card |
| 3.1 | `XP` | TRANSACTION NOT PERMITTED TO CARDHOLDER (REMITTER) | BD | the card is not permitted this transaction, as card_disabled_for_online_payments |
| 3.1 | `XR` | RESTRICTED CARD, DECLINE (REMITTER) | BD | the card is restricted |
| 3.1 | `XX` | NO FINANCIAL ADDRESS RECORD FOUND | BD | no account is mapped to this payment address |
| 3.1 | `YC` | DO NOT HONOUR (REMITTER) | BD | a generic 'do not honour': one probe, then dead, exactly as card_declined |
| 3.1 | `YE` | REMITTING ACCOUNT BLOCKED/FROZEN | BD | the customer's account is blocked or frozen; the code does not say why |
| 3.1 | `Z6` | NUMBER OF PIN TRIES EXCEEDED | BD | too many wrong PINs: the bank has locked UPI on this account |
| 3.1 | `ZF` | TRANSACTION NOT PERMITTED TO DEVICE | BD | the bank does not permit payments from this device |
| 3.1 | `ZX` | INACTIVE OR DORMANT ACCOUNT (REMITTER) | BD | the customer's account is inactive or dormant |
| 3.1 | `MR` | Incorrect Account details due to Amalgamated/Merged Activity on Remitter Side (Remitter) | BD | the account details changed in a bank merger |

#### `CUSTOMER_ACTION` (7)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `AM` | MPIN NOT SET BY CUSTOMER | BD | the customer has not set a UPI PIN yet |
| 3.1 | `VT` | MANDATE IS PAUSED | BD | the customer paused the mandate; it works again once they resume it |
| 3.1 | `ZM` | INVALID MPIN | BD | the customer entered a wrong PIN |
| 3.1 | `ZR` | INVALID OTP | BD | the customer entered a wrong OTP |
| 3.1 | `ZS` | OTP EXPIRED | BD | the OTP expired before the customer used it |
| 3.1 | `ZV` | INCORRECT OTP (Reserved for future purpose) | BD | the customer entered a wrong OTP |
| 4.4 | `U69` | COLLECT EXPIRED | BD | the customer did not approve the collect request before it expired |

#### `RISK_BLOCK` (10)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `59` | SUSPECTED FRAUD, DECLINE/TRANSACTIONS DECLINED BASED ON RISKSCORE BY REMITTER | BD | a risk engine suspected fraud |
| 3.1 | `CI` | COMPLIANCE ERROR CODE FOR ISSUER | BD | a compliance decline; if it is screening, an automated request is tipping-off |
| 3.1 | `K1` | SUSPECTED FRAUD, DECLINE / TRANSACTIONS DECLINED BASED ON RISK SCORE BY REMITTER | BD | a risk engine suspected fraud |
| 3.1 | `VH` | MANDATE SIGNATURE IS TAMPERED OR CORRUPT (REMITTER) | BD | the mandate's signature is tampered or corrupt |
| 3.1 | `XV` | TRANSACTION CANNOT BE COMPLETED. COMPLIANCE VIOLATION (REMITTER) | BD | a compliance decline; if it is screening, an automated request is tipping-off |
| 3.1 | `YA` | LOST OR STOLEN CARD (REMITTER) | BD | a card reported lost or stolen: its holder may not know, or may not be the one paying |
| 3.1 | `ZI` | SUSPECTED FRAUD, DECLINE / TRANSACTIONS DECLINED BASED ON RISK SCORE BY BENEFICIARY | BD | a risk engine suspected fraud |
| 4.4 | `M16` | AI MODEL DECLINE | BD | NPCI's AI model declined it |
| 4.4 | `U16` | RISK THRESHOLD EXCEEDED | BD | UPI's risk threshold was exceeded |
| 4.4 | `U66` | DEVICE FINGERPRINT MISMATCH | BD | the device is not the one the bank registered: the pattern of account takeover |

#### Unmapped — `RECONCILE` (30)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `DF` | DUPLICATE RRN FOUND IN THE TRANSACTION. (BENEFICIARY) | BD | a duplicate: an earlier request decides, and it may have succeeded |
| 3.1 | `DT` | DUPLICATE RRN FOUND IN THE TRANSACTION. (REMITTER) | BD | a duplicate: an earlier request decides, and it may have succeeded |
| 3.1 | `UP` | PSP TIME-OUT | TD | a timeout: whether money moved is settled later, by status check or UDIR |
| 3.1 | `VE` | MANDATE IS ALREADY HONOURED | BD | this execution was already honoured: a retry would collect twice |
| 3.1 | `VS` | DUPLICATE MANDATE REQUEST FOR SAME ITEM | BD | a duplicate: an earlier request decides, and it may have succeeded |
| 3.1 | `ZL` | RECEIVED LATE RESPONSE (Reserved for future purpose) | — | a late response may have carried a success |
| 3.1 | `ZQ` | UNABLE TO PROCESS REVERSAL (Reserved for future purpose) | BD | a reversal is pending, partial or failed: money has moved |
| 4.1 | `32` | PARTIAL REVERSAL | TD | a reversal is pending, partial or failed: money has moved |
| 4.1 | `BT` | ACQUIRER/BENEFICIARY UNAVAILABLE(TIMEOUT) | TD | a timeout: whether money moved is settled later, by status check or UDIR |
| 4.1 | `RB` | CREDIT REVERSAL TIMEOUT(REVERSAL) | TD | a reversal is pending, partial or failed: money has moved |
| 4.1 | `RP` | PARTIAL DEBIT REVERSAL TIMEOUT | TD | a reversal is pending, partial or failed: money has moved |
| 4.1 | `RR` | DEBIT REVERSAL TIMEOUT(REVERSAL) | TD | a reversal is pending, partial or failed: money has moved |
| 4.1 | `UT` | REMITTER/ISSUER UNAVAILABLE (TIMEOUT) | TD | a timeout: whether money moved is settled later, by status check or UDIR |
| 4.4 | `U01` | THE REQUEST IS DUPLICATE | TD | a duplicate: an earlier request decides, and it may have succeeded |
| 4.4 | `U26` | PSP REQUEST CREDIT PAY ACKNOWLEDGEMENT IS NOT RECEIVED | TD | the credit leg follows the debit, so the customer may already be charged |
| 4.4 | `U32` | CREDIT REVERT HAS BEEN FAILED | TD | a reversal is pending, partial or failed: money has moved |
| 4.4 | `U33` | DEBIT REVERT HAS BEEN FAILED | TD | a reversal is pending, partial or failed: money has moved |
| 4.4 | `U35` | RESPONSE IS ALREADY BEEN RECEIVED | TD | a duplicate: an earlier request decides, and it may have succeeded |
| 4.4 | `U36` | REQUEST IS ALREADY BEEN SENT | TD | a duplicate: an earlier request decides, and it may have succeeded |
| 4.4 | `U37` | REVERSAL HAS BEEN SENT | TD | a reversal is pending, partial or failed: money has moved |
| 4.4 | `U38` | RESPONSE IS ALREADY BEEN SENT | TD | a duplicate: an earlier request decides, and it may have succeeded |
| 4.4 | `U42` | IMPS TRANSACTION IS ALREADY BEEN PROCESSED | TD | a duplicate: an earlier request decides, and it may have succeeded |
| 4.4 | `U53` | PSP REQUEST PAY DEBIT ACKNOWLEDGEMENT NOT RECEIVED | TD | the debit leg went unacknowledged: it may have happened |
| 4.4 | `U67` | DEBIT TIMEOUT | TD | a timeout: whether money moved is settled later, by status check or UDIR |
| 4.4 | `U68` | CREDIT TIMEOUT | TD | a timeout: whether money moved is settled later, by status check or UDIR |
| 4.4 | `U70` | RECEIVED LATE RESPONSE | TD | a late response may have carried a success |
| 4.4 | `U82` | READ TIMEOUT IN REQPAY CREDIT | TD | the credit leg follows the debit, so the customer may already be charged |
| 4.4 | `U87` | READ TIMEOUT IN REQPAY DEBIT | TD | a timeout: whether money moved is settled later, by status check or UDIR |
| 4.4 | `U88` | CONNECTION TIMEOUT IN REQPAY CREDIT | TD | the credit leg follows the debit, so the customer may already be charged |
| 4.4 | `S95` | BENEFICIARY_DISPATCH_FAILED | — | the credit leg follows the debit, so the customer may already be charged |

#### Unmapped — `LEGAL_STOP` (5)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `VO` | PAYMENT STOPPED BY COURT ORDER | BD | a legal status stops the account; no automated request should go out |
| 3.1 | `VP` | WITHDRAWAL STOPPED OWING TO DEATH OF ACCOUNT HOLDER | BD | a legal status stops the account; no automated request should go out |
| 3.1 | `VQ` | WITHDRAWAL STOPPED OWING TO INSOLVENCY OF ACCOUNT | BD | a legal status stops the account; no automated request should go out |
| 3.1 | `VR` | WITHDRAWAL STOPPED OWING TO LUNACY OF ACCOUNT HOLD | BD | a legal status stops the account; no automated request should go out |
| 3.1 | `VZ` | PAYMENT STOPPED BY ATTACHMENT ORDER | BD | a legal status stops the account; no automated request should go out |

#### Unmapped — `CAP` (10)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `ID` | DEBIT AMOUNT GREATER THAN BLOCKED AMOUNT | BD | the debit exceeds the amount blocked for it |
| 3.1 | `VK` | NUMBER OF MANDATES ALLOWED ON THIS ACCOUNT HAS EXCEEDED ISSUER'S LIMIT (OPTIONAL: AS PER BANK'S POLICY) | BD | the account holds as many mandates as its bank allows |
| 3.1 | `Z7` | TRANSACTION FREQUENCY LIMIT EXCEEDED AS SET BY REMITTING MEMBER | BD | a limit binds while the instrument works and the money exists |
| 3.1 | `Z8` | PER TRANSACTION LIMIT EXCEEDED AS SET BY REMITTING MEMBER | BD | a limit binds while the instrument works and the money exists |
| 3.1 | `ZT` | OTP TRANSACTION LIMIT EXCEEDED | BD | a limit binds while the instrument works and the money exists |
| 3.1 | `ZU` | LIMIT EXCEEDED FOR REMITTING BANK/ISSUING BANK | BD | a limit binds while the instrument works and the money exists |
| 3.1 | `FL` | FIRST TRANSACTION LIMIT EXCEEDED | BD | a first-time user's first transaction is capped |
| 3.1 | `FP` | FREEZE PERIOD FOR FIRST TIME USER | BD | a first-time user is inside the 24-hour cool-down |
| 4.4 | `U02` | AMOUNT CAP IS EXCEEDED | BD | a limit binds while the instrument works and the money exists |
| 4.4 | `U03` | NET DEBIT CAP IS EXCEEDED | BD | the bank's net debit cap: a system limit that resets |

#### Unmapped — `PAYEE_SIDE` (26)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `CA` | COMPLIANCE ERROR CODE FOR ACQUIRER | BD | a compliance decline at the merchant's acquirer |
| 3.1 | `LC` | UNABLE TO PROCESS CREDIT FROM BANK’S POOL/BGL ACCOUNT | BD | the beneficiary bank could not credit from its pool account |
| 3.1 | `PS` | MAXIMUM BALANCE EXCEEDED AS SET BY BENEFICIARY BANK | BD | the merchant's account is at its maximum balance |
| 3.1 | `VY` | PAYEE VPA IS INCORRECT (REMITTER) | BD | the merchant's own address on the debit is wrong |
| 3.1 | `X6` | INVALID MERCHANT (ACQURIER) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `XI` | ACCOUNT DOES NOT EXIST (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `XK` | REQUESTED FUNCTION NOT SUPPORTED (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `XM` | EXPIRED CARD, DECLINE (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `XO` | NO CARD RECORD (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `XQ` | TRANSACTION NOT PERMITTED TO CARDHOLDER (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `XS` | RESTRICTED CARD, DECLINE (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `XW` | TRANSACTION CANNOT BE COMPLETED. COMPLIANCE VIOLATION (BENEFICIARY) | BD | a compliance decline on the merchant's side |
| 3.1 | `YB` | LOST OR STOLEN CARD (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `YD` | DO NOT HONOUR (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `YF` | BENEFICIARY ACCOUNT BLOCKED/FROZEN | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `YH` | MERCHANT ERROR(ACQUIRING BANK) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `Z5` | INVALID BENEFICIARY CREDENTIALS | BD | the beneficiary's credentials are invalid |
| 3.1 | `ZN` | FUNCTIONALITY NOT YET AVAILABLE FOR MERCHANT THROUGH THE ACQUIRING BANK | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `ZO` | FUNCTIONALITY NOT YET AVAILABLE FOR CUSTOMER THROUGH THE PAYEE PSP | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `ZP` | BANKS AS BENEFICIARY NOT LIVE ON PARTICULAR TXN TYPE | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `ZY` | INACTIVE OR DORMANT ACCOUNT (BENEFICIARY) | BD | the merchant's own side failed; nothing the customer does helps |
| 3.1 | `MB` | Incorrect Account details due to Amalgamated/Merged Activity on Beneficiary Side (Beneficiary) | BD | the merchant's own side failed; nothing the customer does helps |
| 4.4 | `MQ7` | MCC Code Not Mapped with Purpose | BD | the merchant's MCC is not mapped to the purpose |
| 4.4 | `U71` | MERCHANT CREDIT NOT SUPPORTED IN IMPS | TD | the merchant's account cannot take this credit |
| 4.4 | `U77` | MERCHANT BLOCKED | TD | the merchant itself is blocked |
| 4.4 | `U95` | PAYEE VPA AADHAAR OR IIN VPA IS DISABLED | BD | the merchant's payment address is disabled |

#### Unmapped — `INTEGRATION` (50)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `B6` | MISMATCH IN PAYMENT DETAILS | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 3.1 | `UA` | PSP NOT SUPPORTED BY UPI | BD | the PSP is not supported by UPI |
| 3.1 | `VB` | INCORRECT RECURRENCE PATTERN | BD | the debit breaks its mandate's registered rules: the merchant's schedule is wrong |
| 3.1 | `VC` | INCORRECT RECURRENCE PATTERN RULE | BD | the debit breaks its mandate's registered rules: the merchant's schedule is wrong |
| 3.1 | `VD` | INCORRECT AMOUNT RULE | BD | the debit breaks its mandate's registered rules: the merchant's schedule is wrong |
| 3.1 | `VI` | EXECUTION DAY AND EXECUTION RULE MISMATCH (REMITTER) | BD | the debit breaks its mandate's registered rules: the merchant's schedule is wrong |
| 3.1 | `XD` | INVALID AMOUNT (REMITTER) | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 3.1 | `XE` | INVALID AMOUNT (BENEFICIARY) | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 3.1 | `XF` | FORMAT ERROR (INVALID FORMAT) (REMITTER) | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 3.1 | `XG` | FORMAT ERROR (INVALID FORMAT) (BENEFICIARY) | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 3.1 | `ZD` | VALIDATION ERROR | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U04` | REQUEST IS NOT FOUND | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U05` | FORMATION IS NOT PROPER | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `MB7` | Validity greater than 1 year not allowed | BD | a mandate longer than a year was requested |
| 4.4 | `U06` | TRANSACTION ID IS MISMATCHED | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U07` | VALIDATION ERROR | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U10` | ILLEGAL OPERATION | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U11` | CREDENTIALS IS NOT PRESENT | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U12` | AMOUNT OR CURRENCY MISMATCH | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U14` | ENCRYPTION ERROR | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U15` | CHECKSUM FAILED | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U17` | PSP IS NOT REGISTERED | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U21` | REQUEST AUTHORISATION IS NOT FOUND | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U25` | CM URL IS NOT FOUND | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U46` | REQUEST CREDIT IS NOT FOUND | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U47` | REQUEST DEBIT IS NOT FOUND | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U48` | TRANSACTION ID IS NOT PRESENT | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U49` | REQUEST MESSAGE ID IS NOT PRESENT | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U50` | IFSC IS NOT PRESENT | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U51` | REQUEST REFUND IS NOT FOUND | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U52` | PSP ORGID NOT FOUND | BD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U54` | TRANSACTION ID OR AMOUNT IN CREDENTIAL BLOCK DOES NOT MATCH WITH THAT IN REQPAY | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U55` | MESSAGE INTEGRITY FAILED DUE TO ORGID MISMATCH | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U56` | NUMBER OF PAYEES DIFFERS FROM ORIGINAL REQUEST | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U57` | PAYEE AMOUNT DIFFERS FROM ORIGINAL REQUEST | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U58` | PAYER AMOUNT DIFFERS FROM ORIGINAL REQUEST | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U59` | PAYEE ADDRESS DIFFERS FROM ORIGINAL REQUEST | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U60` | PAYER ADDRESS DIFFERS FROM ORIGINAL REQUEST | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U61` | PAYEE INFO DIFFERS FROM ORIGINAL REQUEST | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U62` | PAYER INFO DIFFERS FROM ORIGINAL REQUEST | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U63` | DEVICE REGISTRATION FAILED IN UPI | TD | device registration, not a payment |
| 4.4 | `U64` | DATA TAG SHOULD CONTAIN 4 PARTS DURING DEVICE REGISTRATION | TD | device registration, not a payment |
| 4.4 | `U65` | CREDS BLOCK SHOULD CONTAIN CORRECT ELEMENTS DURING DEVICE REGISTRATION | TD | device registration, not a payment |
| 4.4 | `U74` | PAYER ACCOUNT MISMATCH | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U75` | PAYEE ACCOUNT MISMATCH | TD | a malformed or mismatched message: an engineer's fault, not the customer's |
| 4.4 | `U76` | MOBILE BANKING REGISTRATION FORMAT NOT SUPPORTED BY THE ISSUER BANK | TD | mobile-banking registration, not a payment |
| 4.4 | `U96` | PAYER AND PAYEE IFSC/ACNUM CAN'T BE SAME | BD | payer and payee are the same account |
| 4.4 | `U97` | PSP REQUEST META ACKNOWLEDGEMENT NOT RECEIVED | TD | a meta transaction, not a payment |
| 4.4 | `U98` | NULL ACK RECEIVED BY UPI FOR META TRANSACTION | TD | a meta transaction, not a payment |
| 4.4 | `U99` | NEGATIVE ACK RECEIVED BY UPI FOR META TRANSACTION | TD | a meta transaction, not a payment |

#### Unmapped — `NO_CAUSE` (6)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 4.4 | `U19` | REQUEST AUTHORISATION IS DECLINED | BD | the PSP declined authorisation; the reason is in its own code, outside this vocabulary |
| 4.4 | `U30` | DEBIT HAS BEEN FAILED | NA | an outcome without a cause; classify on the code that accompanies it |
| 4.4 | `U31` | CREDIT HAS BEEN FAILED | NA | an outcome without a cause; classify on the code that accompanies it |
| 4.4 | `U34` | REVERTED | NA | an outcome without a cause; classify on the code that accompanies it |
| 4.4 | `U39` | TRANSACTION IS ALREADY BEEN FAILED | TD | an earlier failure decides; its own code carries the cause |
| 4.4 | `U43` | IMPS IS DECLINED | NA | an outcome without a cause; classify on the code that accompanies it |

#### Unmapped — `UNDESCRIBED` (4)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `XB` | INVALID TRANSACTION OR IF MEMBER IS NOT ABLE TO FIND ANY APPROPRIATE RESPONSE CODE (REMITTER) | BD | NPCI's own catch-all for a member with no better code |
| 3.1 | `XC` | INVALID TRANSACTION OR IF MEMBER IS NOT ABLE TO FIND ANY APPROPRIATE RESPONSE CODE (BENEFICIARY) | BD | NPCI's own catch-all for a member with no better code |
| 3.1 | `YI` | INVALID RESPONSE CODE | BD | the bank returned a response code UPI does not recognise |
| 4.4 | `U29` | ADDRESS RESOLUTION IS FAILED | NA | does not say whose address failed to resolve, the customer's or the merchant's |

#### Unmapped — `NOT_A_DECLINE` (2)

| § | Code | NPCI's description | TD/BD | Why |
|---|---|---|---|---|
| 3.1 | `00` | APPROVED OR COMPLETED SUCCESSFULLY | - | success |
| 3.1 | `NO` | NO ORIGINAL REQUEST FOUND DURING DEBIT/CREDIT | TD | NPCI reserves it for status checks: 'members should not decline' with it |

<!-- npci-table:end -->

## 12. UPI Autopay on Razorpay: the reasons a merchant is actually sent

§11 mapped NPCI's vocabulary. It does not reach a Razorpay merchant.
Razorpay's documentation of subsequent UPI payments — *Create Subsequent
Payments* (UPI), read from its markdown source on 2026-09-15, SHA-256
`6c26636b…` — reports a failed debit in Razorpay's own error envelope (`code`,
`description`, `source`, `step`, `reason`) and lists **61 values of `reason`**.
No NPCI code appears anywhere on the page. So the failure path the programme's
design asked for — a UPI Autopay debit that "fails with a cited NPCI code" — is
built from what a Razorpay merchant is told instead, and NPCI's vocabulary sits
behind a port for a provider that passes the rail's own code on
(`vasool/events/rail_codes.py`, whose default passes nothing). Registered, with
its expectations, before any of its code: `docs/EVALUATION.md` §10, 2026-09-15.

**Provenance: `SIMULATED`, from documentation.** Each reason has one stub in
`data/stubbed_payloads/` (`SIMULATED__upi_autopay__<reason>.json`, built by
`tools/make_upi_stubs.py` from any copy of the page whose hash matches). The
reason and its description are verbatim. **The code** is set for the 19
reasons Razorpay's *List of Errors* files under "Bad Request Errors" or
"Gateway Errors", and null for the other 42. **The source and step are null in
every file**: Razorpay documents the values each can take on UPI and pairs
neither with any reason, so any value would be a pairing nobody documented.
None of the 61 has been seen on this account, which cannot take UPI before
activation (`docs/VERIFIED.md`).

**The mapping is this project's judgement**, in
`vasool/diagnosis/razorpay_upi.py`, over §11's outcome vocabulary — the five
classes, or one of NPCI's `Unmapped` reasons — because the two vocabularies
describe the same rail. The table below is rendered from it and tested against
it.

**Classified on the rail, not the string.** Five of the 61 strings are card
reasons in §4 too — `gateway_technical_error`, `insufficient_funds`,
`payment_failed`, `payment_risk_check_failed`, `payment_timed_out` — and on UPI
three of them can mean money moved. §3's lesson, that a reason alone is not
enough, arrives one level up: a UPI failure (Razorpay's `method: upi`) is read
against this table, a card failure against §4, and §4 does not change.

### The rule that decides the most: money that may be in flight

**29 of the 61 reasons fit the five classes. 32 do not**, and fifteen of those
say money may already have moved: "Any amount deducted will be refunded", "If
money got deducted", a pending payment, a timeout at a step the page does not
name, a response that never came, a mandate "already honoured" this cycle.
**For fourteen of the fifteen, Razorpay's own next step is to try again** —
"Retry after some time", "Please try again after some time". That
is the advice that charges a customer twice: a retry while a deduction is being
refunded is a second deduction. The same page says the opposite a few lines
earlier — "Do not create another subsequent payment until you get the status of
the previous one" — and NPCI's OC-215 gives the protocol: the first status
check "after 90 seconds", "maximum of 3 check transaction status APIs,
preferably within 2 hours". So these fifteen are `RECONCILE`, and get a status
check, never a retry.

### `STATUS_CHECK`, the one intervention §4 does not have

§4's five interventions each act on the customer or the instrument. A failure
whose money may already have moved needs one that does neither: **ask the rail
what happened.** It re-presents nothing and reaches nobody, so it is neither a
retry nor a contact, and no guard about either has jurisdiction; a promise to pay
does not hold it and the unattended-amount ceiling does not escalate it, because
finding out whether a customer was charged twice is not chasing them. It is
argued here because `InterventionType` is closed, and a member belongs in this
document before it belongs in code.

It is sent two ways: by a `RECONCILE` reason, 90 seconds after the failure, and
by a debit whose own response was lost (§10 of the protocol, 2026-09-15: the
client no longer re-sends a debit on a 5xx or a timeout). The rail's answer
decides the episode (`vasool/policy/machine.py`): **debited** — recovered, like
any settlement; **pending** — asked again, up to three checks inside two hours;
**not debited**, **cannot tell**, or a third pending — a person decides. None of
them leads to a debit. The LLM classifier is not offered it: it is the rail's
question, not a reading of four error fields.

**Production cannot tell yet.** No status call has been observed on this
account. The status port's default answers "cannot tell", so every `RECONCILE`
failure goes to a person today. The documented adapter reads Razorpay's order
entity (`created`, `attempted`, `paid`) and is not the default.

### The rest

- **No other unmapped reason is retried either.** `INTEGRATION` (7),
  `CAP` (5), `PAYEE_SIDE` (3), `LEGAL_STOP` (1) and `UNDESCRIBED` (1) go to a
  person, with the outcome named first on the receipt. So does a UPI reason the
  page does not document: §4's fail-safe is one silent retry, and on UPI an
  unknown failure may have moved money (`vasool/diagnosis/upi.py`,
  `UPI_FAILSAFE`).
- **The classes keep §4's shapes.** A transient failure gets one retry and then
  a link, and on a mandate that retry waits for its own pre-debit notice and for
  NPCI's off-peak hours anyway; `LIQUIDITY` gets §4's salary ladder, whose three
  rungs are exactly NPCI's three retries; a dead instrument gets a link to a new
  mandate — for thirteen of the nineteen, Razorpay's own next step is "Create a
  new mandate with the customer" — and a risk decline goes to a person.
- **The rail moves the mandate.** `mandate_cancelled`, `mandate_paused` and
  `mandate_expired` report a state, and the mandate record is moved to it by
  three transitions citing the reason as evidence and NPCI's `VA`, `VT` and
  `VU` for what the state means (`vasool/mandate/evidence.py`). Where the record
  disagrees — expiry before its `valid_until`, cancellation "by user" of a
  mandate created non-revocable — the rail wins, because it is the authority on
  whether a debit can happen, and the disagreement is kept.

### Known limits

1. **An unmapped outcome has no class, and a Diagnosis must carry one.** Its
   proposal says `TRANSIENT` as a placeholder, and its rationale names the
   outcome first — "Unmapped (RECONCILE)" — so that a receipt is not read as
   calling it transient. A closed five-member enum cannot say "none of these";
   widening it is the move §11 refuses.
2. **"Not debited" goes to a person, not to a retry.** It is exactly when a
   retry would be safe. It is not taken automatically until a status call has
   been observed live.
3. **Source and step are unknown for every UPI reason.** A reason that needs
   them to be read correctly — as §3 needed `error_source` for `payment_failed`
   — will be misread until a UPI failure is captured.
4. **The universe draws twenty-one of these sixty-one reasons, and forty at
   zero.** Registered in `docs/EVALUATION.md` §10 on 2026-09-16: half of the
   simulated mandates run on UPI Autopay (`upi_mandate_share`), their debits
   fail with `UPI_REASON_MIX`, and that table's business-to-technical split is
   NPCI's published ratio while the allocation inside each bucket is a guess.
   So the classification in this section is now exercised by the evaluation and
   not only by tests — but only for the reasons that carry a share. The other
   forty are registered at zero, which is a claim that they do not happen in
   this universe rather than a claim about the rail.

### Every reason

<!-- upi-table:start — generated by `python tools/make_upi_stubs.py table`; do not edit -->

#### `TRANSIENT` (4)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `banks_hsm_is_down_remitter` | Remitter bank failed to process the transaction. Please try again after some time. | — | the customer's bank failed to process it and asks for another try; nothing says money moved |
| `issuer_dispatch_failed` | Payment failed due to some issue at the issuer bank. Please try again after some time. | — | the customer's bank failed to process it and asks for another try; nothing says money moved |
| `psp_bank_not_available` | Payer PSP / Bank not available. Please try again after some time. | — | the payer's PSP or bank was unavailable; nothing was processed |
| `remitter_dispatch_failed` | Payment failed due to some issue at the customer's. Please try again after some time. | — | the customer's bank failed to process it and asks for another try; nothing says money moved |

#### `LIQUIDITY` (2)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `adequate_funds_not_available_blocked` | Sufficient unblocked funds not available in customer's account. Please ask customer to add fund and try again. | — | funds exist but are blocked: 'add sufficient unblocked funds and try again' |
| `insufficient_funds` | Transaction failed due to insufficient funds. | BAD_REQUEST_ERROR | the money is not there today: 'add balance to their account and retry' |

#### `INSTRUMENT_DEAD` (19)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `bank_account_invalid` | Payment failed because Account linked to VPA is invalid. | BAD_REQUEST_ERROR | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `debit_declined` | Payment was unsuccessful as it was declined by remitter bank. | GATEWAY_ERROR | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `debit_instrument_blocked` | Payment was unsuccessful as the account linked to this UPI ID is blocked. Try using another account. | GATEWAY_ERROR | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `invalid_token` | Invalid Token. | — | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `invalid_vpa` | You have entered an incorrect UPI ID. Please retry with the correct UPI ID. | GATEWAY_ERROR | the payer address on the mandate is wrong: the customer must supply a valid one |
| `mandate_cancelled` | UPI mandate created for payment has been cancelled by user. | — | 'cancelled by user': the mandate is revoked, and Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `mandate_expired` | UPI Mandate is expired. | — | the mandate expired, and Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `mandate_not_active` | UPI mandate is not active. | — | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `mobile_number_invalid` | Registered Mobile number linked to the account has been changed or removed. | BAD_REQUEST_ERROR | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `nature_of_debit_not_allowed` | Nature of debit not allowed in customer's account. Please ask the customer to use a different bank account. | — | the account does not allow this debit: 'use a different bank account' |
| `no_financial_address_record_found` | No financial address record found for this vpa. Please ask customer to try with another bank account. | — | no account behind the payer address: 'try with another bank account' |
| `number_of_pin_tries_exceeded` | Customer has exceeded PIN retry limit. Please ask customer to create a new mandate and enter the right PIN. | — | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `payer_account_has_changed` | Payer account linked to the customer's VPA has changed. Please request the customer to either change it to the bank account used during mandate registration or register a new mandate for them. | — | the account behind the payer's address changed since registration |
| `remitter_account_dormant` | Bank Account is closed. | — | 'Bank Account is closed', and Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `transaction_not_allowed` | Payment was unsuccessful as it was declined by your bank. Reach out to your bank for more details. Try using another account. | — | Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `transaction_not_permitted_cardholder` | Transaction not permitted for customer's account. Please ask customer to try with another bank account. | — | the account does not permit this debit: 'try with another bank account' |
| `transaction_not_permitted_to_vpa` | Transaction not permitted to payee VPA by the payer PSP. Please contact your bank to enable Autopay for this VPA. | — | autopay to this merchant is off at the customer's bank, as card_disabled_for_online_payments |
| `umn_does_not_exist_payer` | Mandate does not exist. Please create a new mandate. | — | 'Mandate does not exist': Razorpay's next step is a new mandate: this one cannot be debited as it stands |
| `vpa_resolution_failed` | You have entered an incorrect UPI ID. Please retry with the correct UPI ID. | GATEWAY_ERROR | the payer address does not resolve: the customer must supply a valid one |

#### `CUSTOMER_ACTION` (2)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `mandate_paused` | UPI mandate is not active, it is paused by user. | — | 'paused by user': it works again once the customer resumes it |
| `mpin_not_set_by_customer` | UPI MPIN not set by customer. Please ask customer to set MPIN and try again. | — | the customer has to set a UPI PIN: 'ask customer to set MPIN and try again' |

#### `RISK_BLOCK` (2)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `payment_risk_check_failed` | Payment was unsuccessful as your account does not pass the risk checks done by your bank. Try using another account. | GATEWAY_ERROR | the customer's bank ran risk checks and declined |
| `suspected_fraud_decline` | Suspected fraud, transaction declined by customer's bank. Please try again after some time. | — | 'Suspected fraud, transaction declined by customer's bank' |

#### Unmapped — `RECONCILE` (15)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `bank_not_available` | Payment was unsuccessful as the bank linked to this UPI ID is temporarily unavailable. Any amount deducted will be refunded within 5-7 working days. | GATEWAY_ERROR | the page says an amount may have been deducted and will be refunded: money may have moved |
| `bank_technical_error` | Payment was unsuccessful as it was declined by your bank. Any amount deducted will be refunded within 5-7 working days. | GATEWAY_ERROR | the page says an amount may have been deducted and will be refunded: money may have moved |
| `credit_to_beneficiary_failed` | Payment was unsuccessful due to a temporary issue. Any amount deducted will be refunded within 5-7 working days. | — | the credit leg failed, after the debit: the page says an amount may have been deducted and will be refunded: money may have moved |
| `gateway_technical_error` | Payment processing failed due to error at bank or wallet gateway. | GATEWAY_ERROR | 'If money got deducted, reach out to the seller': money may have moved |
| `invalid_response_from_gateway` | Payment was unsuccessful due to a temporary issue. Any amount deducted will be refunded within 5-7 working days. | GATEWAY_ERROR | the page says an amount may have been deducted and will be refunded: money may have moved |
| `mandate_current_cycle_allowed_debit_exceeds` | Mandate is already honoured. | — | 'Mandate is already honoured': a debit already took this cycle's money |
| `null_ack_processing_failure` | Processing failure at gateway. Please try again after some time. | — | an unacknowledged request: whether the debit leg ran, nothing says |
| `payment_failed` | Payment was unsuccessful due to a temporary issue. If amount got deducted, it will be refunded within 5-7 working days. | GATEWAY_ERROR | the page says an amount may have been deducted and will be refunded: money may have moved |
| `payment_pending` | The status of your payment is pending. You can either wait or retry to pay successfully. | GATEWAY_ERROR | 'The status of your payment is pending': the debit may yet complete |
| `payment_timed_out` | Payment was unsuccessful as the bank linked to this UPI ID is not reachable at this time. | GATEWAY_ERROR | a timeout, at a step the page does not name: on or after the debit, money may have moved |
| `psp_not_available` | Payment was unsuccessful as the UPI app is not reachable at this time. Any amount deducted will be refunded within 5-7 working days. | GATEWAY_ERROR | the page says an amount may have been deducted and will be refunded: money may have moved |
| `psp_timeout` | Payer PSP timed out. Please try again. | — | a timeout, at a step the page does not name: on or after the debit, money may have moved |
| `request_timed_out` | Payment was unsuccessful due to a temporary issue. Any amount deducted will be refunded within 5-7 working days. | GATEWAY_ERROR | the page says an amount may have been deducted and will be refunded: money may have moved |
| `response_not_received_within_tat` | VPA resolution into bank account details failed. Please try again after some time. | — | a response that never arrived in time may have carried a debit |
| `unable_to_process_beneficiary_bank` | Error processing request at beneficiary bank. Please try again after some time. | — | the merchant's bank failed on the credit leg, after the debit: money may have moved |

#### Unmapped — `LEGAL_STOP` (1)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `payment_stopped_by_court_order` | Payment processing failure at remitter bank. Please ask customer to try with another bank account. | — | a court order stops the account; no automated request should go out |

#### Unmapped — `CAP` (5)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `limit_exceeded_remitting_bank` | Limit exceeded for remitter bank. Please ask customer to try with another bank account. | — | a limit binds while the instrument works: the same debit succeeds under the limit |
| `mandate_debit_beyond_psp_amount_cap` | Debit amount is beyond payer PSP specified amount cap. Please reduce the amount and try again. | — | a limit binds while the instrument works: the same debit succeeds under the limit |
| `per_transaction_limit_exceeded` | Customer bank per transaction limit exceeded. Please try again with a lower amount. | — | a limit binds while the instrument works: the same debit succeeds under the limit |
| `transaction_frequency_limit_exceeded` | Payment failed. Please try again with another bank account. | GATEWAY_ERROR | a frequency limit binds on the account |
| `transaction_limit_exceeded` | Payment failed because Transaction amount limit has exceeded | BAD_REQUEST_ERROR | a limit binds while the instrument works: the same debit succeeds under the limit |

#### Unmapped — `PAYEE_SIDE` (3)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `invalid_transaction_beneficiary` | Beneficiary address resolution failed. Please try again after some time. | — | the merchant's own side — its address, bank or account — failed; nothing the customer does helps |
| `merchant_error_payee_psp` | VPA resolution into bank account details failed. Please try again after some time. | — | the merchant's own side — its address, bank or account — failed; nothing the customer does helps |
| `transaction_not_permitted_cardholder_beneficiary` | Transaction not permitted in beneficiary account. Please try again with another bank account. | — | the merchant's account does not permit it |

#### Unmapped — `INTEGRATION` (7)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `amount_does_not_match_mandate_amount` | The payment failed as the amount does not match the amount provided at the time of mandate creation. | — | the debit breaks the mandate's registered rules: the merchant's schedule or amount is wrong |
| `bad_request_error` | Invalid Mandate Sequence Number. | — | 'Invalid Mandate Sequence Number': the merchant's request is wrong |
| `execution_day_rule_mismatch` | Day of debit does not match the debit execution rule for the payer. Please ensure execution day matches the execution rule. | — | the debit breaks the mandate's registered rules: the merchant's schedule or amount is wrong |
| `id_value_must_be_present` | Failed to debit customer's bank account. Mandate details are incorrect. | — | 'Mandate details are incorrect': the merchant's request is wrong |
| `payer_seqnum_validation_failure` | Payer sequence number length validation failed. | — | a malformed or mismatched message: an engineer's fault, not the customer's |
| `regid_details_must_be_present` | Gateway validation failure. Please try after sometime or create a new mandate. | — | 'Gateway validation failure': the request is malformed |
| `seqnum_mismatch_payer_psp` | Sequence number mismatch between payer and payee PSP. Please try again after some time. | — | a malformed or mismatched message: an engineer's fault, not the customer's |

#### Unmapped — `UNDESCRIBED` (1)

| Reason | Razorpay's description | Code | Why |
|---|---|---|---|
| `no_original_request_found` | No mandate details were found in the record during debit. Please try after some time. | — | 'No mandate details were found in the record during debit': whose record, the page does not say |

<!-- upi-table:end -->
