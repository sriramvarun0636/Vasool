# POSTMORTEM — what broke, and how I got out

Six incidents. Each one is recorded somewhere else in this repository as well —
in `docs/EVALUATION.md` §10's append-only amendment log, in `docs/taxonomy.md`
§9's known limits, or in `docs/VERIFIED.md` — and the cross-reference is given
so that nothing here rests on my summary of it.

Four of these were found by the system catching itself rather than by me
noticing. Those are the four worth reading.

---

### INC-001 — Razorpay's documented error-scenario cards do not produce their documented errors

**Symptom.** Day one, building the failure taxonomy against live test mode. Every
card in Razorpay's "Error Scenario" table — the ones documented to produce
`card_expired`, `insufficient_fund`, `payment_risk_check_failed` — returned the
same thing:

```
payment_failed / BAD_REQUEST_ERROR / gateway / payment_authorization
```

**Investigation.** Tried both delivery paths, Payment Links and Checkout.js.
Tried every documented card. Identical envelope every time. The four error fields
that the entire taxonomy keys on collapse to one tuple in test mode.

**Root cause.** Test mode does not simulate issuer-side decline reasons. It
simulates *a decline*. The reason strings exist in the API contract and in the
documentation; they are not reachable without a live merchant account and real
failing cards, which an unactivated account does not have.

**Fix.** Split the payload corpus in two, permanently and visibly.
`data/observed_payloads/` holds what was actually captured. `data/stubbed_payloads/`
holds hand-built envelopes, and **every one of them carries `_SIMULATED: true`**.
The demo prints the provenance of its own input before it does anything else:

```
provenance   : SIMULATED stub payload
```

`docs/VERIFIED.md` records which is which and why. The evaluation states the
consequence in its own headline terms: **nine of ten error reasons are
documentation-derived, not observed**, and that sentence appears in
`docs/EVALUATION.md` §11 and on the report card, not in a footnote.

**What I'd do differently.** I planned three days of taxonomy work on the
assumption that I could observe the reasons I was classifying. I should have
spent the first two hours triggering one card and reading what came back, before
writing a single line of the taxonomy. The two-directory split is the right
answer and I would keep it — but I would have reached it on day one instead of
day two, and the day I lost was the most expensive day in the project.

---

### INC-002 — The pre-debit notice was never sent, so no mandate debit ever executed

**The flagship incident. The simulator found it, not me.**

**Symptom.** None, for a long time. Every test passed — 1,353 of them. The safety
predicate held on 1,000 of 1,000 seeds. No guard misbehaved. No receipt was
missing. The system was, by every check I had built, correct.

What was wrong was that a third of the population was quietly doing nothing.

**Investigation.** Found while writing an adversary attack (A23) that turned out
to be inert — it could not fail, because the thing it was attacking never
happened. Measuring seed 0 directly:

| | measured |
|---|---|
| episodes | 888 |
| …on an e-mandate | 275 |
| retries executed across the whole run | 707 |
| …of them on a mandate episode | **0** |
| mandate episodes ending `BLOCKED` | **209 / 275** |

Zero. Not "few". Thirty-one percent of the population had a retry ladder that
never fired once.

**Root cause.** A deadlock, and a perfectly circular one.
`PreDebitNoticeGuard` holds a mandate debit until a notice has been served and
returns `DEFER` carrying an `Obligation(SEND_PRE_DEBIT_NOTICE)`.
`PolicyMachine._execute` was the only place obligations were read. A deferred
proposal does not execute — so no notice was ever built, so
`pre_debit_notice_sent_at` stayed `None`, so the guard deferred again. Five
times, and then `MAX_DEFERRALS` blocked it for good.

**The one thing that could satisfy the guard was an execution the guard was
blocking.**

This is a *liveness* failure, and it is the only one in `docs/taxonomy.md` §9 —
every other known limit is about something the agent might wrongly do. This one
is the agent correctly refusing, forever, an action it was supposed to take. No
safety check can catch it, because nothing unsafe happens. Nothing happens at all.

**Fix.** Obligations are honoured on the deferral path: `_defer` now calls
`_honour`, and the dead loop in `_execute` is gone. `_honour` runs *after*
`_defer`'s `MAX_DEFERRALS` and `DEFER_HORIZON` bounds — warning a customer about
a debit we have just declined to reschedule would be its own defect.

| seed 0, full universe | before | after |
|---|---|---|
| pre-debit notices executed | 0 | **196** |
| retries executed | 707 | **979** |
| …on a mandate episode | 0 | **272** |
| mandate episodes ending `BLOCKED` | 209 / 275 | **30 / 275** |

