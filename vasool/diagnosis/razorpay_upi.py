"""Razorpay's UPI Autopay failure reasons, mapped to the five classes — or not.

A Razorpay merchant is never told NPCI's code. A failed subsequent UPI payment
comes back in Razorpay's error envelope — `code`, `description`, `source`,
`step`, `reason` — and Razorpay's *Create Subsequent Payments* (UPI) lists 61
values of `reason` (read from the page's markdown source on 2026-09-15,
SHA-256 `6c26636b…`, in `DOCUMENT` below). This is those 61, each with this
project's judgement and why; docs/taxonomy.md §12 is the same mapping in prose,
rendered from `MAPPINGS` and tested against it (docs/EVALUATION.md §10,
2026-09-15).

**The outcome vocabulary is §11's.** A reason maps to one of the five classes
or to one of NPCI's `Unmapped` reasons (vasool/diagnosis/npci.py), because the
two vocabularies describe the same rail: widening a class to fit a reason is
the move §11 refuses, and it refuses it here too.

**The rule that decides the most.** A reason whose documentation says money may
already have moved — "Any amount deducted will be refunded", "If money got
deducted", a pending payment, a timeout, a response that never came, a mandate
already honoured for the cycle — is `RECONCILE`, whatever its "Next Steps"
say. Razorpay's advice for most of them is "Retry after some time"; a retry
while a deduction is being refunded is how a customer is charged twice, and the
same page says "Do not create another subsequent payment until you get the
status of the previous one". So they get a status check, never a retry
(vasool/diagnosis/upi.py).

**Classified on the rail, not the string.** Five of these strings are card
reasons in §4 too — `gateway_technical_error`, `insufficient_funds`,
`payment_failed`, `payment_risk_check_failed`, `payment_timed_out` — and on UPI
three of those five can mean money moved. A UPI failure is classified against
this table and a card failure against §4, which does not change.
"""
from __future__ import annotations

from dataclasses import dataclass

from vasool.diagnosis.npci import Unmapped
from vasool.diagnosis.taxonomy import FailureClass

__all__ = ["BY_REASON", "DOCUMENT", "MAPPINGS", "Mapping", "outcome"]

DOCUMENT = {
    "publisher": "Razorpay",
    "title": "Create Subsequent Payments (UPI recurring payments)",
    "retrieved_from": (
        "https://razorpay.com/docs/api/payments/recurring-payments/upi/"
        "create-subsequent-payments.md"
    ),
    "retrieved": "2026-09-15",
    "section": "Error Response Parameters",
    "sha256": "6c26636bafa1b66801ecae4b44b8250a354d5269d3a3c2ee6baf0adb972d1bfe",
}
"""The page the 61 reasons are transcribed from. The markdown source rather than
the rendered page, because its bytes are the text and nothing else."""


@dataclass(frozen=True, slots=True)
class Mapping:
    reason: str
    outcome: FailureClass | Unmapped
    why: str


T, L, D, C, R = (FailureClass.TRANSIENT, FailureClass.LIQUIDITY, FailureClass.INSTRUMENT_DEAD,
                 FailureClass.CUSTOMER_ACTION, FailureClass.RISK_BLOCK)
REC, LEGAL, CAP, PAYEE, INT, UNDESC = (Unmapped.RECONCILE, Unmapped.LEGAL_STOP, Unmapped.CAP,
                                       Unmapped.PAYEE_SIDE, Unmapped.INTEGRATION, Unmapped.UNDESCRIBED)

_DEDUCTED = "the page says an amount may have been deducted and will be refunded: money may have moved"
_TIMEOUT = "a timeout, at a step the page does not name: on or after the debit, money may have moved"
_NEW_MANDATE = "Razorpay's next step is a new mandate: this one cannot be debited as it stands"
_MANDATE_RULE = "the debit breaks the mandate's registered rules: the merchant's schedule or amount is wrong"
_INT = "a malformed or mismatched message: an engineer's fault, not the customer's"
_CAP = "a limit binds while the instrument works: the same debit succeeds under the limit"
_PAYEE = "the merchant's own side — its address, bank or account — failed; nothing the customer does helps"
_BANK_BRIEF = "the customer's bank failed to process it and asks for another try; nothing says money moved"

