# COMPLIANCE — the fifteen guards, and what each one actually rests on

Every guard maps to a rule. Some of those rules are statute, some are platform
constraint, and some are **my judgement wearing a statute's clothes** — the
distinction matters more than the mapping, so it is the first column.

Nothing here is a legal opinion. This is a prototype built against public
documentation by someone who is not a lawyer, and the honest reading of the
table below is *"here is where a compliance reviewer should start arguing"*
rather than *"here is a system that is compliant."*

## How the chain works

Four properties, and each exists because the obvious alternative is wrong:

1. **All fifteen evaluate, then resolve by severity.** Not short-circuit on the
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

## The fifteen

| # | Guard | Rests on | What it does |
|---|---|---|---|
| G01 | `IdempotencyGuard` | **Platform constraint** — Razorpay delivers every webhook at least twice (`docs/VERIFIED.md`) | One execution per (payment, intervention, attempt, role). Not defensive; required. |
| G02 | `RiskBlockGuard` | **Card network norms** on retrying declined authorisations | A risk-declined payment gets nothing automated, ever. Straight to a human queue. |
| G03 | `ConsentGuard` | **DPDP Act 2023 s.6** + DPDP Rules 2025 | No processing without consent; on withdrawal, blocks *and* purges work already queued for that customer. |
| G04 | `RetryCapGuard` | **Platform constraint** — Razorpay halts a subscription after 4 consecutive failures; for UPI Autopay, **NPCI OC-215A/2025-26** row 5 — 1 attempt and 3 retries per sequence number | Caps attempts below the halt, and at three retries on a UPI Autopay mandate. |
| G05 | `PromiseToPayGuard` | **RBI Fair Practices Code** (fair dealing) | A customer who promised a date is not chased before it. Has no jurisdiction over `HUMAN_QUEUE` or a status check — asking the rail whether a customer was charged is not chasing them. |
| G06 | `DNDGuard` | **TRAI TCCCPR 2018**, as amended Feb 2025 | No message to a DND-registered number unless the merchant has declared its template transactional or service. An undeclared category is judged like promotional, and a registry that cannot answer blocks. |
| G07 | `FrequencyCapGuard` | **RBI FPC** (anti-harassment) | ≤2 contacts per episode; ≤3 per customer per rolling 7 days. |
| G08 | `ContactWindowGuard` | **RBI FPC ¶55** | No contact outside 08:00–19:00 in the customer's zone, IST when unknown. Defers rather than blocks, with a per-customer jitter. |
| G09 | `PreDebitNoticeGuard` | **RBI E-mandate Framework, 2026 §6(a)** — "at least 24 hours prior to the actual charge / debit" | A mandate debit is held until a notice at least 24h old is on record. The notice is the issuer's: Vasool only requests it through the rail (`vasool/actions/notice.py`), so no rule about a merchant's messages applies to it. |
| G10 | `AFAThresholdGuard` | **RBI E-mandate Framework, 2026 §8(a)–(b)**; NPCI OC-151A/2023-24 | A recurring debit over its category's limit — ₹15,000, or ₹1,00,000 for insurance premiums, mutual-fund subscriptions and credit-card bills — needs additional factor authentication, so it goes to a human. |
| G11 | `DLTTemplateGuard` | **TRAI TCCCPR** — DLT template registration (Feb 2025 amendment) | Every message carries a template the merchant actually registered. |
| G12 | `SpendCapGuard` | **Merchant policy** — ours, not anyone's regulation | A per-merchant daily ceiling on money moved, plus a re-check of retry quiet hours at final gating. |
| G13 | `HumanApprovalGuard` | **Operational policy** — ours | The execution handoff. Nothing automated proceeds where a human is required. A status check is exempt: it reads the rail and moves nothing. |
| G14 | `MandateStateGuard` | **RBI E-mandate Framework, 2026 §4** — registration only after AFA; withdrawal "at any point of time"; NPCI codes VA, VT, VU | No debit against a mandate that is not live when it would execute — paused, revoked, expired or not yet authenticated. Blocks, a pause included: waiting one out would collect the debit it was for. |
| G15 | `AutopayPeakHoursGuard` | **NPCI OC-215A/2025-26** — row 5(b), and ¶3's peak hours | No UPI Autopay execution inside 10:00–13:00 or 17:00–21:30 IST. Defers to a minute past the window, spread by a per-payment jitter because row 5(a) asks for moderated TPS. Card mandates are not NPCI's and are untouched. |

G14 and G15 joined on 2026-09-15 and run where `vasool/policy/registry.py` places
them; the numbers are identifiers, not positions, so every earlier reference to
a G-number still means what it meant. Every clause the mandate guards and the
mandate lifecycle rest on is quoted verbatim in `vasool/mandate/citations.py`,
from documents pinned by the SHA-256 of the bytes read.

