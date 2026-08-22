"""The policy plane.

Thirteen pure guards and a deterministic state machine standing between an
inert Proposal and any movement of money. Nothing here calls Razorpay, and
nothing here asks an LLM anything — this is the line the LLM cannot cross
(CLAUDE.md invariant 1).

Two rules govern everything in this package:

1. **Guards are pure.** No I/O, no clock except the times on GuardContext. All
   external state arrives as one frozen PolicyFacts snapshot, materialised
   before the chain runs. That is what makes them property-testable, and what
   makes the whole chain a pure function of (facts, proposal, effective_at).

2. **Guards describe, they never perform.** A guard that needs something to
   happen — a pre-debit notice sent, say — returns an inert Obligation and lets
   the state machine act on it. Same discipline as the diagnosis plane, one
   layer down.
"""
