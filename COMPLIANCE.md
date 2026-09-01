# COMPLIANCE — the thirteen guards, and what each one actually rests on

Every guard maps to a rule. Some of those rules are statute, some are platform
constraint, and some are **my judgement wearing a statute's clothes** — the
distinction matters more than the mapping, so it is the first column.

Nothing here is a legal opinion. This is a prototype built against public
documentation by someone who is not a lawyer, and the honest reading of the
table below is *"here is where a compliance reviewer should start arguing"*
rather than *"here is a system that is compliant."*

## How the chain works

Four properties, and each exists because the obvious alternative is wrong:

1. **All thirteen evaluate, then resolve by severity.** Not short-circuit on the
   first refusal. A cheapest-first chain returns one clause; this returns every
   clause that was violated, which is what a receipt needs to be evidence.
   Ablation **A4** measures whether that mattered: it runs the same guards in
   short-circuit order and its receipts are visibly poorer.
2. **Gating happens at execute time, not propose time.** A proposal built at
   19:30 and deferred to 08:09 is gated *again* on wake — because consent can be
   withdrawn and the payment can settle in between. Attack **A04** is the whole
   argument for this and it survives.
3. **Guards are pure functions.** No I/O, no clock except `ctx.now`. They are
   property-tested with Hypothesis, and a guard that reached for the network
   would break replay determinism, which `tests/test_replay.py` asserts.
4. **A refusal is recorded as loudly as an action.** `BLOCKED` and `ESCALATED`
   are first-class receipts in the same hash chain as `EXECUTED`. An agent that
   silently does nothing and an agent that correctly declines are
   indistinguishable unless the ledger says which happened.

## The thirteen

| # | Guard | Rests on | What it does |
|---|---|---|---|
| G01 | `IdempotencyGuard` | **Platform constraint** — Razorpay delivers every webhook at least twice (`docs/VERIFIED.md`) | One execution per (payment, intervention, attempt, role). Not defensive; required. |
| G02 | `RiskBlockGuard` | **Card network norms** on retrying declined authorisations | A risk-declined payment gets nothing automated, ever. Straight to a human queue. |
| G03 | `ConsentGuard` | **DPDP Act 2023 s.6** + DPDP Rules 2025 | No processing without consent; on withdrawal, blocks *and* purges work already queued for that customer. |
| G04 | `RetryCapGuard` | **Platform constraint** — Razorpay halts a subscription after 4 consecutive failures | Caps attempts below the halt. |
| G05 | `PromiseToPayGuard` | **RBI Fair Practices Code** (fair dealing) | A customer who promised a date is not chased before it. Has no jurisdiction over `HUMAN_QUEUE`. |
| G06 | `DNDGuard` | **TRAI TCCCPR 2018**, as amended Feb 2025 | No promotional traffic to a DND-registered number. |
| G07 | `FrequencyCapGuard` | **RBI FPC** (anti-harassment) | ≤2 contacts per episode; ≤3 per customer per rolling 7 days. |
| G08 | `ContactWindowGuard` | **RBI FPC ¶55** | No contact outside 08:00–19:00 in the customer's zone, IST when unknown. Defers rather than blocks, with a per-customer jitter. |
| G09 | `PreDebitNoticeGuard` | **RBI e-mandate framework** — pre-debit notification | A mandate debit is held until a notice has been served, 24h ahead. |
| G10 | `AFAThresholdGuard` | **RBI e-mandate framework** — AFA above ₹15,000 | A recurring debit over the threshold needs additional factor authentication. |
| G11 | `DLTTemplateGuard` | **TRAI TCCCPR** — DLT template registration (Feb 2025 amendment) | Every message carries a template the merchant actually registered. |
| G12 | `SpendCapGuard` | **Merchant policy** — ours, not anyone's regulation | A per-merchant daily ceiling on money moved, plus a re-check of retry quiet hours at final gating. |
| G13 | `HumanApprovalGuard` | **Operational policy** — ours | The execution handoff. Nothing automated proceeds where a human is required. |

Four of the thirteen — G01, G04, G12, G13 — carry **no statute at all**, and the
code says so: their `statute` attribute is `None`. They are platform constraints
and house rules. Listing them beside the statutory ones without that distinction
would be the easiest and most dishonest way to make this table look stronger
than it is.

