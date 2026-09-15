"""The documents the mandate lifecycle is built from, and every clause it cites.

A transition in machine.py names the clauses that permit it by key, and a key
that is not here is an error at import time rather than a broken reference
found later. Each clause is quoted verbatim, so a reader can check a transition
against its text without fetching anything, and each document is pinned by the
SHA-256 of the exact bytes read: the citation names bytes, not a location, and
any copy whose digest matches is the document cited.

**Authority, not provenance tier.** vasool/events/provenance.py's three tiers
classify payloads — facts the simulator may draw. A clause is a rule, not a
payload, and what a reader needs to know about it is who can make it: the
regulator, the operator of the rail, or the platform documenting its own API.
Only one transition rests on the last alone, and tests/test_mandate.py holds
that at exactly one, so a second cannot arrive unnoticed. Three more cite it
beside NPCI: when a UPI debit fails because the mandate was revoked, paused or
has expired, Razorpay's reason is the evidence and NPCI's code says what the
state means (docs/EVALUATION.md §10, 2026-09-15).

**How each document was read.** The RBI Framework and NPCI's OC-149A carry a
text layer. The other NPCI circulars are scans with none, so they were rendered
page by page, read by OCR, and every clause quoted here was checked by eye
against the rendered page; `corrections` lists what OCR got wrong. Typographic
quotation marks are kept as printed, and so is the Framework's own "shall
provider" in §6(c). NPCI no longer hosts the older circulars at their original
addresses, so those were retrieved from the Internet Archive's captures of
them, byte-exact (`id_`), and the pin is the proof that the capture is the
circular.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Authority(StrEnum):
    """Who can make the rule a clause states. Closed."""

    REGULATOR = "REGULATOR"
    """The Reserve Bank of India, issuing Directions under the Payment and
    Settlement Systems Act, 2007."""

    RAIL = "RAIL"
    """NPCI, which operates UPI and binds its members by operating circular."""

    PLATFORM = "PLATFORM"
    """A payment platform describing its own API. Evidence that an operation
    exists; not a rule about when it may be used."""


@dataclass(frozen=True, slots=True)
class Document:
    key: str
    authority: Authority
    publisher: str
    reference: str
    title: str
    dated: date | None
    retrieved: date
    retrieved_from: str
    sha256: str | None
    """None only for a page that is not a versioned document — its bytes change
    with the site around it, so a digest would pin nothing a reader could
    reproduce."""

    read_by: str
    corrections: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Clause:
    key: str
    document: str
    clause: str
    text: str


RETRIEVED = date(2026, 9, 15)
_WAYBACK = "https://web.archive.org/web/{stamp}id_/https://www.npci.org.in/PDF/npci/upi/circular/"
_SCAN = (
    "OCR (Apple Vision) of each page rendered from the PDF, every quoted clause "
    "checked by eye against the page"
)

DOCUMENTS: dict[str, Document] = {
    d.key: d
    for d in (
        Document(
            key="RBI-EMF-2026",
            authority=Authority.REGULATOR,
            publisher="Reserve Bank of India",
            reference="RBI/DPSS/2026-27/396 · RBI/CO.DPSS.POLC.No.S56/02.14.003/2026-27",
            title="Digital Payments – E-mandate Framework, 2026",
            dated=date(2026, 4, 21),
            retrieved=RETRIEVED,
            retrieved_from=(
                "https://rbidocs.rbi.org.in/rdocs/notification/PDFs/"
                "396MDD002E435ECA145509929FC3ACBCFD0E9.PDF"
            ),
            sha256="1b8c8e39a4498d83025a7d0183d4cc5e045ce237d0745b8978e677646bcfdf9c",
            read_by=(
                "the PDF's text layer, which matches the same Directions as published at "
                "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374"
            ),
        ),
        Document(
            key="NPCI-OC-125A",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India",
            reference="NPCI/UPI/OC No.125A/2022-23",
            title=(
                "Addendum to OC 125 - Non-revocation of UPI AUTOPAY mandate for Loan "
                "repayment and EMI collection category"
            ),
            dated=date(2022, 7, 20),
            retrieved=RETRIEVED,
            retrieved_from=_WAYBACK.format(stamp="20250805131652")
            + "2022/OC-125A-Addendum-to-OC-125-Non-revocation-of-UPI-AUTOPAY-mandate-for-"
            "loan-repayment-and-EMI-collection-category.pdf",
            sha256="6557cd8b95e119bc21099fe3acb9273d629e2e0e0eaa894bf7cb0f8c0328d986",
            read_by=_SCAN,
            corrections=("'UP!' read as UPI", "'SDI-' read as SD/-"),
        ),
        Document(
            key="NPCI-OC-149A",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India",
            reference="NPCI/UPI/OC No. 149A/2022-23, with NPCI/UPI/OC No. 149/2022-23 attached",
            title="Addendum to OC 149 - Reduction of business declines in UPI",
            dated=date(2022, 6, 15),
            retrieved=RETRIEVED,
            retrieved_from=_WAYBACK.format(stamp="20241006070945")
            + "2022/OC149-A-Addendum-to-OC-149-Reduction-of-business-declines-in-UPI.pdf",
            sha256="1c7980b2bc224c029436197d02285a69f5bf49aef437dd952584b61eb6ce2147",
            read_by="the PDF's text layer",
            corrections=(
                "extraction spacing closed up: 'Pre -debit' and '1 st' read as Pre-debit and 1st",
            ),
        ),
        Document(
            key="NPCI-OC-151A",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India",
            reference="NPCI/UPI/OC-151A/2023-24",
            title="Enhancement of Limits for UPI AutoPay",
            dated=date(2023, 12, 14),
            retrieved=RETRIEVED,
            retrieved_from=_WAYBACK.format(stamp="20250810000643")
            + "2023/UPI-OC-151A-Enhancement-of-Limits-for-UPI-AutoPay.pdf",
            sha256="230854bb404a5ab81d9b3a6794e86c2ef54a2823a7a408e3a18be9a4865b76ca",
            read_by=_SCAN,
            corrections=(
                "OCR read the rupee sign as '7', '2', 'R' or '$'; every amount is ₹ on the page",
            ),
        ),
        Document(
            key="NPCI-OC-182",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India",
            reference="NPCI/UPI/OC-182/2023-24",
            title="User Experience Enhancements for UPI AutoPay",
            dated=date(2023, 12, 7),
            retrieved=RETRIEVED,
            retrieved_from=_WAYBACK.format(stamp="20250810040542")
            + "2023/UPI-OC-182-User-Experience-Enhancement-for-UPI-AutoPay.pdf",
            sha256="0e40582e9dbe02cd5a570b17f262b329561350ed4f8dae3b2ecad308246e3592",
            read_by=_SCAN,
            corrections=("'UP!' read as UPI",),
        ),
        Document(
            key="NPCI-OC-215",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India",
            reference="NPCI/UPI/OC/215/2025-26",
            title=(
                "Initial Streamlining the Check Transaction usage in UPI and overuse / "
                "misuse of all UPI APIs"
            ),
            dated=date(2025, 4, 26),
            retrieved=RETRIEVED,
            retrieved_from=_WAYBACK.format(stamp="20250914050013")
            + "2025/UPI-OC-No-215-FY-2025-26-Streamlining-the-initiation-of-Check-"
            "Transaction-in-UPI-and-misuse-of-APIs.pdf",
            sha256="42731e60940b232432981068434df95518a8b54cb41b462836fabf8d7c4e9b5a",
            read_by=_SCAN,
            corrections=("'APls' read as APIs", "the pages carry /Rotate 270 and were rendered upright"),
        ),
        Document(
            key="NPCI-OC-215A",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India",
            reference="NPCI/UPI/OC/215A/2025-26",
            title="Guidelines on usage of UPI API",
            dated=date(2025, 5, 21),
            retrieved=RETRIEVED,
            retrieved_from=_WAYBACK.format(stamp="20250613092352")
            + "2025/UPI-OC-No-215-A-FY-2025-26-Guidelines-on-usage-of-UPI-APIs.pdf",
            sha256="a591c9f0ca86cead7d840f5442e36a07710b0ab61701ed1a8261c7c597fd2bbf",
            read_by=_SCAN,
            corrections=(
                "the table's cells were reassembled row by row from the rendered page",
                "the pages carry /Rotate 270 and were rendered upright",
            ),
        ),
        Document(
            key="NPCI-OC-223",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India",
            reference="NPCI/UPI/OC-223/2025-26",
            title="Enhancement of UPI Autopay",
            dated=date(2025, 10, 7),
            retrieved=RETRIEVED,
            retrieved_from=(
                "https://www.npci.org.in/uploads/"
                "UPI_OC_No_223_FY_2025_26_Enhancement_of_UPI_Autopay_88b38535cb.pdf"
            ),
            sha256="501a4a9aabb4ff47125c89a3b08b66cb27484c1fc499c7415b26b08a0bf06007",
            read_by=_SCAN,
            corrections=("'il.' and 'ili.' read as ii. and iii.", "'UP! ID' read as UPI ID"),
        ),
        Document(
            key="NPCI-UPI-CODES-2.9",
            authority=Authority.RAIL,
            publisher="National Payments Corporation of India (NPCI)",
            reference="Public - UPI V 2.9",
            title="Unified Payments Interface — Error and Response Codes",
            dated=date(2024, 1, 17),
            retrieved=RETRIEVED,
            retrieved_from=(
                "https://dth95m2xtyv8v.cloudfront.net/tesseract/assets/upi-tpap-sdk/"
                "UPI_Error_and_Response_Codes_2_9-HHLrJ.pdf"
            ),
            sha256="93584968a0089d37c8cc7d730e1f06820841ea958a8a5bc0ee98bb14c6a231a4",
            read_by="tools/cite_npci.py, into data/cited_payloads/ (docs/EVALUATION.md §10, 2026-09-15)",
        ),
        Document(
            key="RAZORPAY-TPAP-PAUSE",
            authority=Authority.PLATFORM,
            publisher="Razorpay",
            reference="API Reference · TPAP Pro · Mandates · PATCH /v1/upi/tpap/mandates/:umn",
            title="Pause or Resume a Mandate",
            dated=None,
            retrieved=RETRIEVED,
            retrieved_from=(
                "https://razorpay.com/docs/api/payments/tpap-pro/mandate-flow/pause-resume-mandate/"
            ),
            sha256=None,
            read_by="the page's HTML",
        ),
        Document(
            key="RAZORPAY-UPI-SUBSEQUENT",
            authority=Authority.PLATFORM,
            publisher="Razorpay",
            reference="API Reference · Recurring Payments · UPI · Create Subsequent Payments",
            title="Create Subsequent Payments",
            dated=None,
            retrieved=RETRIEVED,
            retrieved_from=(
                "https://razorpay.com/docs/api/payments/recurring-payments/upi/"
                "create-subsequent-payments.md"
            ),
            # Unlike the TPAP page, this one is pinned: Razorpay serves the
            # page's markdown source, whose bytes are the text alone.
            sha256="6c26636bafa1b66801ecae4b44b8250a354d5269d3a3c2ee6baf0adb972d1bfe",
            read_by="the page's markdown source",
        ),
    )
}


CLAUSES: dict[str, Clause] = {
    c.key: c
    for c in (
        # -- RBI: the Framework applies to cards, PPI and UPI alike (§2) -------
        Clause(
            key="RBI-EMF-2026 §2",
            document="RBI-EMF-2026",
            clause="§2",
            text=(
                "The provisions of these Directions shall be applicable to all Payment "
                "System Providers and Payment System Participants in respect of processing "
                "of recurring transactions, domestic or cross-border, using cards / PPI / UPI."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §4(a)",
            document="RBI-EMF-2026",
            clause="§4(a)",
            text=(
                "A customer desirous of opting for e-mandate facility shall undertake a "
                "one-time registration process. The mandate shall be registered only after "
                "successful validation of additional factor of authentication (AFA), in "
                "addition to the normal process required by the issuer."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §4(b)",
            document="RBI-EMF-2026",
            clause="§4(b)",
            text=(
                "Every e-mandate registered by the issuer shall specify the validity period "
                "of the e-mandate. The issuer shall provide the customer with a facility to "
                "modify the validity period or withdraw the e-mandate at any point of time. "
                "Information about this facility shall be clearly communicated to the "
                "customer at the time of registration."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §4(c)",
            document="RBI-EMF-2026",
            clause="§4(c)",
            text=(
                "The e-mandate may be for either a pre-specified fixed amount or for a "
                "variable amount subject to the overall cap fixed by the RBI. In the case of "
                "variable e-mandates, the issuer shall provide the customer with a facility "
                "to specify the maximum value of any recurring transaction."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §4(e)",
            document="RBI-EMF-2026",
            clause="§4(e)",
            text=(
                "Any modification in, or withdrawal of, an existing e-mandate shall require "
                "AFA validation by the issuer."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §5(a)",
            document="RBI-EMF-2026",
            clause="§5(a)",
            text=(
                "The first transaction under an e-mandate shall require AFA validation. If "
                "the first transaction is processed along with registration of the "
                "e-mandate, then AFA validation may be combined."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §6(a)",
            document="RBI-EMF-2026",
            clause="§6(a)",
            text=(
                "An issuer shall send a pre-transaction notification to the customer, at "
                "least 24 hours prior to the actual charge / debit."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §6(c)",
            document="RBI-EMF-2026",
            clause="§6(c)",
            text=(
                "The issuer shall provider a customer with a facility to opt-out of any "
                "particular transaction or the e-mandate. Any such opt-out shall be "
                "validated by the issuer using AFA. An intimation to this effect shall be "
                "sent to the customer."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §8(a)",
            document="RBI-EMF-2026",
            clause="§8(a)",
            text=(
                "All recurring transactions may be authorised without AFA up to ₹15,000/- "
                "per transaction. Transactions above this amount shall be subject to AFA."
            ),
        ),
        Clause(
            key="RBI-EMF-2026 §8(b)",
            document="RBI-EMF-2026",
            clause="§8(b)",
            text=(
                "Payment of insurance premiums, subscription to mutual funds, and credit "
                "card bill payments may be made without AFA up to ₹1,00,000/- per transaction."
            ),
        ),
        # -- NPCI: loan and EMI mandates the payer cannot revoke or pause ---------
        Clause(
            key="NPCI-OC-125A ¶2",
            document="NPCI-OC-125A",
            clause="¶2",
            text=(
                "Along the same lines, revoke and pause option shall not be shown to the user "
                "on the Payer App for the UPI AUTOPAY mandates (including already created "
                "mandate) for the below-mentioned categories. i. Loan repayment – MCC 7322 "
                "ii. EMI Collection – MCC 7322"
            ),
        ),
        Clause(
            key="NPCI-OC-125A ¶3",
            document="NPCI-OC-125A",
            clause="¶3",
            text=(
                "For the creation of a new mandate, Payee/ Merchant shall send the "
                "“revokeable” flag as “N” during mandate creation. Payer PSP/ App needs to "
                "ensure if the revokeable flag is “N”, then, they shall not show the revoke "
                "and pause option to the user for the created mandate against the "
                "aforementioned categories."
            ),
        ),
        Clause(
            key="NPCI-OC-125A ¶4",
            document="NPCI-OC-125A",
            clause="¶4",
            text=(
                "For aforementioned categories, Merchant/ Corporate must provide the "
                "provision to revoke the UPI AUTOPAY mandate to the user on their website/ "
                "app and explicitly communicate the process to revoke the mandate. Revoking "
                "of the mandate shall be with the consent of the Merchant/ Corporate. The "
                "Acquiring bank must verify the same before making the Merchant/ Corporate "
                "live for UPI AUTOPAY for these categories."
            ),
        ),
        # -- NPCI: who asks for the pre-debit notification -----------------------
        Clause(
            key="NPCI-OC-149A Annexure NU",
            document="NPCI-OC-149A",
            clause="Annexure (OC 149), response code NU, “Unable to notify the Customer”",
            text=(
                "For AutoPay, Payee PSP to ensure that they initiate Pre-debit Notification "
                "API (ReqValCust) prior 24hrs of the subsequent execution and for the cases "
                "wherein 1st execution is not happening on the same day of mandate creation."
            ),
        ),
        # -- NPCI: the higher AFA tier, and who notifies ------------------------
        Clause(
            key="NPCI-OC-151A ¶1",
            document="NPCI-OC-151A",
            clause="¶1 (for the MCCs in Annexure A)",
            text=(
                "Payer Apps shall capture UPI PIN as an AFA, if the UPI AutoPay execution "
                "amount is more than ₹1,00,000/-, whereas process the execution without AFA "
                "for the transaction amount less than or equal to ₹1,00,000/-."
            ),
        ),
        Clause(
            key="NPCI-OC-151A ¶4",
            document="NPCI-OC-151A",
            clause="¶4",
            text=(
                "It is mandatory for the PSP to notify the user before and after execution of "
                "any mandate via push notification, and Issuer Bank to notify the user via "
                "SMS before and after execution."
            ),
        ),
        Clause(
            key="NPCI-OC-151A Annexure A",
            document="NPCI-OC-151A",
            clause="Annexure A",
            text=(
                "List of Merchant Category Code eligible for limit enhancement: 5413 Credit "
                "Card Bill Payments; 5960 Direct Marketing Insurance Services; 6012 Financial "
                "Institutions Merchandise and Services; 6211 Securities brokers and dealers; "
                "6300 Insurance sales, underwriting and premiums; 6381 Insurance Premiums; "
                "6399 Insurance; 6529 LIC. Note: Acquirer banks to enable only merchants from "
                "the category of merchants allowed by RBI in respective MCCs."
            ),
        ),
        # -- NPCI: pause, and cancellation by the merchant ------------------------
        Clause(
            key="NPCI-OC-182 §3.1",
            document="NPCI-OC-182",
            clause="§3.1",
            text=(
                "All Merchants accepting payments via UPI AutoPay (except merchants under MCC "
                "7322 category) shall support mandate pause functionality. Merchants shall "
                "not delete mandates only from their end if it is paused by user from any "
                "UPI app."
            ),
        ),
        Clause(
            key="NPCI-OC-182 §3.2",
            document="NPCI-OC-182",
            clause="§3.2",
            text=(
                "If a merchant needs to cancel the mandate, they shall send the cancellation "
                "request to UPI AutoPay channel via their acquiring bank (payee initiated "
                "revoke) instead of deleting it only on their end."
            ),
        ),
        # -- NPCI: status checks, the protocol a money-in-flight code needs -------
        Clause(
            key="NPCI-OC-215 ¶3",
            document="NPCI-OC-215",
            clause="¶3",
            text=(
                "PSP banks / Acquiring banks shall initiate the first check transaction "
                "status API after 90 seconds from the initiation/authentication of the "
                "original transaction. After the timers are changed (ref. UPI OC 214, dated "
                "26th April, 2025), members may initiate the same after 45 to 60 seconds of "
                "the initiation/authentication of original transaction, after NPCI revised "
                "communication."
            ),
        ),
        Clause(
            key="NPCI-OC-215 ¶4",
            document="NPCI-OC-215",
            clause="¶4",
            text=(
                "PSP banks / Acquiring banks may initiate maximum of 3 check transaction "
                "status APIs, preferably within 2 hours from the initiation/authentication of "
                "the original transaction."
            ),
        ),
        # -- NPCI: executing a mandate ----------------------------------------------
        Clause(
            key="NPCI-OC-215A row 5",
            document="NPCI-OC-215A",
            clause="table, SN 5, “Autopay Mandate Execution”",
            text=(
                "Purpose: Executing UPI Autopay mandates. Frequency / Limits: Maximum of 1 "
                "attempt and 3 retries per mandate (per sequence number) shall be permitted. "
                "Usage guidelines: a) Initiator PSPs to ensure UPI Autopay executions shall "
                "be initiated at moderated TPS b) To be initiated in non-peak hours"
            ),
        ),
        Clause(
            key="NPCI-OC-215A ¶3",
            document="NPCI-OC-215A",
            clause="¶3 (after the table)",
            text=(
                "Peak hours are defined as the period during the day when UPI financial "
                "transactions reach the highest transactions per second, observed from 10:00 "
                "hrs to 13:00 hrs and from 17:00 hrs to 21:30 hrs. Any other time shall be "
                "referred as non-peak hour. During peak hours, UPI members are required to "
                "restrict non-customer-initiated APIs."
            ),
        ),
        Clause(
            key="NPCI-OC-215A row 8",
            document="NPCI-OC-215A",
            clause="table, SN 8, “ValCust API”",
            text=(
                "Purpose: Service is used to validate the customer details, pre-debit "
                "notification, customer activation for FIR etc. Frequency / Limits: Service "
                "shall be only allowed to be used for valid use cases. Usage guidelines: "
                "a) For IPO, PAN validation shall only be initiated where the mandate has been "
                "successfully created b) For other use case it shall be initiated in limited "
                "attempts and at moderate TPS"
            ),
        ),
        # -- NPCI: the lifecycle the payer's app must offer ---------------------
        Clause(
            key="NPCI-OC-223 §1(i)",
            document="NPCI-OC-223",
            clause="§1(i)",
            text=(
                "Payer PSPs and UPI apps shall ensure complete lifecycle management, "
                "including viewing and porting of user’s UPI Autopay mandates on the "
                "respective app only under the ‘Manage Bank accounts’ or the dedicated ‘UPI "
                "Autopay’ section. This will be in addition to existing operations like "
                "revoke, pause, modify etc."
            ),
        ),
        Clause(
            key="NPCI-OC-223 §1(ii)",
            document="NPCI-OC-223",
            clause="§1(ii)",
            text=(
                "All payer-initiated UPI Autopay operations, as per existing guidelines, "
                "shall require UPI PIN."
            ),
        ),
        # -- NPCI: what the rail answers when a debit meets each state -----------
        # Quoted from data/cited_payloads/, which tests/test_mandate.py checks.
        Clause(
            key="NPCI-UPI-CODES-2.9 §3.1 VA",
            document="NPCI-UPI-CODES-2.9",
            clause="§3.1, response code VA",
            text="MANDATE HAS BEEN REVOKED",
        ),
        Clause(
            key="NPCI-UPI-CODES-2.9 §3.1 VT",
            document="NPCI-UPI-CODES-2.9",
            clause="§3.1, response code VT",
            text="MANDATE IS PAUSED",
        ),
        Clause(
            key="NPCI-UPI-CODES-2.9 §3.1 VU",
            document="NPCI-UPI-CODES-2.9",
            clause="§3.1, response code VU",
            text="MANDATE HAS EXPIRED",
        ),
        Clause(
            key="NPCI-UPI-CODES-2.9 §4.4 U69",
            document="NPCI-UPI-CODES-2.9",
            clause="§4.4, response code U69",
            text="COLLECT EXPIRED",
        ),
        # -- Razorpay: the only source found for resuming a paused mandate ------
        Clause(
            key="RAZORPAY-TPAP-PAUSE request",
            document="RAZORPAY-TPAP-PAUSE",
            clause="PATCH /v1/upi/tpap/mandates/:umn, request body",
            text=(
                "Pause or resume a mandate using the Razorpay TPAP Pro API. "
                '"action": "pause | unpause", '
                '"pause": { "start_at": 1722317078, "end_at": 1722317078 }'
            ),
        ),
        # -- Razorpay: what a merchant is told when a UPI Autopay debit fails ---
        # The rail's evidence for three mandate transitions. Razorpay reports
        # the state; NPCI's codes (VA, VT, VU) say what the state means.
        Clause(
            key="RAZORPAY-UPI-SUBSEQUENT mandate_cancelled",
            document="RAZORPAY-UPI-SUBSEQUENT",
            clause="§3.2, Error Response Parameters, mandate_cancelled",
            text="UPI mandate created for payment has been cancelled by user.",
        ),
        Clause(
            key="RAZORPAY-UPI-SUBSEQUENT mandate_paused",
            document="RAZORPAY-UPI-SUBSEQUENT",
            clause="§3.2, Error Response Parameters, mandate_paused",
            text="UPI mandate is not active, it is paused by user.",
        ),
        Clause(
            key="RAZORPAY-UPI-SUBSEQUENT mandate_expired",
            document="RAZORPAY-UPI-SUBSEQUENT",
            clause="§3.2, Error Response Parameters, mandate_expired",
            text="UPI Mandate is expired.",
        ),
        Clause(
            key="RAZORPAY-UPI-SUBSEQUENT status",
            document="RAZORPAY-UPI-SUBSEQUENT",
            clause="§3.2, UPI Payments",
            text="Do not create another subsequent payment until you get the status of the previous one.",
        ),
        Clause(
            key="RAZORPAY-UPI-SUBSEQUENT notification",
            document="RAZORPAY-UPI-SUBSEQUENT",
            clause="§3.1, Handy Tips",
            text=(
                "You can use the notification object in the request if you want to control "
                "pre-debit notifications and recurring debits."
            ),
        ),
        Clause(
            key="RAZORPAY-UPI-SUBSEQUENT no retry",
            document="RAZORPAY-UPI-SUBSEQUENT",
            clause="§3.1, Request Parameters, notification (Watch Out!)",
            text=(
                "We will not attempt any retry if the debit fails for tokens with the "
                "notification object in the created order. You should manually retry the "
                "debit attempt."
            ),
        ),
    )
}


def cite(*keys: str) -> tuple[str, ...]:
    """The citation for a transition or a rule: clause keys, each checked now.

    Resolving at import time is the point. A transition citing a clause that
    is not quoted here would otherwise be a reference nobody can follow, found
    — if at all — by a reader who went looking.
    """
    if not keys:
        raise ValueError("a citation names at least one clause")
    missing = [key for key in keys if key not in CLAUSES]
    if missing:
        raise KeyError(f"cited clauses not in vasool/mandate/citations.py: {missing}")
    return keys


def authorities(keys: tuple[str, ...]) -> frozenset[Authority]:
    """Who stands behind a citation."""
    return frozenset(DOCUMENTS[CLAUSES[key].document].authority for key in keys)


def _check() -> None:
    """The registry's own shape, enforced where a mistake would be made."""
    for clause in CLAUSES.values():
        if clause.document not in DOCUMENTS:
            raise KeyError(f"{clause.key} cites unknown document {clause.document!r}")
        if not clause.key.startswith(clause.document + " "):
            raise ValueError(f"{clause.key!r} must be keyed as '<document> <clause>'")
    for document in DOCUMENTS.values():
        if document.sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", document.sha256):
            raise ValueError(f"{document.key}: sha256 must be 64 lowercase hex digits")
        if document.sha256 is None and document.authority is not Authority.PLATFORM:
            raise ValueError(f"{document.key}: only platform documentation may go unpinned")


_check()
