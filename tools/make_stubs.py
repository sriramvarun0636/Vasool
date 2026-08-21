"""Derive stubbed payloads from an OBSERVED payload.

Razorpay's Error Scenario cards do not produce their documented error_reason
in test mode — every card failure returns a generic payment_failed regardless
of which card is used (docs/VERIFIED.md, confirmed across four attempts and
two checkout flows). Only payment_failed is reproducible live.

These stubs keep the real observed envelope byte-for-byte and vary only the
four error fields, so the taxonomy can be exercised against every class it
handles. Every file is marked _SIMULATED: true and lives in a separate
directory from captured data. The two are never mixed.
"""
import json, pathlib, glob

REAL = pathlib.Path("data/observed_payloads")
OUT = pathlib.Path("data/stubbed_payloads"); OUT.mkdir(exist_ok=True)

src = sorted(glob.glob(str(REAL / "payment_failed__*.json")))[0]
template = json.loads(pathlib.Path(src).read_text())

# reason -> (code, source, step)
#
# SOURCE: Razorpay's published error-code documentation, not observation.
# The first seven come from the Error Scenario card table; the last two are
# from the cards error-code reference and are required by docs/taxonomy.md —
# card_expired is the flagship INSTRUMENT_DEAD case, payment_risk_check_failed
# is the entire RISK_BLOCK class.
#
# Provenance is tracked deliberately: documented values and captured values
# are different kinds of evidence, and the taxonomy keys the generic case on
# (reason, source), so a wrong source here changes behaviour.
STUBS = {
    "payment_timed_out":                 ("BAD_REQUEST_ERROR", "customer", "payment_authentication"),
    "insufficient_fund":                 ("BAD_REQUEST_ERROR", "bank",     "payment_authorization"),
    "payment_cancelled":                 ("BAD_REQUEST_ERROR", "customer", "payment_authentication"),
    "card_declined":                     ("BAD_REQUEST_ERROR", "bank",     "payment_authorization"),
    "card_disabled_for_online_payments": ("BAD_REQUEST_ERROR", "bank",     "payment_authorization"),
    "card_number_invalid":               ("BAD_REQUEST_ERROR", "customer", "payment_initiation"),
    "gateway_technical_error":           ("GATEWAY_ERROR",     "gateway",  "payment_authorization"),
    "card_expired":                      ("BAD_REQUEST_ERROR", "bank",     "payment_authorization"),
    "payment_risk_check_failed":         ("BAD_REQUEST_ERROR", "business", "payment_authorization"),
}

NOTE = (
    "Derived from the observed envelope {src}. The four error fields are set "
    "from Razorpay's error-code documentation; everything else is real "
    "captured structure. See docs/VERIFIED.md for why live capture of these "
    "reasons is not possible."
)

for reason, (code, source, step) in STUBS.items():
    d = json.loads(json.dumps(template))
    d["_SIMULATED"] = True
    d["_source_note"] = NOTE.format(src=pathlib.Path(src).name)
    e = d["body"]["payload"]["payment"]["entity"]
    e["error_reason"] = reason
    e["error_code"] = code
    e["error_source"] = source
    e["error_step"] = step
    (OUT / f"SIMULATED__payment_failed__{reason}.json").write_text(json.dumps(d, indent=2))
    print(f"wrote SIMULATED__payment_failed__{reason}.json")

print(f"\n{len(STUBS)} stubs written to {OUT}/")