**It had been shaping every number published up to that point.** Vasool's mean
recovery rate went **0.344341 → 0.490698**. F5 — the registered criterion for
"compliance is unaffordable", measured as the gap to the ungated arm — went
**19.378 → 4.742** against a threshold of 20. So roughly **three quarters of what
I had been calling the price of the guards was this bug**, and F5 had been
sitting 0.6 points from firing for a reason that had nothing to do with
compliance. Against `naive_retry`, Vasool went from −0.122 to +0.025: a flipped
conclusion.

Recorded in `docs/EVALUATION.md` §10 under 2026-08-25 and in `docs/taxonomy.md`
§9.13 with the full before/after.

**What I'd do differently.** Every test I had asked "did the agent do something
wrong?" Not one asked "did the agent do anything at all?" A guard that returns
`DEFER` forever is indistinguishable from a guard that is working, if the only
question you know how to ask is whether a violation occurred. I would add a
liveness class of assertion — *this population should produce actions; how many
did it produce* — alongside the safety ones, from the beginning. The simulator
found this because it was the first thing that ran the agent at a scale where
"nothing happened" was visible as a number.

---

### INC-003 — The re-run that didn't run, and finished in 5.5 seconds

**Symptom.** Immediately after fixing INC-002, I re-ran the full evaluation to
get post-fix numbers. Nine arms, one thousand seeds. It finished in **5.5
seconds** and reported a complete set of results.

**Investigation.** The base protocol takes about twenty minutes. Five and a half
seconds is not a fast run; it is not a run. The evaluator resumes by reading
which seeds are already present in each shard file and computing only the
missing ones — and every seed was already present, from *before* the fix. It
re-emitted a thousand pre-fix rows and labelled the result a post-fix evaluation.

**Root cause.** The resume is justified by architectural invariant 5 — same seed,
byte-identical ledger. That invariant holds for a *fixed* agent. It says nothing
across a change to the agent, and **a shard carries no fingerprint of the code
that produced it**.

**Fix.** The stale shards were preserved rather than deleted, the incident was
written into `docs/EVALUATION.md` §10 the same day, and the run was redone
against cleared shards. A content fingerprint on each shard is registered there
as outstanding work, not as done.

**This is the mirror of the failure §3c exists to prevent.** That section is
built to stop a *silent re-run* — quietly trying again until the numbers
improve. What happened here was a silent *non*-run, and the only thing that
surfaced it was an elapsed time too implausible to ignore. Had the base protocol
taken 5 seconds legitimately, I would have published pre-fix numbers under a
post-fix headline and never known.

**What I'd do differently.** Cache invalidation is not an optimisation detail
when the cache holds evidence. Any artifact that can be resumed needs to record
what produced it — I would put a hash of the agent's source tree in every shard
before I wrote the resume logic, not after it burned me.

*(Postscript: four days later this recurred in a form I could check. A resume
finished in 16.1 seconds and produced byte-identical values. That is also exactly
what a stale resume looks like — so this time I recomputed the entire base
protocol into a scratch directory and compared: 9,000 rows, 207,000 field
comparisons, byte-identical. The evidence, not the plausibility, is what settled
it. §10, 2026-08-29.)*

---

### INC-004 — A queued retry outlived the diagnosis that built it

**Symptom.** Four adversary attacks — A15, A16, A18, A19 — all survived the
survival criterion by doing things that were obviously wrong. The cleanest is
A16: a card expires between attempt 2 and 3. The agent correctly classifies it
`INSTRUMENT_DEAD`, correctly sends a re-auth link — and then, thirty minutes
later, a `SILENT_RETRY` queued from the *earlier* benign failure re-presents the
expired card. §5's flagship zero, spent anyway.

**Investigation.** The policy plane re-reads the *world* on every gate — consent,
contacts already sent, whether the payment settled. That is the right design and
it is argued at length in `vasool/policy/machine.py`. What it never re-read was
the **classification**. A new event for the same episode mints a new proposal and
retires nothing; the old proposal sits on the queue carrying the old row's
`failure_class` and gates on its own terms when its time comes.

**Root cause, and the part that stings:** `EVALUATION.md` §2a's two class-keyed
safety claims — no automated action on a `RISK_BLOCK` episode, no retry on an
`INSTRUMENT_DEAD` classification — both key on `Proposal.failure_class`, which on
a stale proposal is the **old label**. So A15 and A16 executed exactly the actions
those two rows forbid, and both rows still passed. The ledger scan could not see
it.

**Fix.** `PolicyMachine.observe()` retires queued proposals when a later failure
changes the reason or source that produced them, and the transition log records
each supersession. `SpendCapGuard` re-checks quiet hours at final gating.
`PromiseToPayGuard` has no jurisdiction over `HUMAN_QUEUE`, so a risk handoff is
immediate rather than delayed by a day and a half. All four attacks now survive
— verified by an actual run, `18 of 22`, recorded in §10 on 2026-08-29.

