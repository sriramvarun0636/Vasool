"""Who the contact cap is counting.

`FrequencyCapGuard` allows three contacts per customer per seven days, and a
customer has always been a payment identifier: `derive_customer_id`, which is
contact and email hashed together under the pepper. One person writing from two
addresses is therefore two customers, may be contacted twice as often, and
every scan in this repository reports compliance while it happens — attack A07,
registered FAILS the day the suite was written and open until now.

The unit is the rule. A cap that exists because the Fair Practices Code is
about not harassing a *person* has to count people, so the resolver here answers
one question — which human is this record — and the fact store hands the guard
that human's contacts instead of that identifier's. The guard is unchanged; it
counts what it is given (docs/EVALUATION.md §10, 2026-09-17).
"""
