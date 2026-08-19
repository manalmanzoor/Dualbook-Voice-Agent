"""Exercise the REAL _handle_message: locking, error containment, expiry."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath("."))
os.environ["WHATSAPP_PROVIDER"] = "meta"

import run_whatsapp  # noqa: E402
from dualbook.whatsapp_client import IncomingMessage  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name)


inflight = {"now": 0, "max": 0}
order = []
lock = threading.Lock()


class FakeEngine:
    booking_saved = False
    profile = None

    def handle(self, text):
        with lock:
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
        time.sleep(0.15)
        with lock:
            order.append(text)
            inflight["now"] -= 1
        return f"reply to {text}"


class FakeClient:
    sent = []

    def send_text(self, to, body):
        FakeClient.sent.append((to, body))
        return {"ok": True}


engine = FakeEngine()
run_whatsapp.get_session = lambda phone: engine
client = FakeClient()


def msg(text, phone="+923001234567"):
    return IncomingMessage(chat_id=phone, phone=phone, text=text, message_id=text)


print("\n=== Real _handle_message serialises the same customer ===")
threads = [threading.Thread(target=run_whatsapp._handle_message,
                            args=(client, msg(f"m{i}"))) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(f"max concurrent turns for one phone = {inflight['max']} (want 1)",
      inflight["max"] == 1)
check("all 5 turns ran", len(order) == 5)
check("all 5 replies sent", len(FakeClient.sent) == 5)

print("\n=== Different customers still run in parallel ===")
inflight.update(now=0, max=0)
order.clear()
threads = [threading.Thread(target=run_whatsapp._handle_message,
                            args=(client, msg(f"p{i}", phone=f"+9230011122{i:02d}")))
           for i in range(4)]
t0 = time.perf_counter()
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.perf_counter() - t0
check(f"4 different phones overlapped (max={inflight['max']}, {elapsed:.2f}s)",
      inflight["max"] > 1)

print("\n=== A failing turn cannot escape into the threadpool ===")


class Exploding:
    booking_saved = False
    profile = None

    def handle(self, text):
        raise RuntimeError("model exploded")


run_whatsapp.get_session = lambda phone: Exploding()
try:
    run_whatsapp._handle_message(client, msg("boom"))
    check("exception swallowed and logged, not raised", True)
except Exception as exc:
    check(f"exception swallowed (got {exc!r})", False)

print("\n=== Lock is released even when the turn fails ===")
lk = run_whatsapp.session_lock("+923001234567")
check("lock not left held after a failure", lk.acquire(blocking=False))
lk.release()

print("\n=== Idle sessions are pruned ===")
run_whatsapp.get_session = run_whatsapp.__dict__["get_session"]
run_whatsapp._SESSIONS.clear()
run_whatsapp._SESSION_SEEN.clear()
run_whatsapp._SESSIONS["+92300000001"] = engine
run_whatsapp._SESSION_SEEN["+92300000001"] = time.monotonic() - (
    run_whatsapp.SESSION_IDLE_TIMEOUT + 60)
run_whatsapp._SESSIONS["+92300000002"] = engine
run_whatsapp._SESSION_SEEN["+92300000002"] = time.monotonic()
with run_whatsapp._REGISTRY_LOCK:
    run_whatsapp._prune_sessions(time.monotonic())
check("stale session dropped", "+92300000001" not in run_whatsapp._SESSIONS)
check("fresh session kept", "+92300000002" in run_whatsapp._SESSIONS)

print("\n=== Dedupe cache stays bounded ===")
run_whatsapp._SEEN_MESSAGE_IDS.clear()
for i in range(run_whatsapp._SEEN_LIMIT + 500):
    run_whatsapp.claim_message(f"id{i}")
check(f"cache capped at {run_whatsapp._SEEN_LIMIT} "
      f"(is {len(run_whatsapp._SEEN_MESSAGE_IDS)})",
      len(run_whatsapp._SEEN_MESSAGE_IDS) == run_whatsapp._SEEN_LIMIT)
check("oldest id evicted", "id0" not in run_whatsapp._SEEN_MESSAGE_IDS)
check("newest id retained",
      f"id{run_whatsapp._SEEN_LIMIT + 499}" in run_whatsapp._SEEN_MESSAGE_IDS)
check("message with no id is always claimed", run_whatsapp.claim_message(None) is True)
check("  ...twice", run_whatsapp.claim_message(None) is True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
