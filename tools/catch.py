"""Session 0A webhook catcher. Dumps raw payload + headers to data/observed_payloads/."""
import json, pathlib, datetime, hashlib
from fastapi import FastAPI, Request

app = FastAPI()
OUT = pathlib.Path("data/observed_payloads")
OUT.mkdir(parents=True, exist_ok=True)

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    hdrs = dict(request.headers)
    event_id = hdrs.get("x-razorpay-event-id", "no-id")
    record = {"captured_at": datetime.datetime.now().isoformat(),
              "headers": hdrs, "body": body}

    event = body.get("event", "unknown").replace(".", "_")
    pay = body.get("payload", {}).get("payment", {}).get("entity", {})
    reason = pay.get("error_reason") or "none"
    tag = hashlib.sha256(event_id.encode()).hexdigest()[:6]

    path = OUT / f"{event}__{reason}__{tag}.json"
    dup = path.exists()
    path.write_text(json.dumps(record, indent=2))

    print(f"\n=== {path.name} ===")
    print(f"event-id : {event_id}   {'(DUPLICATE)' if dup else ''}")
    print(f"reason   : {reason}")
    print(f"code     : {pay.get('error_code')}")
    print(f"source   : {pay.get('error_source')}")
    print(f"step     : {pay.get('error_step')}")
    return {"ok": True}
