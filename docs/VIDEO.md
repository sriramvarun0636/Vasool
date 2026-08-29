# Video script — 5 minutes, shot by shot

Design spec §16 stage 12: *"Video, rehearsed 8×. Graded most."* This is the
script to rehearse against. Timings are targets, not limits; the only hard rule
is that **the loss goes in the first thirty seconds**, because everything after
it is more persuasive once the audience knows you are not selling.

Three lines are marked **VERBATIM**. The spec asks for two of them by name; the
third is the one that makes the result honest. Say them exactly.

---

## 0:00 — 0:30 · Open with the loss

**On screen:** the dashboard hero, then Exhibit A's three cards side by side.

> "This is a revenue-recovery agent for Razorpay. Over a thousand seeded
> universes it recovers **49.1%** of failed payments. The realistic incumbent —
> retry everything, then send a link — recovers **65.4%**.
>
> So it loses by sixteen points. I registered that as falsification criterion
> F1 before I ran anything, and I'm opening with it, because the interesting
> part is what the sixteen points bought."

**Beat.** Let it sit. Nobody else in the competition opens by losing.

---

## 0:30 — 1:10 · What the gap bought

**On screen:** the three cards, cursor moving down the fine print on each.

> "The incumbent satisfies the safety predicate on **zero** of a thousand seeds.
> Vasool: a thousand out of a thousand. `pass^100` is 1.0 — every one of a
> hundred independent worlds clean, not an average that hides the bad ones.
>
> Look at what the incumbent does to earn those points. Twenty thousand nine
> hundred and eighty-eight automated actions on **risk-declined** payments.
> Sixty-six thousand retries on a class the taxonomy prices at zero attempts.
> Two hundred ninety-two thousand retries on cards that are already dead.
>
> It isn't a better agent that scores higher. It's an agent that can't be
> deployed, scoring higher *because* of the actions that make it undeployable."

---

## 1:10 — 1:50 · The one rule worth thirty seconds

**On screen:** `make demo --scenario payment_risk_check_failed`, live in a
terminal. Let it print. Land on the `HUMAN_QUEUE` line.