**What I'd do differently.** I had already fixed this exact pattern one plane up
and did not recognise it the second time. `rules.py`'s docstring records the
first instance: a quiet-hours hold applied at classify time to a path no rule
governed. The lesson both times is the same — **a decision computed at time T and
applied at time T+n is a bug unless something re-checks it at T+n** — and I would
now treat "what is re-evaluated on wake, and what merely persists" as a property
the state machine has to state explicitly rather than one you infer by reading it.

---

### INC-005 — A hardcoded constant rendering as a measurement

**Symptom.** Reading the dashboard's source during a review, this line:

```js
let riskActions = EVAL?.per_arm?.naive_retry?.risk_block_actions_world || 18541;
```

**Investigation.** `18541` happens to be the correct measured value. That is what
makes it dangerous rather than merely wrong: if the artifact ever stopped
carrying the field, or the arm legitimately measured zero, the page would render
a hardcoded number as a measurement — with no warning, because the fallback
banner was only raised when `per_arm` was missing entirely.

The same review turned up an untracked script, `scratch/patch_customer_action.py`,
which opened the published evaluation artifact and wrote
`customer_action_retries_world = 142583` into it. That number was invented. It
had never been measured by anything. The script was inert only because the
artifact had been regenerated after it last ran.

**Root cause.** The report card was the only part of the system with no
provenance discipline and no tests. Every other artifact in this project has to
justify its numbers; the page that *displays* them did not.

**Fix.** Three things. The script was deleted. Every figure on the dashboard now
reaches the DOM through a single `trace()` helper that stamps it with the exact
manifest key it came from — and a toggle reveals all fifty at once, so a reader
can check the page against `evaluation.json` without leaving it. A missing value
renders as a dash and raises the banner; it never renders as a plausible number.

And it is now enforced. `tests/test_report.py` fails the build if a `|| <number>`
fallback is reintroduced on any expression reading from the manifest, and a
second test fails if the README quotes a percentage the manifest does not
support.

**What I'd do differently.** I would have written the provenance test before the
report card, for the same reason the policy plane got tests before it got guards.
The presentation layer was treated as decoration for most of this project, and
it is the only layer a judge actually reads.

---

### INC-006 — The page that rendered blank, with zero console errors

**Symptom.** After adding provenance mode, the dashboard rendered with every
figure fallen back to a dash. The forest plot was gone. The hero counter read
`0`. The browser reported **no errors at all.**

**Investigation.** An error listener caught nothing. Instrumenting further:

```
forestMarkers=0  traced=0  hero=0  errors=[]
```

The whole render handler had not run, silently. Adding an `unhandledrejection`
listener produced it immediately:

```
REJECT ReferenceError: Cannot access 'traced' before initialization
```

**Root cause.** I had placed the `trace()` helper *below* the first code that
called it. `trace` is a hoisted function declaration, but the `const traced = []`
it closes over is not — it sits in the temporal dead zone. The throw happened
inside an `async` DOMContentLoaded handler, so it surfaced as an unhandled
**promise rejection** rather than an error event, which is why `window.onerror`
saw nothing and the page failed quietly instead of loudly.

**Fix.** Moved the declaration above its first call site — and, more usefully,
added a test that asserts the ordering, since this is not a mistake a human
reliably catches by reading:

```python
earliest_call = min(m.start() for m in re.finditer(r"(?<![.\w])trace\(", SOURCE))
assert earliest_call > SOURCE.index("const traced = [];")
```

I verified the test fails when the bug is reintroduced.

**What I'd do differently.** This is the cost of the presentation layer being
1,581 lines of HTML, CSS and JavaScript inside a Python f-string with 590
escaped brace pairs — no syntax highlighting, no linter, no type checker, nothing
that would have flagged it. The same file is where I typed a full-width `］`
instead of `]` earlier the same day and caught it only by eye. **That file should
be a Jinja2 template, and Jinja2 is already a declared dependency of this
project.** It is the largest piece of known, named, unpaid technical debt in the
repository and it is recorded as such rather than quietly left.

---

## The pattern across all six

Four of these — INC-002, INC-003, INC-004, INC-006 — share a shape: **the system
was silent about being wrong.** No exception, no failing test, no violated
invariant. A deferral loop that never terminates, a cache that returns stale
evidence, a proposal carrying an expired label, a promise rejection nobody
listened for.

Every one was caught by an artifact built to be *checkable* rather than by
someone noticing: a simulator that ran the agent at a scale where "nothing
happened" became a number, an elapsed time too short to be real, an adversary
whose verdict is scanned from the ledger rather than reported by the code under
test, and a diagnostic that had to be added before the failure would speak.

That is the argument this project is actually making. Not that the agent is
correct — I have six incidents here that say otherwise, and three known
adversarial failures still open in the README. The argument is that **the
apparatus is built so that being wrong is discoverable**, and the evidence for
that is the list above: it is long, it is specific, and most of it was found by
the apparatus rather than by me.
