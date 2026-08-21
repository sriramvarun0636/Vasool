"""Derive stubbed payloads from an OBSERVED payload.

Error Scenario cards don't work in test mode (see docs/VERIFIED.md), so
reasons other than payment_failed cannot be produced live. These stubs keep
the real observed envelope and vary only the error fields.

Every file is marked _SIMULATED: true. Nothing here was observed.
"""
import json, pathlib, glob

REAL = pathlib.Path("data/observed_payloads")
OUT = pathlib.Path("data/stubbed_payloads"); OUT.mkdir(exist_ok=True)

src = sorted(glob.glob(str(REAL / "payment_failed__*.json")))[0]
template = json.loads(pathlib.Path(src).read_text())

# reason -> (code, source, step) — from Razorpay's published error docs.
# NOT observed. Verify before relying on any of it.
STUBS = {
    "payment_timed_out":                 ("BAD_REQUEST_ERROR", "customer", "payment_authentication"),
    "insufficient_fund":                 ("BAD_REQUEST_ERROR", "bank",     "payment_authorization"),
    "payment_cancelled":                 ("BAD_REQUEST_ERROR", "customer", "payment_authentication"),
    "card_declined":                     ("BAD_REQUEST_ERROR", "bank",     "payment_authorization"),
    "card_disabled_for_online_payments": ("BAD_REQUEST_ERROR", "bank",     "payment_authorization"),
    "card_number_invalid":               ("BAD_REQUEST_ERROR", "customer", "payment_initiation"),
    "gateway_technical_error":           ("GATEWAY_ERROR",     "gateway",  "payment_authorization"),
}

for reason, (code, source, step) in STUBS.items():
    d = json.loads(json.dumps(template))
    d["_SIMULATED"] = True
    d["_source_note"] = f"derived from observed envelope {pathlib.Path(src).name}; error fields hand-set from Razorpay error docs, NOT observed"
    e = d["body"]["payload"]["payment"]["entity"]
    e["error_reason"] = reason
    e["error_code"] = code
    e["error_source"] = source
    e["error_step"] = step
    (OUT / f"SIMULATED__payment_failed__{reason}.json").write_text(json.dumps(d, indent=2))
    print(f"wrote SIMULATED__payment_failed__{reason}.json")
