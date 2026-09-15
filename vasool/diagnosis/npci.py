"""NPCI's UPI response codes, mapped to the five failure classes — or not.

The vocabulary is NPCI's. `data/cited_payloads/npci_upi_v2.9__s*.json`
transcribe §3.1, §4.1 and §4.4 of *Unified Payments Interface — Error and
Response Codes* v2.9 into the CITED tier (docs/EVALUATION.md §10, 2026-09-15).
The mapping here is this project's judgement; docs/taxonomy.md §11 is the same
mapping in prose, generated from `MAPPINGS` and tested against it.

**Nothing runs this yet.** No classifier reads this module and no module the
simulator imports reaches it — tests/test_npci.py asserts both — because what
UPI failures the universe draws, and how a Razorpay webhook would carry an NPCI
code at all, are §2.5's decisions, each with its own amendment and re-run. This
lands the judgement first, where it can be read and argued with before
anything depends on it.

**Keyed by (section, code), never by code.** NPCI's own document gives U81 two
meanings — "REMITTER BANK DEEMED CHECK DECLINE" in §4.4, "UIDAI AUTH RES
INVALID/FORMAT ERROR" in §4.5 — so a bare code is not an identifier.

**The rules, applied to every code.**

  - `TRANSIENT` — a bank, switch or PSP failed technically, or declined up
    front on a throttle or health check, and nothing moved. NPCI §2.1: a
    FAILURE result means "the fund transfer shall not be successful", so any
    code carried in one describes a transfer that did not happen — except the
    codes that report a timeout on or after the debit leg, a reversal, a
    duplicate or a late response, which are `Unmapped.RECONCILE`. A timeout in
    the authorisation step (U09, U20) comes before any debit and is TRANSIENT.
  - `LIQUIDITY` — the funds are not available now: short (Z9), or blocked by
    another mandate (IE).
  - `INSTRUMENT_DEAD` — the account, card, mandate or registration cannot be
    debited as it stands, and the fix lies outside the payment: at the bank, in
    the registration, on the mandate.
  - `CUSTOMER_ACTION` — the customer's own input or choice is what is missing:
    a PIN or OTP, an approval, a paused mandate. The line between these two is
    the one the Razorpay mapping already draws (docs/taxonomy.md §4) between
    `card_number_invalid` (input — CUSTOMER_ACTION) and
    `card_disabled_for_online_payments` (the instrument's state — DEAD).
  - `RISK_BLOCK` — a fraud, risk or compliance engine declined, or an integrity
    signal fired. Compliance declines are here rather than DEAD because an
    automated payment request after a screening hit is the one message that
    must not go out.

  Everything else is `Unmapped`, with a reason. Each reason is a finding about
  the taxonomy, not a gap to close by widening a class — docs/taxonomy.md §11.

**Anchored to NPCI's own column.** The document never expands "TD" and "BD",
so the anchor is stated in the column's terms: no code marked TD maps to a
class that places the fault with the customer or the instrument, and no code
marked BD maps to TRANSIENT. tests/test_npci.py holds both.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vasool.diagnosis.taxonomy import FailureClass

__all__ = ["BY_KEY", "MAPPINGS", "Mapping", "Unmapped", "outcome"]


class Unmapped(StrEnum):
    """Why a code sits outside the five classes. Every member is a finding."""

    RECONCILE = "RECONCILE"
    """Money may have moved: a timeout on or after the debit leg, a pending,
    partial or failed reversal, a duplicate, a late response. All five classes assume a failed payment moved
    nothing — TRANSIENT retries, and so does the unmapped fail-safe — so here a
    retry can be a second debit. The action is a status check, never a retry."""

    LEGAL_STOP = "LEGAL_STOP"
    """Death, insolvency, incapacity, a court or attachment order. DEAD would send
    a re-authorisation link — to a deceased customer's phone, or into an
    insolvency moratorium. Only a human should decide what happens next."""

    CAP = "CAP"
    """A limit binds while the instrument works and the money exists: the same
    request succeeds below the limit, or once the limit's own window resets —
    a time-shifted retry like LIQUIDITY's, but on the cap's clock, not payday's."""

    PAYEE_SIDE = "PAYEE_SIDE"
    """The merchant's own account, bank, acquirer or registration. Every class
    assumes the failure is the customer's or the rail's; nothing the customer
    does fixes this, and a message asking them to would be false."""

    INTEGRATION = "INTEGRATION"
    """A malformed or mismatched message between PSP, UPI and bank, or a debit
    that breaks its mandate's registered rules — an engineer's fault."""

    NO_CAUSE = "NO_CAUSE"
    """Reports an outcome without a cause (U30 DEBIT HAS BEEN FAILED). The cause
    is the code that travels with it: UPI failures carry two (§2.2 ErrorCode,
    §2.3 RespCode), exactly as a Razorpay failure is classified on
    (error_reason, error_source) rather than one field (docs/taxonomy.md §3)."""

    UNDESCRIBED = "UNDESCRIBED"
    """The description does not say what failed, or whose side it failed on."""

    NOT_A_DECLINE = "NOT_A_DECLINE"
    """Not a failure at all: success, or a code NPCI reserves for status checks."""