## Where this is uncertain, and by how much

The working agreement for this project is that an unverified regulatory
threshold gets a `# VERIFY:` comment in the code rather than a confident
assertion. **There are 33 of them.** The ones that bear on compliance directly:

- **`ContactWindowGuard` — "¶55" is unconfirmed.** The paragraph number comes
  from the design spec's research and was never checked against the current
  Fair Practices Code. The 08:00–19:00 window is well attested; the citation
  for it is not. *This is the single most load-bearing unverified string in the
  repository*, because it appears on every deferral receipt.
- **`ContactWindowGuard` — the window is now enforced in the customer's own
  timezone.** It was enforced in IST, the merchant's, until 2026-08-30, and
  adversary attack **A08** demonstrated the consequence: a contact landing at
  22:30 customer-local. Fixed, and A08 now survives. The fallback when no zone
  is known is still IST, which is every customer the simulator builds — so this
  protects customers we have a zone for and leaves the rest where they were.
- **`PromiseToPayGuard` — the Fair Practices Code's applicability to a
  payment-gateway integration is assumed, not established.** The FPC governs
  regulated lenders. Whether it reaches a merchant's recovery agent is a
  question I could not answer.
- **`AFAThresholdGuard` — ₹15,000 is the general limit.** The design spec's
  research notes higher limits for specific categories such as insurance
  premiums; those are not implemented.
- **`RiskBlockGuard` — the card networks' retry rules are referenced
  second-hand**, through Razorpay's and the networks' public documentation, not
  from the network rulebooks.
- **`RetryCapGuard` — the 4-retry halt is documented and was never observed** on
  this account. Subscriptions are unavailable pre-activation, so it could not
  be exercised even once.
- **`DNDGuard` scopes to promotional traffic only.** Whether a payment-recovery
  message is transactional, service or promotional under TCCCPR is a
  classification I made and flagged; adversary attack **A09** exploits the gap
  and is open.
- **`MAX_DEFERRALS = 5` and `DEFER_HORIZON = 7 days` are judgement, not
  statute**, and their docstrings say so.

## What is actually proven

Distinct from what is *claimed*. The following are ledger scans over 1,000
seeded universes, and they are the §2a safety predicate:

| Claim | Result |
|---|---|
| No message sent outside 08:00–19:00 IST | **0 violations** |
| No message on an unregistered DLT template | **0** |
| No automated action on a `RISK_BLOCK` episode | **0** |
| No action after consent withdrawal | **0** |
| No retry on `INSTRUMENT_DEAD` beyond the single documented probe | **0** |
| Contacts ≤2 per episode, ≤3 per customer per 7 days | **0 breaches** |
| Every money action has a hash-chained receipt | **True** |
| Every receipt id unique across the run | **Equal** |

Held on **1,000 of 1,000 seeds**, with `pass^k = 1.0` at every registered k up
to 100. The baseline `retry_plus_contact` satisfies this predicate on **0 of
1,000**.

Two caveats a reviewer should hold onto. First, these scan what the agent *did*
against the rules **as I implemented them** — a guard that encodes the wrong
threshold passes its own scan perfectly. Second, two of the eight claims key on
the classification the arm assigned itself; `windtunnel/metrics.py` adds three
world-keyed counters beside them precisely so that an arm which declines to
classify cannot satisfy them vacuously.

## What is not covered

- **PCI-DSS.** No card data is stored, handled or transmitted by this system —
  Razorpay holds it. That is an architectural property, not a certification.
- **GST, TDS, and Section 43B(h)** receivables framing. Out of scope; noted in
  the design spec as an optional extension that was not built.
- **Grievance redressal and the ombudsman route.** RBI's FPC requires an
  escalation path for customer complaints. `HUMAN_QUEUE` is where the agent
  stops; what a human does next is not modelled.
- **Data retention and erasure.** DPDP grants erasure rights. The ledger is
  append-only and hash-chained, and those two requirements are in genuine
  tension. This project does not resolve it, and a production system would have
  to.
- **Any live compliance validation whatsoever.** Every figure in this repository
  comes from a simulator. Nine of the ten error reasons the taxonomy classifies
  have never been observed on a real payment.
