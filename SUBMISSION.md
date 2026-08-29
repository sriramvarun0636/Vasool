# Submission — copy-paste answers

For the Razorpay AI Buildathon form. Track 03. Everything below is written to
stand alone, because a form field is read without the repo open.

---

## Project name

```
Vasool
```

*("वसूल" — recovery, collection. The thing a merchant does when money owed
hasn't arrived.)*

---

## What it solves

```
Payment failures don't fail in one clean way, and treating them as one
problem is what loses the money. An expired card and a gateway blip look
identical in a webhook and need opposite responses: retrying the expired
card has exactly zero chance of working while burning one of the four
attempts Razorpay allows before it halts the subscription — the attempt a
re-auth link needed.

Vasool detects revenue at risk from payment.failed webhooks, classifies
each failure into one of five classes, chooses the intervention that class
warrants, and executes it through a bounded workflow. Across 1,000 seeded
universes of 500 customers it recovered ₹116.09 Cr, with the §2a safety
predicate holding on 1,000 of 1,000 seeds and zero automated actions taken
on risk-declined payments.

The recovery is the product. What makes it deployable is that thirteen
pure-function guards sit between any proposal and any money movement —
RBI's contact window, TRAI's DLT templates, DPDP consent, the e-mandate
pre-debit notice — and every action, including every refusal, writes a
hash-chained receipt. A blocked action is as visible in the ledger as an
executed one, because an agent that correctly declines and an agent that
is broken look identical otherwise.

The LLM never touches money. It emits an inert verdict object; there is no
code path in the repository that converts it into something executable.
I measured what that costs. Across all twelve cells the LLM gets the failure
class right 66.7% of the time and picks the right action 58.3%. More to the
point: of the two risk-declined cells — where the correct action is to do
nothing — it proposed sending the customer a payment link on one. That is the
argument for where it sits.
```

---

## What broke, and how you got out

> **They say this is the one they read first.** ~320 words, no repo required.

```
The pre-debit notice was never sent, so no mandate debit ever executed —
and nothing told me.

Every test passed. 1,353 of them. The safety predicate was clean on 1,000
seeds. No guard misbehaved, no receipt was missing, no exception was
raised. And a third of my population was quietly doing nothing at all.

The guard that holds a mandate debit until a 24-hour notice has been served
returns DEFER carrying an obligation to send one. Obligations were only
read on the execute path. A deferred proposal doesn't execute — so no
notice was ever built, so the timestamp stayed empty, so the guard deferred
again, five times, and then blocked it for good. The one thing that could
satisfy the guard was an execution the guard was blocking.

I found it while writing an adversarial attack that turned out to be inert:
it couldn't fail, because the thing it attacked never happened. Measuring
directly: of 707 retries executed on seed 0, zero landed on any of the 275
mandate episodes. 209 of them ended BLOCKED. Thirty-one percent of the
population had a retry ladder that never fired once.

The fix was to honour obligations on the deferral path, after the deferral
bounds rather than before — warning a customer about a debit you've just
declined to reschedule is its own defect. 196 notices now execute on seed
0, and 272 of 979 retries land on mandate episodes.

It had been shaping every number I'd published. Recovery went 0.344341 to
0.490698. F5 — my pre-registered criterion for whether compliance is
affordable — went from 19.378 to 4.742 against a threshold of 20. So three
quarters of what I'd been calling "the price of the guards" was this bug,
and a registered criterion had been sitting six tenths of a point from
firing for a reason that had nothing to do with compliance.

Every test I had asked whether the agent did something wrong. Not one asked
whether it did anything at all. That's the lesson: liveness needs its own
assertions, and a guard that defers forever is indistinguishable from a
guard that works.
```

---

## GitHub repo URL

```
https://github.com/sriramvarun0636/Vasool
```

**Must be public before submitting.** Check in an incognito window.

---

## Pitch video

Script with timings and the three verbatim lines: [`docs/VIDEO.md`](docs/VIDEO.md).
Unlisted YouTube is fine. Their guidance is to rehearse eight times.

Non-negotiable beats, in order: open on the money and the zero violations;
the risk-decline rule that gets nothing automated; the air gap verbatim
line; the bug above; F1 excluding zero on the wrong side; the honest-
limitations verbatim line; close on 18 of 22 with the four named failures.

---

## Pre-submit checklist

- [ ] Repo public — verify in incognito
- [ ] `out/holdout/evaluation.json` pushed — it now ships in the repo, and it **cannot be regenerated**: §3c allows the holdout one run and it has been spent. Keep a copy off this machine until the push lands.
- [ ] `pytest` green on a fresh clone — 1,387 pass, **1 skips** (`test_real_captured_signature_verifies`, which needs `RAZORPAY_WEBHOOK_SECRET`). The manifests and cassettes ship, so the drift guards and the cassette pin actually run rather than skipping silently.
- [ ] `docs/index.html` renders on GitHub Pages
- [ ] Video uploaded, link works logged out
- [ ] `.env` **not** committed — `git log --all -- .env` returns nothing