@dataclass(frozen=True, slots=True)
class Mapping:
    section: str
    code: str
    outcome: FailureClass | Unmapped
    why: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.section, self.code)


T, L, D, C, R = (FailureClass.TRANSIENT, FailureClass.LIQUIDITY, FailureClass.INSTRUMENT_DEAD,
                 FailureClass.CUSTOMER_ACTION, FailureClass.RISK_BLOCK)
REC, LEGAL, CAP, PAYEE, INT, NOCAUSE, UNDESC, NOTDEC = Unmapped

_TECH = "a bank, switch or PSP failed technically; nothing moved"
_TECH_PAYEE = "the merchant's bank or acquirer failed technically; nothing moved, and the customer did nothing wrong"
_THROTTLE = "declined up front by a throttle or health check; nothing was sent"
_UNDELIVERED = "the request was never delivered, so nothing was debited"
_UNEXPANDED = "NPCI marks it TD and describes it no further"
_TIMEOUT = "a timeout: whether money moved is settled later, by status check or UDIR"
_CREDIT_LEG = "the credit leg follows the debit, so the customer may already be charged"
_REVERSAL = "a reversal is pending, partial or failed: money has moved"
_DUPLICATE = "a duplicate: an earlier request decides, and it may have succeeded"
_LATE = "a late response may have carried a success"
_PAYEE = "the merchant's own side failed; nothing the customer does helps"
_INT = "a malformed or mismatched message: an engineer's fault, not the customer's"
_MANDATE_RULE = "the debit breaks its mandate's registered rules: the merchant's schedule is wrong"
_CAP = "a limit binds while the instrument works and the money exists"
_LEGAL = "a legal status stops the account; no automated request should go out"
_NOCAUSE = "an outcome without a cause; classify on the code that accompanies it"
_RISK = "a risk engine suspected fraud"
_COMPLIANCE = "a compliance decline; if it is screening, an automated request is tipping-off"

_S31, _S41, _S44 = "3.1", "4.1", "4.4"

