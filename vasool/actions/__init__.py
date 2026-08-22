"""The action plane.

Everything downstream of a GATED proposal that is allowed to move money or
reach a customer. Two rules hold this package to CLAUDE.md invariant 1 and 3:

1. **Only razorpay_client.py touches the Razorpay SDK.** Enforced by
   tests/test_actions_boundary.py the way tests/test_no_wallclock.py enforces
   the clock invariant — nothing outside this package may import it.
2. **Only executor.py calls razorpay_client.py.** comms.py sends messages
   through an injected deliverer, not the SDK directly, so its own tests never
   need a Razorpay mock.

Nothing here decides *whether* an action happens — that was already settled by
the policy plane's thirteen guards before a Proposal reaches this package.
This package only knows *how*.
"""
