"""The ledger plane.

Everything the policy plane decided and the action plane did, turned into an
immutable, hash-chained audit trail. Two rules:

1. **Receipts wrap Transitions; they don't duplicate them.** Every field a
   Receipt needs about a decision — the proposal, the verdicts, the event —
   already exists on a vasool/policy/transitions.py::Transition. A Receipt is
   built from one, not assembled by hand at a call site.

2. **Restraint is a receipt too.** A BLOCKED or ESCALATED transition never
   moves money, but taxonomy.md §5 is explicit that the audit trail has to
   show the decision not to act as carefully as the decision to act — on the
   RISK_BLOCK path, correct behaviour is otherwise indistinguishable from the
   agent being broken.
"""
