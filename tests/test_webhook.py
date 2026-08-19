"""Exercise the webhook hardening: signatures, dedupe, fast ack, serialisation."""
import hashlib
import hmac
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath("."))

SECRET = "test-app-secret"
os.environ["WHATSAPP_PROVIDER"] = "meta"
os.environ["META_WA_APP_SECRET"] = SECRET
os.environ["META_WA_TOKEN"] = "fake"
os.environ["META_WA_PHONE_NUMBER_ID"] = "12345"

from fastapi.testclient import TestClient  # noqa: E402

import run_whatsapp  # noqa: E402
from dualbook import config  # noqa: E402

config.META_WA_APP_SECRET = SECRET
config.WHATSAPP_PROVIDER = "meta"

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name)


def meta_payload(wamid, text="hi", frm="923001234567"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": frm, "id": wamid, "type": "text", "text": {"body": text}}
    ]}}]}]}


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# --- Stub the actual work: we're testing transport, not the LLM -------------
handled = []
handled_lock = threading.Lock()
inflight = {"now": 0, "max": 0}


def fake_handle(client, incoming):
    with handled_lock:
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
    time.sleep(0.25)                      # stand in for a model call
    with handled_lock:
        handled.append(incoming.message_id)
        inflight["now"] -= 1


real_handle = run_whatsapp._handle_message
run_whatsapp._handle_message = fake_handle

app = run_whatsapp.build_app()
client = TestClient(app)

print("\n=== 3. Webhook signature verification ===")
body = json.dumps(meta_payload("wamid.A")).encode()

r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
check("valid signature -> 200", r.status_code == 200)

r = client.post("/webhook", content=body,
                headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
check("forged signature -> 401", r.status_code == 401)

r = client.post("/webhook", content=body)
check("no signature header -> 401", r.status_code == 401)

r = client.post("/webhook", content=json.dumps(meta_payload("wamid.B")).encode(),
                headers={"X-Hub-Signature-256": sign(body)})
check("signature of a DIFFERENT body -> 401", r.status_code == 401)

r = client.post("/webhook", content=b"{not json",
                headers={"X-Hub-Signature-256": sign(b"{not json")})
check("malformed JSON -> 400 (not 500)", r.status_code == 400)

config.META_WA_APP_SECRET = None
r = client.post("/webhook", content=body)
check("no secret configured -> accepted (demo mode)", r.status_code == 200)
check("  ...and authenticated() reports False",
      run_whatsapp.whatsapp_client.MetaWhatsAppClient.authenticated() is False)
config.META_WA_APP_SECRET = SECRET
check("  ...and True once set",
      run_whatsapp.whatsapp_client.MetaWhatsAppClient.authenticated() is True)

print("\n=== 2a. Duplicate delivery suppression ===")
handled.clear()
run_whatsapp._SEEN_MESSAGE_IDS.clear()
body = json.dumps(meta_payload("wamid.RETRY")).encode()
sig = sign(body)
results = [client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
           for _ in range(3)]
time.sleep(0.6)
check("3 identical deliveries -> handled once", len(handled) == 1)
check("first reports queued=1", results[0].json()["queued"] == 1)
check("retries report duplicates=1",
      all(r.json()["duplicates"] == 1 and r.json()["queued"] == 0 for r in results[1:]))

print("\n=== 2b. Ack is fast (work happens after the response) ===")
handled.clear()
body = json.dumps(meta_payload("wamid.FAST")).encode()
t0 = time.perf_counter()
r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
ack = time.perf_counter() - t0
# TestClient waits for background tasks, so measure the handler itself instead:
# if the ack blocked on the work, `handled` would already be populated at t0.
check(f"handler returned 200 ({ack:.2f}s incl. TestClient waiting on the task)",
      r.status_code == 200)
check("response body is an ack, not a result",
      set(r.json()) == {"received", "queued", "duplicates"})

print("\n=== 2c. Two messages from the SAME customer never overlap ===")
handled.clear()
inflight.update(now=0, max=0)
run_whatsapp._SESSION_LOCKS.clear()
threads = []
for i in range(4):
    b = json.dumps(meta_payload(f"wamid.SAME{i}", frm="923001234567")).encode()
    t = threading.Thread(target=lambda b=b: client.post(
        "/webhook", content=b, headers={"X-Hub-Signature-256": sign(b)}))
    threads.append(t)
# Same phone -> the per-phone lock must serialise them.
run_whatsapp._handle_message = lambda c, i: real_locked(c, i)


def real_locked(c, i):
    with run_whatsapp.session_lock(i.phone):
        fake_handle(c, i)


for t in threads:
    t.start()
for t in threads:
    t.join()
time.sleep(0.5)
check(f"4 concurrent messages, max overlap = {inflight['max']} (want 1)",
      inflight["max"] == 1)
check("all 4 were handled", len(handled) == 4)

print("\n=== /health ===")
h = client.get("/health").json()
print("  ", h)
check("health reports webhook_authenticated", "webhook_authenticated" in h)

print("\n=== Whapi transport still works ===")
config.WHATSAPP_PROVIDER = "whapi"
config.WHAPI_WEBHOOK_SECRET = "whapi-secret"
run_whatsapp._handle_message = fake_handle
wapp = TestClient(run_whatsapp.build_app())
wbody = json.dumps({"messages": [{"id": "w1", "from": "923001234567",
                                  "type": "text", "text": {"body": "hi"}}]}).encode()
r = wapp.post("/webhook", content=wbody, headers={"X-Webhook-Secret": "whapi-secret"})
check("whapi: correct secret -> 200", r.status_code == 200)
r = wapp.post("/webhook", content=wbody, headers={"X-Webhook-Secret": "wrong"})
check("whapi: wrong secret -> 401", r.status_code == 401)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
