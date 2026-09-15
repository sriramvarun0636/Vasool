# POSTMORTEM — what broke, and how I got out

Ten incidents. Each one is recorded somewhere else in this repository as well —
in `docs/EVALUATION.md` §10's append-only amendment log, in `docs/taxonomy.md`
§9's known limits, or in `docs/VERIFIED.md` — and the cross-reference is given
so that nothing here rests on my summary of it.

Four of these were found by the system catching itself rather than by me
noticing. Those are the four worth reading — and INC-007, for the opposite
reason: nothing caught it until after v1.0 was tagged; INC-009, which
nothing in the apparatus could have caught, because the rule itself was wrong;
and INC-010, a double debit every record the agent keeps would have shown as one.

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

### INC-007 — The v1.0 tag that did not reproduce on a clean clone

**Symptom.** After v1.0 was tagged, on a fresh checkout with nothing
configured — the state anyone who clones it is in — `pytest` failed eight tests, and
the README's own reproduction command for the LLM comparison died on its first
cell:

```
error: no cassette for provider='gemini' model='gemini-3.6-flash' repeat=0 key=547301f1020a…
coverage: 0 of 12 cells, 0 of 12 classifications recorded
```

On the machine that built v1.0, every test passed.

**Investigation.** Three unrelated faults with one cause.

1. `RazorpayClient.__init__` resolved `RazorpayConfig.from_env()` *before*
   checking whether a client had been injected. Every test in
   `tests/test_razorpay_client.py` injects a fake and none touches the network,
   yet each one demanded credentials — present in my `.env`, absent everywhere
   else.
2. A cleanup commit on 2026-09-01 (`5232bb8`) left one trailing space inside
   the classifier's prompt. A cassette is addressed by sha256 over provider,
   model, repeat and the whole prompt, so all 50 recordings stopped matching at
   once: the tagged prompt appears verbatim in **0 of 50** of them, and in
   **50 of 50** once the space is removed. Every cassette test read the disk;
   none asked the current code for the addresses on it.
3. The evaluation's pepper came from my `.env` and was registered nowhere, and
   §3c's split orders customers by an HMAC keyed on it. So *"the whole artifact
   regenerates from source"* was true on one machine: under any other pepper,
   **0 of 27** recomputed rows match the published shards. No test could see
   this one; it was found by an audit after v1.0 was tagged.

A fourth fault arrived with the fixes. To make `make demo` run with nothing
configured, the Makefile gained `export VASOOL_ID_PEPPER ?= vasool_demo_pepper`
for every target — and `load_dotenv()` never overrides a variable already set,
so from 2026-09-07 every `make` target on my machine silently ran in a different
world under an unchanged fingerprint. It was found before anything was written
under it.

**Root cause.** Nothing in the apparatus ever ran in a stranger's environment.
Every test run, every `make` target and every verification quoted in this
repository ran on a machine with credentials configured, so three
environment-dependent faults were invisible to all of them. This is the one
incident here the system did not catch. A person did, after v1.0 was tagged.

**Fix.** Each fault is closed by a test that fails if it returns: an injected
client must need no credentials (`tests/test_razorpay_client.py`); every
cassette must be addressed by a prompt the code still builds
(`tests/windtunnel/test_cassette_pin.py`); and the pepper is registered in
`windtunnel/pepper.py`, no entry point may read it from the environment, and
seed 0 under it must reproduce the manifest's twelve receipts byte for byte
(`tests/windtunnel/test_pepper.py`; `docs/EVALUATION.md` §10, 2026-09-14). The
Makefile default is gone: a replay falls back to the public test pepper inside
`vasool/demo.py`, and `--live` refuses to. Then the cause itself, rather than
its symptoms: `.github/workflows/tests.yml` runs the whole suite on a fresh
clone with no secrets on every push, and `tests/test_ci.py` fails if the
workflow is ever handed one.

**What I'd do differently.** Treat the clean clone as the definition of done
rather than a last check. A verification that passes only where it was written
verifies the machine, not the code — and until this incident, every "the suite
is green" in this repository's history had been measured on mine.

---

### INC-008 — The run count that was never read

