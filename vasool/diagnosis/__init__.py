"""The diagnosis plane.

Turns a FailureEvent into a classification and a proposed intervention. It
proposes; it never acts. Only actions/executor.py may call Razorpay
(architectural invariant 1).
"""
