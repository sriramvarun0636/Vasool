"""The wind tunnel: the world Vasool is measured in.

The boundary this package holds, in one sentence: **windtunnel/ decides what
happens TO the agent — which failures arrive, when, and whether an
intervention lands — and vasool/ decides what the agent DOES.** Nothing here
reimplements a guard, a classification, a schedule or a state transition. The
runner advances a virtual clock, hands the real `PolicyMachine` real
`FailureEvent`s built from real captured envelopes, and replays real settlement
webhooks back through the receiver's own dispatch.

Read docs/EVALUATION.md first, and §1 of it before anything else: this package
is the world the agent is measured against, and I wrote it. Every number it
decides is registered in that document before it was run, and
windtunnel/parameters.py is asserted against the markdown in both directions so
the two cannot drift.

  rng.py         addressed randomness — no generator state anywhere
  parameters.py  §4's outcome model and §10's world shape, provenance-tagged
  payloads.py    real envelopes off disk, stamped with simulated identity
  universe.py    500 customers and the episodes they generate
  outcome.py     does money arrive?
  world.py       the FactStore the guards read
  runner.py      virtual time across the whole universe

No network, ever. This is not a --live path.
"""