**Symptom.** The dashboard's hero read *160,200 arm-seed runs · 9,000 base +
151,200 sweep* on every build. It was right, for the manifest it sat on. It had
never been read from it. This surfaced on 2026-09-15, while making the hero
honest about re-run #1's manifest, which carries no sweep grid: the count would
have gone on saying 151,200 sweep runs beside a manifest holding none.

**Investigation.** `let totalRuns = 160200;`, overwritten only from
`EVAL.metadata.total_trajectories` or `EVAL.summary.total_runs` — keys no
manifest this project has written has ever carried. The breakdown beside it was
typed into the markup. The three hero yields started life as `49.07`, `65.42`
and `53.81`, overwritten when the manifest had values and shown silently when it
did not. And a console note hardcoded "9,000 base trajectories" and sent the
reader to `make replay`, which replays nothing.

**Root cause.** This is INC-005 again. INC-005's test forbids `|| <number>` on
an expression reading the manifest; a literal starting value, overwritten by a
branch that never runs, is the same failure in a shape that test did not know
about.

**Fix.** Every displayed figure starts as `null` and becomes a number only by
being read. The run count is derived from the manifest — each arm's seeds, plus
§7's configurations and reference times arms times seeds when the grid is there
— and the breakdown says so when it is not. `tests/test_report.py` now fails on a
numeric starting value for any displayed figure, and was checked against the old
source: it catches all four.

**What I'd do differently.** A guard written against the syntax of the last bug
catches the last bug. The property is *nothing on the page is a number the
manifest did not produce*, and the test that holds it renders the page against
a manifest with every field removed and asserts that no figure survives as a
digit. That test does not exist yet, and until it does this entry is the reason
it should.

---

### INC-009 — The pre-debit notice was the merchant's message, and the regulation says it is the issuer's

**Symptom.** Re-run #1 (§2.4, the fail-closed DND guard) took Vasool down 3.03
points, and its registering row predicted that mandate debits would fall with
the messages, because "RBI's pre-debit notice is itself a contact on an
undeclared template". The prediction held. The mechanism it named was the
defect.

**Investigation.** Building §2.5's mandate lifecycle meant reading the rules
it rests on rather than the design spec's summary of them. RBI's *Digital
Payments – E-mandate Framework, 2026*, §6(a): "An issuer shall send a
pre-transaction notification to the customer, at least 24 hours prior to the
actual charge / debit." NPCI's OC-149 annexure gives the merchant's side as a
request — the payee's PSP to "initiate Pre-debit Notification API (ReqValCust)
prior 24hrs of the subsequent execution" — and OC-151A ¶4 has the PSP and the
issuing bank notify the customer. Vasool built the notice as its own SMS, on a
DLT template of its own, and gated it as a contact: the contact window, DND,
DLT and the frequency cap all ruled on a notification the merchant never
sends, and every notice that went out spent the customer's contact budget.

**Root cause.** The design spec read "notify the customer before a recurring
debit" as the merchant's obligation, and every layer built on that faithfully —
the guard, whose docstring argued the notice must be gated like any contact;
the proposal; the executor, which sent it through comms; INC-002's fix, which
made the notice go out at all; and §2.4's row, which predicted its cost. A rule
encoded wrongly and tested well passes every test that checks the code against
the rule as encoded. COMPLIANCE.md's caveat says exactly this, and here it
happened.

**Fix.** §10, 2026-09-15. The notice is a request through the rail: not a
contact, no channel, no template, sent through a port
(`vasool/actions/notice.py`) whose default adapter refuses, because no such
request is wired on this account. Re-run #2 measured it, and every registered
expectation held: the three unguarded arms byte-identical; contacts down in
every guarded arm; Vasool 46.04% → 47.39%, with 8,293 fewer refusals; the gap
to the incumbent −19.38 → −18.03 points; and the sign against `naive_retry`
back where it was before §2.4, from −0.55 to +0.80.

**What I'd do differently.** Quote the rule before encoding it. The mandate
lifecycle now refuses a transition without a quoted, pinned clause; the guards
carry statute strings and no quotations, and a statute string is a claim nobody
has to check. The next guard written starts from its clause.