MAPPINGS: tuple[Mapping, ...] = (
    # --- §3.1: codes a remitter or beneficiary bank returns on a debit or credit
    Mapping(_S31, "00", NOTDEC, "success"),
    Mapping(_S31, "15", D, "the customer's bank is not live on UPI"),
    Mapping(_S31, "59", R, _RISK),
    Mapping(_S31, "AM", C, "the customer has not set a UPI PIN yet"),
    Mapping(_S31, "B1", D, "the mobile number the UPI profile rests on changed; it must be re-registered"),
    Mapping(_S31, "B3", D, "the bank does not permit this account to be debited this way"),
    Mapping(_S31, "B6", INT, _INT),
    Mapping(_S31, "CA", PAYEE, "a compliance decline at the merchant's acquirer"),
    Mapping(_S31, "CI", R, _COMPLIANCE),
    Mapping(_S31, "DF", REC, _DUPLICATE),
    Mapping(_S31, "DT", REC, _DUPLICATE),
    Mapping(_S31, "HS", T, _TECH),
    Mapping(_S31, "IC", D, "the block this debit draws on does not exist"),
    Mapping(_S31, "ID", CAP, "the debit exceeds the amount blocked for it"),
    Mapping(_S31, "IE", L, "the funds exist but another mandate has blocked them"),
    Mapping(_S31, "IR", T, _TECH),
    Mapping(_S31, "K1", R, _RISK),
    Mapping(_S31, "LC", PAYEE, "the beneficiary bank could not credit from its pool account"),
    Mapping(_S31, "LD", T, _TECH),
    Mapping(_S31, "NO", NOTDEC, "NPCI reserves it for status checks: 'members should not decline' with it"),
    Mapping(_S31, "PS", PAYEE, "the merchant's account is at its maximum balance"),
    Mapping(_S31, "QU", D, "the account behind the payer's address changed"),
    Mapping(_S31, "UA", INT, "the PSP is not supported by UPI"),
    Mapping(_S31, "UB", T, _TECH_PAYEE),
    Mapping(_S31, "UP", REC, _TIMEOUT),
    Mapping(_S31, "VA", D, "the mandate was revoked"),
    Mapping(_S31, "VB", INT, _MANDATE_RULE),
    Mapping(_S31, "VC", INT, _MANDATE_RULE),
    Mapping(_S31, "VD", INT, _MANDATE_RULE),
    Mapping(_S31, "VE", REC, "this execution was already honoured: a retry would collect twice"),
    Mapping(_S31, "VF", D, "the bank holds no mandate with this UMN"),
    Mapping(_S31, "VG", D, "the payer address on the mandate is wrong"),
    Mapping(_S31, "VH", R, "the mandate's signature is tampered or corrupt"),
    Mapping(_S31, "VI", INT, _MANDATE_RULE),
    Mapping(_S31, "VJ", D, "the account behind the mandate changed"),
    Mapping(_S31, "VK", CAP, "the account holds as many mandates as its bank allows"),
    Mapping(_S31, "VL", D, "this account type cannot carry a mandate"),
    Mapping(_S31, "VM", D, "this account type does not allow this kind of debit"),
    Mapping(_S31, "VO", LEGAL, _LEGAL),
    Mapping(_S31, "VP", LEGAL, _LEGAL),
    Mapping(_S31, "VQ", LEGAL, _LEGAL),
    Mapping(_S31, "VR", LEGAL, _LEGAL),
    Mapping(_S31, "VS", REC, _DUPLICATE),
    Mapping(_S31, "VT", C, "the customer paused the mandate; it works again once they resume it"),
    Mapping(_S31, "VU", D, "the mandate expired"),
    Mapping(_S31, "VY", PAYEE, "the merchant's own address on the debit is wrong"),
    Mapping(_S31, "VZ", LEGAL, _LEGAL),
    Mapping(_S31, "X6", PAYEE, _PAYEE),
    Mapping(_S31, "X7", T, _TECH_PAYEE),
    Mapping(_S31, "XB", UNDESC, "NPCI's own catch-all for a member with no better code"),
    Mapping(_S31, "XC", UNDESC, "NPCI's own catch-all for a member with no better code"),
    Mapping(_S31, "XD", INT, _INT),
    Mapping(_S31, "XE", INT, _INT),
    Mapping(_S31, "XF", INT, _INT),
    Mapping(_S31, "XG", INT, _INT),
    Mapping(_S31, "XH", D, "the customer's account does not exist"),
    Mapping(_S31, "XI", PAYEE, _PAYEE),
    Mapping(_S31, "XJ", D, "the customer's bank does not support this function"),
    Mapping(_S31, "XK", PAYEE, _PAYEE),
    Mapping(_S31, "XL", D, "the card on record expired, as card_expired"),
    Mapping(_S31, "XM", PAYEE, _PAYEE),
    Mapping(_S31, "XN", D, "the bank holds no record of the card"),
    Mapping(_S31, "XO", PAYEE, _PAYEE),
    Mapping(_S31, "XP", D, "the card is not permitted this transaction, as card_disabled_for_online_payments"),
    Mapping(_S31, "XQ", PAYEE, _PAYEE),
    Mapping(_S31, "XR", D, "the card is restricted"),
    Mapping(_S31, "XS", PAYEE, _PAYEE),
    Mapping(_S31, "XT", T, "the customer's bank is in its cut-off; nothing moved"),
    Mapping(_S31, "XU", T, "the merchant's bank is in its cut-off; nothing moved"),
    Mapping(_S31, "XV", R, _COMPLIANCE),
    Mapping(_S31, "XW", PAYEE, "a compliance decline on the merchant's side"),
    Mapping(_S31, "XX", D, "no account is mapped to this payment address"),
    Mapping(_S31, "XY", T, _TECH),
    Mapping(_S31, "Y1", T, _TECH_PAYEE),
    Mapping(_S31, "YA", R, "a card reported lost or stolen: its holder may not know, or may not be the one paying"),
    Mapping(_S31, "YB", PAYEE, _PAYEE),
    Mapping(_S31, "YC", D, "a generic 'do not honour': one probe, then dead, exactly as card_declined"),
    Mapping(_S31, "YD", PAYEE, _PAYEE),
    Mapping(_S31, "YE", D, "the customer's account is blocked or frozen; the code does not say why"),
    Mapping(_S31, "YF", PAYEE, _PAYEE),
    Mapping(_S31, "YH", PAYEE, _PAYEE),
    Mapping(_S31, "YI", UNDESC, "the bank returned a response code UPI does not recognise"),
    Mapping(_S31, "Z5", PAYEE, "the beneficiary's credentials are invalid"),
    Mapping(_S31, "Z6", D, "too many wrong PINs: the bank has locked UPI on this account"),
    Mapping(_S31, "Z7", CAP, _CAP),
    Mapping(_S31, "Z8", CAP, _CAP),
    Mapping(_S31, "Z9", L, "the account is short of funds right now"),
    Mapping(_S31, "ZC", T, _TECH_PAYEE),
    Mapping(_S31, "ZD", INT, _INT),
    Mapping(_S31, "ZF", D, "the bank does not permit payments from this device"),
    Mapping(_S31, "ZI", R, _RISK),
    Mapping(_S31, "ZJ", T, _TECH_PAYEE),
    Mapping(_S31, "ZK", T, _TECH),
    Mapping(_S31, "ZL", REC, _LATE),
    Mapping(_S31, "ZM", C, "the customer entered a wrong PIN"),
    Mapping(_S31, "ZN", PAYEE, _PAYEE),
    Mapping(_S31, "ZO", PAYEE, _PAYEE),
    Mapping(_S31, "ZP", PAYEE, _PAYEE),
    Mapping(_S31, "ZQ", REC, _REVERSAL),
    Mapping(_S31, "ZR", C, "the customer entered a wrong OTP"),
    Mapping(_S31, "ZS", C, "the OTP expired before the customer used it"),
    Mapping(_S31, "ZT", CAP, _CAP),
    Mapping(_S31, "ZU", CAP, _CAP),
    Mapping(_S31, "ZV", C, "the customer entered a wrong OTP"),
    Mapping(_S31, "ZX", D, "the customer's account is inactive or dormant"),
    Mapping(_S31, "ZY", PAYEE, _PAYEE),
    Mapping(_S31, "FL", CAP, "a first-time user's first transaction is capped"),
    Mapping(_S31, "FP", CAP, "a first-time user is inside the 24-hour cool-down"),
    Mapping(_S31, "MR", D, "the account details changed in a bank merger"),
    Mapping(_S31, "MB", PAYEE, _PAYEE),
    # --- §4.1: codes UPI itself returns on a debit or credit timeout
    Mapping(_S41, "21", T, "fully reversed: nothing moved, so a fresh attempt is safe"),
    Mapping(_S41, "32", REC, _REVERSAL),
    Mapping(_S41, "BT", REC, _TIMEOUT),
    Mapping(_S41, "RB", REC, _REVERSAL),
    Mapping(_S41, "RP", REC, _REVERSAL),
    Mapping(_S41, "RR", REC, _REVERSAL),
    Mapping(_S41, "UT", REC, _TIMEOUT),
    # --- §4.4: errors from the UPI service layer
    Mapping(_S44, "M16", R, "NPCI's AI model declined it"),
    Mapping(_S44, "U01", REC, _DUPLICATE),
    Mapping(_S44, "U02", CAP, _CAP),
    Mapping(_S44, "U03", CAP, "the bank's net debit cap: a system limit that resets"),
    Mapping(_S44, "U04", INT, _INT),
    Mapping(_S44, "U05", INT, _INT),
    Mapping(_S44, "MB7", INT, "a mandate longer than a year was requested"),
    Mapping(_S44, "MQ7", PAYEE, "the merchant's MCC is not mapped to the purpose"),
    Mapping(_S44, "U06", INT, _INT),
    Mapping(_S44, "U07", INT, _INT),
    Mapping(_S44, "U08", T, "a system exception inside UPI; nothing moved"),
    Mapping(_S44, "U09", T, "the authorisation step timed out, before any debit"),
    Mapping(_S44, "U10", INT, _INT),
    Mapping(_S44, "U11", INT, _INT),
    Mapping(_S44, "U12", INT, _INT),
    Mapping(_S44, "U13", T, _UNEXPANDED),
    Mapping(_S44, "U14", INT, _INT),
    Mapping(_S44, "U15", INT, _INT),
    Mapping(_S44, "U16", R, "UPI's risk threshold was exceeded"),
    Mapping(_S44, "U17", INT, _INT),
    Mapping(_S44, "U18", T, "the authorisation step went unacknowledged, before any debit"),
    Mapping(_S44, "U19", NOCAUSE, "the PSP declined authorisation; the reason is in its own code, outside this vocabulary"),
    Mapping(_S44, "U20", T, "the authorisation step timed out, before any debit"),
    Mapping(_S44, "U21", INT, _INT),
    Mapping(_S44, "U22", T, _UNEXPANDED),
    Mapping(_S44, "U23", T, _UNEXPANDED),
    Mapping(_S44, "U24", T, _UNEXPANDED),
    Mapping(_S44, "U25", INT, _INT),
    Mapping(_S44, "U26", REC, _CREDIT_LEG),
    Mapping(_S44, "U27", T, "the PSP did not respond, before any debit"),
    Mapping(_S44, "U28", T, _TECH),
    Mapping(_S44, "U29", UNDESC, "does not say whose address failed to resolve, the customer's or the merchant's"),
    Mapping(_S44, "U30", NOCAUSE, _NOCAUSE),
    Mapping(_S44, "U31", NOCAUSE, _NOCAUSE),
    Mapping(_S44, "U32", REC, _REVERSAL),
    Mapping(_S44, "U33", REC, _REVERSAL),
    Mapping(_S44, "U34", NOCAUSE, _NOCAUSE),
    Mapping(_S44, "U35", REC, _DUPLICATE),
    Mapping(_S44, "U36", REC, _DUPLICATE),
    Mapping(_S44, "U37", REC, _REVERSAL),
    Mapping(_S44, "U38", REC, _DUPLICATE),
    Mapping(_S44, "U39", NOCAUSE, "an earlier failure decides; its own code carries the cause"),
    Mapping(_S44, "U40", T, _TECH),
    Mapping(_S44, "U41", T, _TECH),
    Mapping(_S44, "U42", REC, _DUPLICATE),
    Mapping(_S44, "U43", NOCAUSE, _NOCAUSE),
    Mapping(_S44, "U44", T, _UNEXPANDED),
    Mapping(_S44, "U45", T, _UNEXPANDED),
    Mapping(_S44, "U46", INT, _INT),
    Mapping(_S44, "U47", INT, _INT),
    Mapping(_S44, "U48", INT, _INT),
    Mapping(_S44, "U49", INT, _INT),
    Mapping(_S44, "U50", INT, _INT),
    Mapping(_S44, "U51", INT, _INT),
    Mapping(_S44, "U52", INT, _INT),
    Mapping(_S44, "U53", REC, "the debit leg went unacknowledged: it may have happened"),
    Mapping(_S44, "U54", INT, _INT),
    Mapping(_S44, "U55", INT, _INT),
    Mapping(_S44, "U56", INT, _INT),
    Mapping(_S44, "U57", INT, _INT),
    Mapping(_S44, "U58", INT, _INT),
    Mapping(_S44, "U59", INT, _INT),
    Mapping(_S44, "U60", INT, _INT),
    Mapping(_S44, "U61", INT, _INT),
    Mapping(_S44, "U62", INT, _INT),
    Mapping(_S44, "U63", INT, "device registration, not a payment"),
    Mapping(_S44, "U64", INT, "device registration, not a payment"),
    Mapping(_S44, "U65", INT, "device registration, not a payment"),
    Mapping(_S44, "U66", R, "the device is not the one the bank registered: the pattern of account takeover"),
    Mapping(_S44, "U67", REC, _TIMEOUT),
    Mapping(_S44, "U68", REC, _TIMEOUT),
    Mapping(_S44, "U69", C, "the customer did not approve the collect request before it expired"),
    Mapping(_S44, "U70", REC, _LATE),
    Mapping(_S44, "U71", PAYEE, "the merchant's account cannot take this credit"),
    Mapping(_S44, "U72", T, _UNEXPANDED),
    Mapping(_S44, "U74", INT, _INT),
    Mapping(_S44, "U75", INT, _INT),
    Mapping(_S44, "U76", INT, "mobile-banking registration, not a payment"),
    Mapping(_S44, "U77", PAYEE, "the merchant itself is blocked"),
    Mapping(_S44, "U78", T, _TECH_PAYEE),
    Mapping(_S44, "U80", T, _THROTTLE),
    Mapping(_S44, "U81", T, _THROTTLE),
    Mapping(_S44, "U82", REC, _CREDIT_LEG),
    Mapping(_S44, "U84", T, _THROTTLE),
    Mapping(_S44, "U85", T, _UNDELIVERED),
    Mapping(_S44, "U86", T, _THROTTLE),
    Mapping(_S44, "U87", REC, _TIMEOUT),
    Mapping(_S44, "U88", REC, _CREDIT_LEG),
    Mapping(_S44, "U89", T, _THROTTLE),
    Mapping(_S44, "U90", T, _THROTTLE),
    Mapping(_S44, "U91", T, _THROTTLE),
    Mapping(_S44, "U92", T, _TECH),
    Mapping(_S44, "U93", T, _TECH_PAYEE),
    Mapping(_S44, "U94", T, _THROTTLE),
    Mapping(_S44, "U95", PAYEE, "the merchant's payment address is disabled"),
    Mapping(_S44, "U96", INT, "payer and payee are the same account"),
    Mapping(_S44, "U97", INT, "a meta transaction, not a payment"),
    Mapping(_S44, "U98", INT, "a meta transaction, not a payment"),
    Mapping(_S44, "U99", INT, "a meta transaction, not a payment"),
    Mapping(_S44, "S93", T, _THROTTLE),
    Mapping(_S44, "S94", T, _THROTTLE),
    Mapping(_S44, "S95", REC, _CREDIT_LEG),
    Mapping(_S44, "S96", T, _UNDELIVERED),
    Mapping(_S44, "S97", T, "address resolution was never dispatched, before any debit"),
    Mapping(_S44, "S98", T, _UNDELIVERED),
    Mapping(_S44, "HS1", T, "UPI's HSM was unavailable; nothing moved"),
    Mapping(_S44, "HS2", T, "UPI's HSM was unavailable; nothing moved"),
    Mapping(_S44, "HS3", T, "UPI's HSM was unavailable; nothing moved"),
)

BY_KEY: dict[tuple[str, str], Mapping] = {m.key: m for m in MAPPINGS}


def outcome(section: str, code: str) -> FailureClass | Unmapped:
    """What `MAPPINGS` says about one code. A key it does not hold raises —
    there is no default here, because a default is a mapping nobody chose."""
    return BY_KEY[(section, code)].outcome
