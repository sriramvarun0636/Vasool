# VASOOL — project memory

Revenue-recovery agent for Razorpay + a verification harness that proves it's
safe to deploy. Submission for the Razorpay AI Buildathon, Track 03.
Full design: `docs/VASOOL-design-spec.md` — read the relevant section before
implementing anything.

## Non-negotiable architectural invariants

1. **The LLM never calls a tool.** The diagnosis plane emits an inert `Proposal`
   object. Only `actions/executor.py` may call Razorpay. If you find yourself
   giving an LLM a tool, stop and ask me.
2. **Nothing calls `datetime.now()` or `time.time()`** outside `vasool/clock.py`.
   All time comes from an injected clock. Enforced by
   `tests/test_no_wallclock.py`. Violating it silently breaks replay
   determinism, which is a headline claim of the project.
3. **Every money action produces a hash-chained `Receipt`.** No exceptions, no
   "TODO: add receipt later".
4. **Guards are pure functions.** No I/O, no clock access except `ctx.now`.
   They must be property-testable.
5. **Same seed → byte-identical ledger.** `tests/test_replay.py` asserts this.
   If a change breaks it, that's a bug, not an acceptable regression.

## Secrets

Never write a real key into any file. Read config from environment via
python-dotenv. `.env` is gitignored; `.env.example` holds placeholder names only.
If you need a new secret, add it to `.env.example` and tell me.

`VASOOL_ID_PEPPER` keys the customer_id HMAC. Without it, customer ids are
brute-forcible from a phone number (~10^9 candidates for an Indian mobile).

## Working agreement

- **One stage per session.** Don't run ahead into the next stage because it
  seems obvious. Ask.
- **Tests first for the policy plane.** Guards and the state machine get tests
  written before implementation.