---

### INC-010 — The client re-sent a debit it could not confirm, and every record showed one

**Symptom.** None in any artifact. Found by reading `vasool/actions/razorpay_client.py`
during the review of 2026-09-15. Every write the client made went through one
retry loop: a 4xx was never retried, and a gateway or server error was re-sent
up to four times with backoff. That loop was right for creating a payment link,
where a duplicate is at worst a second link. It was also the loop the mandate
debit went through.

**Investigation.** Demonstrated on the code at `47bf76f`, through the real state
machine and a fake rail that takes each debit it is sent. On a gateway error
the rail received **four debits**, and the episode moved on to `AWAITING` as if
one had failed. On a read timeout the `requests` exception was never caught: it
escaped the client, the executor and `PolicyMachine.tick`, left the episode in
`EXECUTING`, and the journal recorded nothing — a debit that may have gone
through, with no receipt that says so. The idempotency header the client sent
on every write has never been seen honoured by Razorpay; the re-sends rested on
it.

Then the attack that should catch it was written (A26), and run against the old
rule. **All three of the survival criterion's universal clauses held**: no
money moved without a full chain of ALLOW, no contact outside policy, a ledger
chained from genesis. One proposal, one receipt, one dispatch — and the rail had
taken the customer's money twice. The criterion reads the agent's own records,
and a re-send below the executor is in none of them.

**Root cause.** The client did not distinguish a write that moves money from
one that does not, and so did not distinguish "the request failed" from "we do
not know what the request did". The taxonomy says the second is the dangerous
one — `payment_timed_out`'s own rationale: "repeatedly retrying a transaction
whose outcome is unknown is how double-charges happen" — and applied it one
layer up, to classification, while the transport underneath did the opposite.
And the same call carried a request body Razorpay's documentation does not
describe: a `payment_id` it never names, and six of the eight fields it makes
mandatory missing (`docs/VERIFIED.md`, 2026-09-15). Never having run against a
live account, the path had never been checked against the page either.

**Fix.** `docs/EVALUATION.md` §10, 2026-09-15, registered and pushed before the
code. A money-moving write is never re-sent: a 5xx, a timeout, a dropped
connection or an unreadable body on one raises at once, marked
`outcome_unknown`; the receipt says `OUTCOME_UNKNOWN` rather than
`EXECUTION_FAILED`; and the state machine sends the episode to a status check —
NPCI OC-215's 90 seconds, at most three within two hours — never to a second
debit. The debit itself became a port whose default refuses, with Razorpay's
documented call built behind it and off until one live debit has been observed.
A26 now counts debits **at the rail**, with a predicate added to the criterion
for it, and survives; against the old rule it fails on exactly that clause.

**What I'd do differently.** Count at the boundary that matters. Every safety
check in this repository asks the agent's own records what happened — which is
right for what the agent decided and blind to what happened below it. The rail
is the only place a double debit exists, so it is the only place to count one.

---

## The pattern across all ten

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

INC-007 is the exception, and it belongs in this section because it is one. It
was not silent — eight tests failed loudly — it was simply never run anywhere
it could fail. An apparatus that runs only on the machine that built it
measures that machine. So the clean clone is part of the apparatus now: CI runs
the suite from one on every push, with nothing configured.

INC-009 is the other exception, and the harder one. Nothing in the apparatus
could have caught it, because the apparatus implemented the wrong rule
faithfully: every scan passed, because every scan checked the code against the
rule as written. It was found by reading the regulation instead of a summary of
it, which is why the mandate work quotes every clause it rests on.

INC-010 is the third, and it widens the lesson. The apparatus scanned the right
records correctly; the defect lived below every record it scans. A double debit
existed only at the rail, so an attack now counts there — the survival
criterion's first check that does not ask the agent what it did.

That is the argument this project is actually making. Not that the agent is
correct — I have ten incidents here that say otherwise, and two known
adversarial failures still open in the README. The argument is that **the
apparatus is built so that being wrong is discoverable**, and the evidence for
that is the list above: it is long, it is specific, and most of it was found by
the apparatus rather than by me.
