# ARCHITECTURE

The system is built around one claim: **an LLM can be useful in a money-moving
loop without ever being trusted with money.** Everything below is the mechanism
that makes that claim checkable rather than aspirational.

## The shape

```
webhook ──▶ EVENT PLANE ──▶ DIAGNOSIS PLANE ──▶ POLICY PLANE ──▶ ACTION PLANE ──▶ LEDGER
            HMAC verify      classify           13 guards        Razorpay          hash chain
            dedupe           (rules | LLM)      FSM              (only caller)     append-only
                                    │
                                    └── the LLM lives here, and only here
```

Five planes, one direction. The interesting boundary is the second arrow.

## The air gap is a property of the type graph

The usual way to sandbox an LLM is to give it tools and validate what it asks
for. This project does not do that, because a validator is code that can have a
bug in it, and the thing it is guarding is a payment.

Instead: **the LLM has no tools at all.** `vasool/diagnosis/llm.py` is a pure
prompt plus a parser. It emits an `LLMVerdict`. The policy plane consumes a
`Proposal`. These are different types, **and there is no function anywhere in the
repository that turns one into the other.**

That is the whole mechanism. Not a check that could be bypassed — an absence.
For the LLM to move money, someone would have to write a conversion that does
not exist, and `tests/test_shadow_boundary.py` walks the import graph in both
directions to assert it stays that way.

This is a deliberate departure from the design spec, which described the LLM
emitting a `Proposal` that guards would then refuse. That design is weaker: it
puts the LLM's output on the same rails as the agent's own decisions and relies
on the guards to be perfect. The current design means a bug in the guards is a
compliance bug, not an arbitrary-execution bug.

**The LLM is therefore in shadow mode only.** It classifies, its answers are
compared against the deterministic classifier in `windtunnel/shadow.py`, and it
never touches a path that writes a ledger. Determinism is bought twice: the
comparison runs from recorded cassettes, and the boundary test proves the import
graph cannot reach a provider from anywhere a receipt is written.

## The five invariants

These are enforced by tests, not by discipline. Each one has a specific failure
it exists to prevent.

**1. The LLM never calls a tool.** Above. Enforced structurally.

**2. Nothing calls `datetime.now()` outside `vasool/clock.py`.** All time comes
from an injected clock, and `tests/test_no_wallclock.py` scans for violations.
A single real-clock call anywhere breaks replay determinism, which is a headline
claim — and it would break it *silently*, which is worse.

**3. Every money action produces a hash-chained receipt.** No exceptions. A
receipt is written once, at the decision it records, and never amended —
amending field N invalidates hash N, which receipt N+1 already committed to as
its `prev_hash`. "Amend and rehash the rest" is indistinguishable from rewriting
history, which is precisely what `verify_chain` exists to catch. When settlement
resolves later, that becomes a *fourth* receipt appended to the chain, not an
edit to the third.

**4. Guards are pure functions.** No I/O, no clock except `ctx.now`. This is what
makes them property-testable with Hypothesis, and what makes the whole run
replayable.

**5. Same seed → byte-identical ledger.** `tests/test_replay.py` asserts it.
Every derived id — receipt ids, proposal ids, the contact-window jitter — is a
SHA-256 of its inputs rather than a random draw or Python's salted `hash()`.

## Restraint is evidence

`_RECEIPTABLE` covers four transitions: `EXECUTING`, `BLOCKED`, `ESCALATED`, and
`RECOVERED`. The middle two are the point.

An agent that correctly declines to act and an agent that is broken and does
nothing produce the same observable behaviour — unless the refusal is recorded.
So a `BLOCKED` receipt carries every clause that refused the action, hash-chained
alongside the executions. `docs/taxonomy.md` §5 argues this at length for the
`RISK_BLOCK` path specifically, where correct behaviour is *always* inaction.

This is also why the guard chain evaluates all thirteen and then resolves by
severity rather than short-circuiting on the first refusal: a short-circuit
receipt cites one clause, and the receipt is meant to be evidence.

## The windtunnel

`windtunnel/` is a separate system from the agent, and the separation is
load-bearing. It builds seeded universes — 500 customers, failures drawn from a
registered mix — and runs the *real* agent against them. Not a mock: the real
receiver, the real FSM, the real thirteen guards, the real executor behind a
seam.

Three things it buys:

- **Scale at which absence is visible.** The pre-debit-notice deadlock
  (`POSTMORTEM.md` INC-002) produced no error and no failing test. It was found
  because 275 mandate episodes executed zero retries between them, and that is
  only a number you can see at a thousand seeds.
- **A world class distinct from the agent's label.** `ExecutedAction` records
  what the episode *actually* was, per the registered table — not what the arm
  believed. An arm cannot earn a recovery by being wrong about what failed, and
  three world-keyed counters catch an arm that satisfies a safety claim by
  mislabelling.
- **Adversarial pressure with an independent verdict.**
  `windtunnel/adversary/criterion.py` was registered *before* any attack was
  written, and `judge()` is the only thing that can produce one. It scans the
  ledger the way §2a scans — never "a guard returned BLOCKED". An attack may
  declare extra evidence requirements; it cannot lower the bar. `attacks.py`
  contains no `assert` and names no scoring function, enforced by AST.

## Where the numbers come from

```
windtunnel  ──▶  out/development/*.jsonl   ──▶  evaluation.json  ──▶  report.html
   runs           one row per (arm, seed)        the manifest         the dashboard
                                                       │                    │
                                                       └────────────────────┘
                                          every figure carries its manifest key
```

The report card reads the manifest and nothing else. A value the manifest does
not carry renders as a dash and raises a banner — it is never defaulted to a
plausible number, and `tests/test_report.py` fails the build if that discipline
is reintroduced. `POSTMORTEM.md` INC-005 is why that test exists.

## Known structural debt

Two, both named rather than quietly carried:

- **`tools/report.py` is 1,581 lines of HTML, CSS and JavaScript inside a Python
  f-string**, with 590 escaped brace pairs. No highlighting, no linting, no type
  checking. It has tests now; it should be a Jinja2 template, and Jinja2 is
  already a dependency. Two real bugs came out of this file's shape
  (`POSTMORTEM.md` INC-006).
- **Shards carry no fingerprint of the code that produced them.** The evaluator
  resumes by seed, which is correct for a fixed agent and wrong across a change
  to one. It has caused a silent non-run once (INC-003). The mitigation today is
  procedural — recompute and compare before trusting a fast resume — where it
  should be a content hash written into every shard.

## Repository map

| Path | Contents |
|---|---|
| `vasool/events/` | Webhook receiver, HMAC verification, dedupe, settlement correlation |
| `vasool/diagnosis/` | The failure taxonomy, the deterministic classifier, `Proposal` construction, and the LLM shadow |
| `vasool/policy/` | Thirteen guards, the state machine, the transition log |
| `vasool/actions/` | The executor — the only code permitted to call Razorpay |
| `vasool/ledger/` | Hash-chained receipts and `verify_chain` |
| `windtunnel/` | Simulator, universe, outcome model, evaluator, sweeps, adversary |
| `tools/` | CLI entry points: demo, eval, redteam, shadow, report |
| `docs/EVALUATION.md` | The pre-registered protocol. Append-only. |
| `docs/taxonomy.md` | Why each failure class gets its intervention, and §9's known limits |
| `docs/VERIFIED.md` | What was learned from the live account, including what did not work |