- **Never invent a Razorpay error string.** Every `error_reason` must come from
  `data/observed_payloads/` (captured live) or `data/stubbed_payloads/`
  (hand-built, every file marked `_SIMULATED: true`; see docs/VERIFIED.md for
  why they're necessary). Anything in neither directory does not exist — do not
  guess, do not infer from documentation, do not "helpfully" add plausible ones.
  Never move a file between the two directories.
- **Flag uncertainty in code.** If a regulatory threshold or API behaviour is
  unverified, write `# VERIFY: <what> <why uncertain>` rather than asserting it.
- **Run `pytest` (bare, not `python -m pytest`) before declaring a session
  done.** `pytest.ini` sets `pythonpath = .` so both forms resolve the package.
- **Small commits**, imperative messages, one logical change each.
- Prefer stdlib and boring solutions. No new dependency without asking.
- When you disagree with the spec, say so before implementing. The spec is mine
  and it has mistakes in it.
- **Never print the contents of `.env`, and never echo a secret's value.**
  `cat .env`, `echo $RAZORPAY_KEY_SECRET`, printing `os.environ`, or any
  equivalent is forbidden — including inside a debugging detour. To check
  whether a variable is set, test for presence and print only the boolean.

## Known environment constraints

Discovered live on 2026-08-21, recorded in full in `docs/VERIFIED.md`:

- **Only `payment_failed` is reproducible in test mode.** Razorpay's documented
  "Error Scenario" cards do not produce their documented reasons — every failure
  returns `payment_failed / BAD_REQUEST_ERROR / gateway / payment_authorization`
  regardless of card, via both Payment Links and Checkout.js. All other reasons
  must come from `data/stubbed_payloads/`.
- **Every webhook is delivered twice** with an identical `x-razorpay-event-id`
  from two Razorpay IPs. Attribution is unresolved — two webhook registrations
  were active simultaneously and the API has no DELETE, so platform
  double-delivery vs. duplicate registration could not be isolated. Idempotency
  on `event_id` is required either way.
- **Subscriptions are unavailable pre-activation.** Every `subscription.*` event
  is rejected at webhook registration. The failed-subscription loop is
  stub-only.
- **`POST /v1/webhooks` needs `events` as an object**, not an array:
  `{"events": {"payment.failed": 1}}`. An array returns
  "Invalid event name/names: 1, 2, 3, 4".
- **There is no `PATCH` or `DELETE` on `/v1/webhooks/{id}`.** Both return
  "no Route matched with those values". Register a new webhook instead.
- **Payment Links reject contacts with repeated digits** (`+919999999999`).
- **Razorpay signs the compact JSON body** — `json.dumps(body,
  separators=(",", ":"))` reproduces every captured signature. Verified against
  all 9 fixtures, but not guaranteed for payloads with unicode or floats.

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLite (Postgres optional) ·
pytest + hypothesis · httpx (test only) · numpy/scipy · structlog ·
OpenTelemetry · Jinja2

## Commands

- `make demo` — live run against Razorpay test mode
- `make eval` — 1000-seed evaluation, development cohort
- `make sweeps` — eval + §7's full grid (83 configs + reference × 200 seeds, ~9h)
- `make sweep-one` — one parameter's 4 configs + reference (`TARGET=<name>`)
- `make shadow` — §4.5's rules-vs-LLM comparison. Replay by default;
  `RECORD=1 make shadow` is the only thing that calls Gemini.
- `make redteam` — 22 attacks, scored against §2a-shaped ledger scans
- `make report` — build out/report.html
- `make replay` — assert ledger hash determinism
- `pytest` — full suite

`tools/evaluate.py` also takes `--sweep-target NAME ...` and `--skip-base`.
A partial grid writes `sweeps.json`, not `evaluation.json`, and refuses to
report an F6 verdict — F6 is registered against all 83 configurations.

`tools/redteam.py` runs the adversary. The survival criterion is registered in
`windtunnel/adversary/criterion.py` and `judge()` is the only thing that can
produce a verdict: an attack survives iff no money moved that policy forbids,
no contact was sent outside policy, and the ledger records the refusal with a
verifiable chain — all three scanned from the ledger the way EVALUATION.md §2a
scans, never from "a guard returned BLOCKED". An attack declares `evidence`
that can only *add* requirements; `attacks.py` contains no `assert` and names
no scoring function, and `tests/adversary/test_attacks.py` enforces both by
AST. Each attack's registered expectation is asserted against its actual
outcome, so a known failure keeps the suite green and a *fixed* one turns it
red.

`tools/shadow.py` takes `--record`, `--partial`, `--repeats N`,
`--consistency-cell REASON/SOURCE`, `--consistency-k N`, `--model`, `--rpm`.
`--consistency-cell` measures one cell at depth and renders it as its own
section with **accuracy printed beside stability** — a stable wrong answer is
the finding, and either number alone misreports it. Depth defaults to however
many cassettes that cell has. `--partial` replays only the cassettes that exist: unrecorded cells
render as `—`, contribute to no rate, and the result is written to
`classifier_comparison_partial.*` so it cannot impersonate a full run — the
same rule `--skip-base` follows with `sweeps.json`. Without
`--record` no provider client is constructed at all and a missing cassette is a
hard failure (exit 3), never a silent live call. `--record` is incremental: it
requests only the cassettes that are absent, so raising `--repeats` costs only
the new repeats and an interrupted run resumes.

## Status

Current stage: 8 (adversary). 1370 tests.

Stages complete:
  - 0A — live payloads captured, VERIFIED.md written
  - 0B — closed early; subscriptions unavailable pre-activation
  - 1  — clock + event plane, HMAC verified against real captured signatures
  - 2  — failure taxonomy + deterministic classifier
  - 3  — policy plane: 13 guards, FSM, transition log.
         Guards evaluate all-then-resolve-by-severity, not short-circuit.
         Gating happens at execute time, not propose time.
  - 4  — actions + ledger; 4.5 demo; 4.6 golden fixtures; 4.7 replay-by-default
  - 5  — windtunnel: simulator, universe, outcome model, runner
  - 5.5 — two agent defects the simulator found (Closure enum; RetryIndex
         correlation through from_webhook)
  - 6  — evaluator: metrics, arms, ablations, sweeps, split. F6 wired.
         §10 rows 1–5 registered.
  - 7  — LLM classifier in shadow: vasool/diagnosis/llm.py (pure prompt +
         parser, emits LLMVerdict — deliberately NOT a Proposal, see below),
         windtunnel/cassette.py, windtunnel/shadow.py, tools/gemini.py,
         tools/shadow.py. Corpus is the whole input space: 12 distinct
         (reason, source, code, step) tuples, which is every question the
         registered universe can ask a fields-only classifier.
  - 8  — adversary: windtunnel/adversary/{criterion,arena,attacks,harness}.py,
         tools/redteam.py. Criterion registered before any attack was written.
         22 attacks, ids kept from the reviewed list (A17/A21/A25 were cut, so
         the numbering has gaps on purpose). **18 of 22 survive.** Every attack
         drives the real receiver, the real FSM, the real thirteen guards and
         the real executor; the arena only decides what happens *to* the agent.

Adversarial state: 18/22. Four named failures remain: §9.3 timezone, §9.10
out-of-band settlement, `derive_customer_id`'s identity split, and the DND
classification gap. A15/A16/A18/A19 were fixed on 2026-08-25: later failure
evidence supersedes queued work from a different reason/source, retry quiet
hours are re-checked at final gating, and promise-to-pay never delays a human
handoff.

**taxonomy.md §9.13 — found, fixed and re-run on 2026-08-25.** Did not come
from an attack. Obligations were honoured only in `PolicyMachine._execute`,
while `PreDebitNoticeGuard` emits its own on a DEFER, so a mandate debit never
produced its notice and never executed — a *liveness* failure, the only one in
§9. `_defer` now calls `_honour`, after its `MAX_DEFERRALS` and `DEFER_HORIZON`
bounds, and the dead loop in `_execute` is gone. Seed 0, full universe: 196
pre-debit notices now execute, 272 of 979 retries land on the 275 mandate
episodes (was 0 of 707), and 30 of those episodes end BLOCKED (was 209). It had
been shaping every evaluation number published before that date — Vasool
0.344341 → 0.490698, F5's gap 19.378 → 4.742pp against a threshold of 20, so
about three quarters of the measured "price of the guards" was this defect.
`EVALUATION.md` §10 carries the fix, the re-run, and the stale-shard incident;
`docs/taxonomy.md` §9.13 keeps the full before/after.

Evaluation state: base protocol run at 1000 seeds, development cohort, all
post-fix. Full §7 grid run. F1 does **not** fire — its interval excludes zero —
but it excludes it on the wrong side: Vasool is behind `retry_plus_contact` by
−0.164, which the artifact's own `detail` field flags as a worse result than F1
firing, so `fired: false` must not be read as "good". Holds across the sweep
range. No criterion fires; F6 false. Holdout sealed.

**The model is pinned**: `windtunnel/shadow.py::PINNED_MODEL`. Cassettes are
keyed by model, so changing that string orphans every recording at once and a
re-record costs a fresh day. `tests/windtunnel/test_cassette_pin.py` holds the
pin against the cassettes actually on disk, so the edit fails a test before it
spends the day. There is deliberately no second model constant anywhere.

**Gemini free tier: 20 requests/day** on `gemini-3.6-flash` for this project,
observed 2026-08-24 from the API's own refusal (`quotaValue: 20`,
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). Smaller than one pass
over the 12-cell corpus at any k. `tools/gemini.py` now stops immediately on a
per-day refusal instead of retrying it — the first record run spent 4 futile
retries, a fifth of a day's budget. Per-minute refusals are still retried, and
honour the server's `retryDelay`.

Cassettes: `data/cassettes/`, one JSON per (provider, model, prompt, repeat),
sha256-addressed and provider-agnostic. Replay is the default everywhere,
including pytest; a miss raises `CassetteMiss` rather than calling anything.
Determinism is bought twice: the LLM also never runs on any path that writes a
ledger, and `tests/test_shadow_boundary.py` walks the import graph in both
directions to prove it. There is no conversion from an `LLMVerdict` to a
`Proposal` anywhere — invariant 1 is a property of the type graph, which is a
deliberate departure from §4.5's wording.

Ground truth for the comparison is `PlannedEpisode.failure_class`, which
resolves through the same `lookup()` the rules classifier reads — so the Rules
column is **1.000 by construction, not by measurement**. That is stated in the
rendered artifact itself, alongside the nine-of-ten-reasons-are-`_SIMULATED`
limit (EVALUATION.md §11) and the fact that the model was chosen for cost.

- **Never run git commit, git add, git push, git merge, or git checkout.**
  I commit. You write code; I review the diff and commit it myself. If you
  think something is ready to commit, say so and stop. This includes read-only
  git — reconstruct diffs from the files.
- Small commits, imperative messages, one logical change each.