MAPPINGS: tuple[Mapping, ...] = (
    Mapping("adequate_funds_not_available_blocked", L, "funds exist but are blocked: 'add sufficient unblocked funds and try again'"),
    Mapping("amount_does_not_match_mandate_amount", INT, _MANDATE_RULE),
    Mapping("bad_request_error", INT, "'Invalid Mandate Sequence Number': the merchant's request is wrong"),
    Mapping("bank_account_invalid", D, _NEW_MANDATE),
    Mapping("bank_not_available", REC, _DEDUCTED),
    Mapping("bank_technical_error", REC, _DEDUCTED),
    Mapping("banks_hsm_is_down_remitter", T, _BANK_BRIEF),
    Mapping("credit_to_beneficiary_failed", REC, "the credit leg failed, after the debit: " + _DEDUCTED),
    Mapping("debit_declined", D, _NEW_MANDATE),
    Mapping("debit_instrument_blocked", D, _NEW_MANDATE),
    Mapping("execution_day_rule_mismatch", INT, _MANDATE_RULE),
    Mapping("gateway_technical_error", REC, "'If money got deducted, reach out to the seller': money may have moved"),
    Mapping("id_value_must_be_present", INT, "'Mandate details are incorrect': the merchant's request is wrong"),
    Mapping("insufficient_funds", L, "the money is not there today: 'add balance to their account and retry'"),
    Mapping("invalid_response_from_gateway", REC, _DEDUCTED),
    Mapping("invalid_token", D, _NEW_MANDATE),
    Mapping("invalid_transaction_beneficiary", PAYEE, _PAYEE),
    Mapping("invalid_vpa", D, "the payer address on the mandate is wrong: the customer must supply a valid one"),
    Mapping("issuer_dispatch_failed", T, _BANK_BRIEF),
    Mapping("limit_exceeded_remitting_bank", CAP, _CAP),
    Mapping("mandate_cancelled", D, "'cancelled by user': the mandate is revoked, and " + _NEW_MANDATE),
    Mapping("mandate_current_cycle_allowed_debit_exceeds", REC, "'Mandate is already honoured': a debit already took this cycle's money"),
    Mapping("mandate_debit_beyond_psp_amount_cap", CAP, _CAP),
    Mapping("mandate_expired", D, "the mandate expired, and " + _NEW_MANDATE),
    Mapping("mandate_not_active", D, _NEW_MANDATE),
    Mapping("mandate_paused", C, "'paused by user': it works again once the customer resumes it"),
    Mapping("merchant_error_payee_psp", PAYEE, _PAYEE),
    Mapping("mobile_number_invalid", D, _NEW_MANDATE),
    Mapping("mpin_not_set_by_customer", C, "the customer has to set a UPI PIN: 'ask customer to set MPIN and try again'"),
    Mapping("nature_of_debit_not_allowed", D, "the account does not allow this debit: 'use a different bank account'"),
    Mapping("no_financial_address_record_found", D, "no account behind the payer address: 'try with another bank account'"),
    Mapping("no_original_request_found", UNDESC, "'No mandate details were found in the record during debit': whose record, the page does not say"),
    Mapping("null_ack_processing_failure", REC, "an unacknowledged request: whether the debit leg ran, nothing says"),
    Mapping("number_of_pin_tries_exceeded", D, _NEW_MANDATE),
    Mapping("payer_account_has_changed", D, "the account behind the payer's address changed since registration"),
    Mapping("payer_seqnum_validation_failure", INT, _INT),
    Mapping("payment_failed", REC, _DEDUCTED),
    Mapping("payment_pending", REC, "'The status of your payment is pending': the debit may yet complete"),
    Mapping("payment_risk_check_failed", R, "the customer's bank ran risk checks and declined"),
    Mapping("payment_stopped_by_court_order", LEGAL, "a court order stops the account; no automated request should go out"),
    Mapping("payment_timed_out", REC, _TIMEOUT),
    Mapping("per_transaction_limit_exceeded", CAP, _CAP),
    Mapping("psp_bank_not_available", T, "the payer's PSP or bank was unavailable; nothing was processed"),
    Mapping("psp_not_available", REC, _DEDUCTED),
    Mapping("psp_timeout", REC, _TIMEOUT),
    Mapping("regid_details_must_be_present", INT, "'Gateway validation failure': the request is malformed"),
    Mapping("remitter_account_dormant", D, "'Bank Account is closed', and " + _NEW_MANDATE),
    Mapping("remitter_dispatch_failed", T, _BANK_BRIEF),
    Mapping("request_timed_out", REC, _DEDUCTED),
    Mapping("response_not_received_within_tat", REC, "a response that never arrived in time may have carried a debit"),
    Mapping("seqnum_mismatch_payer_psp", INT, _INT),
    Mapping("suspected_fraud_decline", R, "'Suspected fraud, transaction declined by customer's bank'"),
    Mapping("transaction_frequency_limit_exceeded", CAP, "a frequency limit binds on the account"),
    Mapping("transaction_limit_exceeded", CAP, _CAP),
    Mapping("transaction_not_allowed", D, _NEW_MANDATE),
    Mapping("transaction_not_permitted_cardholder", D, "the account does not permit this debit: 'try with another bank account'"),
    Mapping("transaction_not_permitted_cardholder_beneficiary", PAYEE, "the merchant's account does not permit it"),
    Mapping("transaction_not_permitted_to_vpa", D, "autopay to this merchant is off at the customer's bank, as card_disabled_for_online_payments"),
    Mapping("umn_does_not_exist_payer", D, "'Mandate does not exist': " + _NEW_MANDATE),
    Mapping("unable_to_process_beneficiary_bank", REC, "the merchant's bank failed on the credit leg, after the debit: money may have moved"),
    Mapping("vpa_resolution_failed", D, "the payer address does not resolve: the customer must supply a valid one"),
)

BY_REASON: dict[str, Mapping] = {m.reason: m for m in MAPPINGS}


def outcome(reason: str) -> FailureClass | Unmapped:
    """What `MAPPINGS` says about one reason. A reason it does not hold raises:
    a default is a mapping nobody chose (vasool/diagnosis/upi.py decides what
    an undocumented reason gets, and it is not this)."""
    return BY_REASON[reason].outcome