*(Spec §4, note on `payment_risk_check_failed`: "This one rule is worth thirty
seconds of your video.")*

> "A risk-declined payment gets nothing. No retry, no link, no SMS — it goes
> straight to a human.
>
> That's not a technical constraint. Retrying it would probably work sometimes.
> It's that if the network declined a payment for suspected fraud, the person on
> the other end may be a victim, and an automated system chasing them for money
> is the single worst thing this software could do. So the rule is: hard stop,
> human queue, zero outbound.
>
> And the receipt records the refusal, hash-chained, with the clause. Because an
> agent that correctly does nothing and an agent that's broken look identical
> unless the ledger says which one happened."

---

## 1:50 — 2:30 · The air gap · **VERBATIM**

**On screen:** the mermaid diagram from `ARCHITECTURE.md`, then
`vasool/diagnosis/llm.py` and the `LLMVerdict` type.

> **"You did not use an LLM to decide whether money moves. You used a state
> machine, because money movement must be replayable, and a stochastic planner
> is not."** ← *VERBATIM, spec §1*

> "The LLM has no tools. It emits an `LLMVerdict`. The policy plane consumes a
> `Proposal`. Those are different types and there is **no function anywhere in
> this repository that converts one to the other** — so for the model to move
> money, someone would have to write code that doesn't exist. A test walks the
> import graph in both directions to keep it that way.
>
> That's the answer to prompt injection too. Attack A10 puts an injection in a
> customer's name field. It fails structurally — the model can only emit values
> from a closed enum, and the guards run downstream regardless of what it says.
> Not a filter. A design."

---

## 2:30 — 3:10 · The bug the simulator found

**On screen:** `POSTMORTEM.md` INC-002, then the before/after table.

> "Here's the one I'd want you to ask about.
>
> Every test passed. Thirteen hundred and fifty-three of them. Safety predicate
> clean on a thousand seeds. And a third of the population was doing nothing at
> all.
>
> The pre-debit notice guard holds a mandate debit until a notice is sent, and
> returns an obligation to send one. But obligations were only read on the
> *execute* path — and a deferred proposal doesn't execute. So no notice was
> ever built, so the guard deferred again, forever. The one thing that could
> satisfy the guard was an execution the guard was blocking.
>
> Zero of seven hundred and seven retries landed on the two hundred seventy-five
> mandate episodes. No error. No failing test. Nothing unsafe happened — nothing
> happened at all.
>
> Fixing it moved the headline recovery rate from **0.344 to 0.491**, and it
> moved F5 — my registered criterion for whether compliance is affordable — from
> 19.4 to 4.7 against a threshold of 20. So three quarters of what I'd been
> calling *the price of the guards* was this bug. It had been sitting six tenths
> of a point from firing a criterion, for a reason that had nothing to do with
> compliance."

---

## 3:10 — 3:50 · Falsification and the sweep

**On screen:** Exhibit B3 (the F1–F7 board), then Exhibit B2 (the sweep grid).

> "Seven criteria, thresholds fixed before any run. None fired — but read F1's
> row. It didn't fire because F1 as written fires when the interval *includes*
> zero, and mine excludes it. On the wrong side. The artifact flags that in its
> own `detail` field, because `fired: false` there is worse news than firing.
>
> Eight of my nine outcome parameters are guesses — labelled `[guess]` in the
> simulator's source, where a parameter with no provenance tag fails a test. So
> every one gets swept ±50%, eighty-three configurations, and every comparison
> re-tested in all of them.
>
> A3 fails in all eighty-three. That's not a parameter effect — its reference
> interval at two hundred seeds already includes zero, so it fails because the
> reference was never conclusive at that depth. I registered that as a limit
> instead of quietly dropping the row, and it pushes F6 *toward* firing, which
> is the conservative direction."

---

## 3:50 — 4:20 · Provenance, live

**On screen:** click **trace every number**. Let all fifty paths appear at once.
Then Exhibit E — paste a receipt id, hit verify, show the SHA-256 match.

> "Every figure on this page shows the exact key it came from in the manifest.
> Fifty of them. A value the manifest doesn't carry renders as a dash, never as
> a plausible number — and a test fails the build if anyone reintroduces a
> fallback, because I found a hardcoded constant in here rendering as a
> measurement and that's in the postmortem too.
>
> And this recomputes the receipt hash in your browser, from the exact bytes it
> was signed over. You don't have to trust the page."

---

## 4:20 — 4:45 · Honest limitations · **VERBATIM**

**On screen:** `docs/EVALUATION.md` §11, and `COMPLIANCE.md`'s uncertainty list.

> **"These outcome probabilities are calibrated to published benchmarks, not
> observed on live traffic. No student has live merchant data. So the number I'm
> reporting is not '₹X was recovered.' It is: under a stated, sensitivity-tested
> outcome model, this policy beats the control by X, and the direction of that
> result is robust to ±50% error in every parameter. The methodology is the
> deliverable. Plug in real traffic and the same harness gives you a real
> number."** ← *VERBATIM, spec §7.5*

> "Two more. Nine of my ten error reasons are simulated — Razorpay test mode
> reproduces exactly one, no matter which documented error-scenario card you
> use, and that's in VERIFIED.md with the evidence.
>
> And `card_declined` I'm genuinely unsure about. Some issuers use it for soft
> declines, some for hard. I treat it as dead after one retry. That's a
> judgement call on ambiguous evidence and I'd change it tomorrow given real
> decline data." ← *spec §4 asks for this uncertainty by name*

---

## 4:45 — 5:00 · Close

**On screen:** `make redteam` running live. `18 / 22 survived`, then the four
named failures.

> "Twenty-two attacks. Eighteen survive. Four are open and named — out-of-band
> settlement, an identity split, a timezone, and a DND classification gap. I
> could have written twenty-two attacks it passes. These are the ones it
> doesn't.
>
> The claim isn't that this agent is correct. There are six incidents in the
> postmortem that say otherwise. The claim is that **the apparatus is built so
> that being wrong is discoverable** — and the evidence for that is how long
> that list is, and how much of it the apparatus found instead of me."

---

## Rehearsal notes

- **The loss is the hook.** If you soften it in take three because it feels bad
  to open by losing, start again.
- **Run the terminal commands live.** `make demo` and `make redteam` both
  complete fast enough. Pre-recorded terminal reads as fake, and this whole
  submission is about the difference.
- **Don't explain the architecture before the result.** Judges who have watched
  forty of these will thank you.
- **Numbers to say precisely, from the manifest:** 49.07 / 65.42 / −16.35pp /
  1,000 of 1,000 / pass^100 = 1.0 / 4.74pp of a 20pp threshold / 18 of 22.
  Everything else can be rounded out loud.
- The three **VERBATIM** blocks are the ones to over-rehearse. Everything else
  should sound like you're thinking, because you are.
