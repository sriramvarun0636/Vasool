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
   All time comes from an injected clock. Enforced by a test. Violating it
   silently breaks replay determinism, which is a headline claim of the project.
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
- **Small commits**, imperative messages, one logical change each.
- Prefer stdlib and boring solutions. No new dependency without asking.
- When you disagree with the spec, say so before implementing. The spec is mine
  and it has mistakes in it.

## Known environment constraints

Discovered live on 2026-08-21, recorded in full in `docs/VERIFIED.md`:

- **Only `payment_failed` is reproducible in test mode.** Razorpay's documented
  "Error Scenario" cards do not produce their documented reasons — every failure
  returns `payment_failed / BAD_REQUEST_ERROR / gateway / payment_authorization`
  regardless of card, via both Payment Links and Checkout.js. All other reasons
  must come from `data/stubbed_payloads/`.
- **Every webhook is delivered twice** with an identical `x-razorpay-event-id`
  from two Razorpay IPs. Idempotency on `event_id` is required, not defensive.
- **`POST /v1/webhooks` needs `events` as an object**, not an array:
  `{"events": {"payment.failed": 1}}`. An array returns
  "Invalid event name/names: 1, 2, 3, 4".
- **There is no `PATCH /v1/webhooks/{id}`.** Register a new webhook to change
  the event set.
- **Payment Links reject contacts with repeated digits** (`+919999999999`).

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLite (Postgres optional) ·
pytest + hypothesis · numpy/scipy · structlog · OpenTelemetry · Jinja2

## Commands

- `make demo` — live run against Razorpay test mode
- `make eval` — 1000-seed evaluation
- `make redteam` — 18 adversarial scenarios
- `make report` — build out/report.html
- `make replay` — assert ledger hash determinism
- `pytest` — full suite

## Status

Current stage: 1 (Session 0A complete)
Stages complete: 0A — live payloads captured, VERIFIED.md written
Cassettes: not yet — LLM classifier lands in Session 7