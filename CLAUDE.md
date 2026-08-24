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
- `make redteam` — 18 adversarial scenarios
- `make report` — build out/report.html
- `make replay` — assert ledger hash determinism
- `pytest` — full suite

`tools/evaluate.py` also takes `--sweep-target NAME ...` and `--skip-base`.
A partial grid writes `sweeps.json`, not `evaluation.json`, and refuses to
report an F6 verdict — F6 is registered against all 83 configurations.

`tools/shadow.py` takes `--record`, `--repeats N`, `--model`, `--rpm`. Without
`--record` no provider client is constructed at all and a missing cassette is a
hard failure (exit 3), never a silent live call. `--record` is incremental: it
requests only the cassettes that are absent, so raising `--repeats` costs only
the new repeats and an interrupted run resumes.

## Status

Current stage: 7 (LLM classifier, shadow mode). 1241 tests.

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

Evaluation state: base protocol run at 1000 seeds, development cohort.
Full §7 grid run. F1 fires against Vasool as registered
(−0.310 vs retry_plus_contact) and holds across the sweep range.
Holdout sealed.

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