Four of the fifteen — G01, G04, G12, G13 — carry **no statute at all**, and the
code says so: their `statute` attribute is `None`. G04's UPI Autopay cap is
NPCI's rule, and its refusal names the circular in its own reason; the attribute
stays `None` because the guard's other two caps are nobody's rule. They are platform constraints
and house rules. Listing them beside the statutory ones without that distinction
would be the easiest and most dishonest way to make this table look stronger
than it is.

## Where this is uncertain, and by how much

The working agreement for this project is that an unverified regulatory
threshold gets a `# VERIFY:` comment in the code rather than a confident
assertion. **There are 36 of them** — two closed on 2026-09-15 by RBI's E-mandate
Framework, 2026, three opened by the mandate work, and on the UPI failure path
three opened and one closed with the call it described. The ones that bear on
compliance directly:

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
- **`AFAThresholdGuard` — the category tiers are now implemented, from the
  source.** §8(b) of RBI's E-mandate Framework, 2026 raises the limit to
  ₹1,00,000 for insurance premiums, mutual-fund subscriptions and credit-card
  bills. The category is the merchant's declaration; for UPI, NPCI keys the
  tier on merchant category code (OC-151A's Annexure A), which the merchant's
  acquirer verifies and this code does not see.
- **`PreDebitNoticeGuard` — the notice was modelled as the merchant's, and is
  now the issuer's.** §6(a) of the Framework: "An issuer shall send a
  pre-transaction notification". For UPI the payee's PSP requests it through
  NPCI's ReqValCust API, 24 hours ahead (OC-149's annexure, code NU), and the
  PSP and the issuing bank notify the customer (OC-151A ¶4). Until 2026-09-15
  Vasool built it as a merchant SMS and gated it through the contact window,
  DND, DLT and the frequency cap — conservative, never unsafe, and costly: a
  debit waited on a notice those rules refused. It is now a request through a
  port, `vasool/actions/notice.py`, whose default adapter refuses because no
  such request is wired on this account. That default is the honest gap:
  until an adapter exists, production cannot ask for the notice, and a fact
  store that records only requests that succeeded would hold every mandate
  debit here — the safe way for the gap to show.
- **Non-revocable mandates.** NPCI OC-125A/2022-23 removes the payer's revoke
  and pause from loan-repayment and EMI mandates (MCC 7322) created with
  "revokeable" set to "N", routing revocation through the merchant with its
  consent. §4(b) of RBI's 2026 Framework gives the customer withdrawal "at any
  point of time". The lifecycle models NPCI's rule; the tension is recorded,
  not resolved.
- **Resuming a paused mandate rests on platform documentation.** No rule
  document found says how a pause ends; that one transition cites Razorpay's
  API reference (`"action": "pause | unpause"`, with a pause window), and a test
  holds it at exactly one.
- **`RiskBlockGuard` — the card networks' retry rules are referenced
  second-hand**, through Razorpay's and the networks' public documentation, not
  from the network rulebooks.
- **The UPI Autopay failure path rests on Razorpay's documentation, not
  observation.** A Razorpay merchant is sent one of 61 documented reasons, not
  NPCI's code, and none has been seen on this account (`docs/taxonomy.md` §12,
  `docs/VERIFIED.md`). Fifteen say money may have moved; they get a status check
  (NPCI OC-215) and never a retry, and because no status call is wired yet,
  every one of them goes to a person. The documented debit, notice and status
  calls are built behind ports whose defaults refuse, and stay off until one
  live call has been observed.
- **A debit is never re-sent.** Until 2026-09-15 the client re-sent a mandate
  debit up to four times on a gateway error, resting on an idempotency header
  never seen honoured, and a timeout escaped unrecorded (`POSTMORTEM.md`
  INC-010). No guard could see it; it was below the guards. A lost response now
  goes to a status check.
- **`RetryCapGuard` — the 4-retry halt is documented and was never observed** on
  this account. Subscriptions are unavailable pre-activation, so it could not
  be exercised even once. The UPI Autopay cap of three is NPCI's, cited.
- **`DNDGuard`'s scope rests on the merchant's declaration.** Whether a
  payment-recovery message is transactional, service or promotional under
  TCCCPR is decided by how its template is registered on DLT, which only the
  merchant knows. Until 2026-09-15 every message was assumed transactional and
  adversary attack **A09** was open; now an undeclared template is `UNKNOWN`,
  judged like promotional, and A09 survives. A merchant that declares a
  template transactional takes that message out of the guard's jurisdiction,
  and a false declaration is the merchant's. No DND scrub is built — the port
  exists (`vasool/policy/dnd_registry.py`) with a null adapter that blocks.
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
- **e-NACH.** A third mandate rail with its own rules, none of them gathered.
  RBI's 2026 Framework covers cards, PPI and UPI (§2); nothing here models
  NACH, and a simulated netbanking debit on a mandate is held to the card rules.
- **Any live compliance validation whatsoever.** Every figure in this repository
  comes from a simulator. Of the 71 failure reasons the taxonomy classifies —
  ten card reasons and 61 UPI Autopay ones — one has been observed on a real
  payment.
