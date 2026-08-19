"""Exercise the template / session-window send path against a throwaway DB."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath("."))
os.environ["WHATSAPP_PROVIDER"] = "meta"

from dualbook import config, notify, store, wa_templates  # noqa: E402

config.WHATSAPP_PROVIDER = "meta"
# Force SIMULATED mode regardless of what .env holds. Without this the suite
# reads real credentials and fires real WhatsApp messages at a real handset
# every time it runs — tests must not be able to reach a customer.
config.META_WA_TOKEN = None
config.META_WA_PHONE_NUMBER_ID = None
DB = os.path.join(tempfile.mkdtemp(), "t.db")
store.init_db(DB)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))


def booking(phone="+923001234567", channel="whatsapp"):
    return store.Booking(
        phone=phone, customer_name="Ali Raza", vehicle_type="Honda Civic",
        preferred_date="2026-08-20", preferred_time="10:00",
        service_type="Premium Wash", contact_details=phone, channel=channel,
    )


def last_row():
    return store.list_outbound(1, db_path=DB)[0]


print("\n=== Template config loads and validates ===")
t = wa_templates.get("booking_confirmation")
check("booking_confirmation template is declared", t is not None)
check("  name is Meta-legal", t and t.name == "booking_confirmation", t.name if t else "")
check("  language is a full locale", t and t.language == "en_US", t.language if t else "")
check("  6 variables for 6 placeholders", t and len(t.variables) == 6)
check("_README key is skipped", "_README" not in wa_templates.load())

vals = {"name": "Ali", "service": "Premium Wash", "business": "SparkleWash",
        "date": "2026-08-20", "time": "10:00", "id": 42}
rendered = t.render(vals)
check("renders a real preview", "Ali" in rendered and "#42" in rendered, rendered[:56] + "...")
comps = t.components(vals)
check("builds Meta components", comps[0]["type"] == "body"
      and len(comps[0]["parameters"]) == 6
      and comps[0]["parameters"][0] == {"type": "text", "text": "Ali"})

print("\n=== Bad declarations are caught locally, not at Meta ===")
for label, spec in [
    ("count mismatch", {"name": "x", "language": "en_US",
                        "body_text": "Hi {{1}} {{2}}", "variables": ["name"]}),
    ("gap in placeholders", {"name": "x", "language": "en_US",
                             "body_text": "Hi {{1}} {{3}}", "variables": ["a", "b", "c"]}),
    ("uppercase name", {"name": "Booking_Confirm", "language": "en_US",
                        "body_text": "hi", "variables": []}),
    ("missing language", {"name": "x", "language": "",
                          "body_text": "hi", "variables": []}),
]:
    try:
        wa_templates._parse("k", spec)
        check(f"rejects {label}", False)
    except wa_templates.TemplateError:
        check(f"rejects {label}", True)

print("\n=== The 24-hour window decides the shape ===")
NEW = "+923009990001"
check("never messaged us -> outside window",
      notify.within_session_window(NEW, db_path=DB) is False)
check("  ...so auto mode picks template",
      notify.choose_mode(NEW, db_path=DB) == "template")

store.record_inbound(NEW, db_path=DB)
check("after an inbound -> inside window",
      notify.within_session_window(NEW, db_path=DB) is True)
check("  ...so auto mode picks session",
      notify.choose_mode(NEW, db_path=DB) == "session")

# Age the row past the window.
old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
with store.connect(DB) as conn:
    conn.execute("UPDATE wa_contacts SET last_inbound_at = ? WHERE phone = ?", (old, NEW))
check("25 hours later -> window has closed",
      notify.within_session_window(NEW, db_path=DB) is False)

check("explicit use_template=True overrides an open window",
      notify.choose_mode("+923001234567", use_template=True, db_path=DB) == "template")
check("explicit use_template=False overrides a closed window",
      notify.choose_mode(NEW, use_template=False, db_path=DB) == "session")

print("\n=== Whapi has no window, so it never needs a template ===")
config.WHATSAPP_PROVIDER = "whapi"
check("whapi always picks session", notify.choose_mode(NEW, db_path=DB) == "session")
config.WHATSAPP_PROVIDER = "meta"

print("\n=== Simulated mode still works, in BOTH shapes ===")
r = notify.booking_confirmation(booking(), 101, db_path=DB)
check("cold number simulates as a template",
      r["status"] == "simulated" and r["mode"] == "template", f"mode={r['mode']}")

store.record_inbound("+923001234567", db_path=DB)
r = notify.booking_confirmation(booking(), 102, db_path=DB)
check("after inbound, same booking simulates as session",
      r["status"] == "simulated" and r["mode"] == "session", f"mode={r['mode']}")
check("  outbox recorded the mode", last_row()["mode"] == "session")
check("  outbox body is the owner's free text",
      "SparkleWash" in last_row()["body"])

VOICE = "+923007778899"
r = notify.booking_confirmation(booking(phone=VOICE, channel="voice"), 103, db_path=DB)
check("voice booking (never messaged us) simulates as TEMPLATE",
      r["status"] == "simulated" and r["mode"] == "template", f"mode={r['mode']}")
row = last_row()
check("  outbox previews the approved body", "Booking #103" in row["body"], row["body"][:56])
check("  outbox mode is template", row["mode"] == "template")

print("\n=== With no template declared, out-of-window is blocked, not attempted ===")
_real_get = wa_templates.get
wa_templates.get = lambda kind, path=None: None
notify.wa_templates.get = wa_templates.get
r = notify.booking_confirmation(booking(phone="+923004445566", channel="voice"), 104, db_path=DB)
check("blocked rather than sent to fail", r["status"] == "blocked", r.get("reason", "")[:60])
check("  reason names the fix", "template" in r["reason"].lower())
check("  recorded in the outbox", last_row()["status"] == "blocked")
wa_templates.get = _real_get
notify.wa_templates.get = _real_get

print("\n=== Regressions ===")
r = notify.send_whatsapp("not-a-number", "hi", kind="test", db_path=DB)
check("invalid number still rejected before anything else", r["status"] == "rejected")
r = notify.send_whatsapp("+923001234567", "ping", kind="test", db_path=DB)
check("dashboard test message still works (in window)", r["status"] == "simulated")
ts = notify.transport_status()
check("transport_status still reports configured/mode",
      ts["configured"] is False and ts["mode"] == "simulated")
kinds = {t["kind"] for t in ts["templates"]}
check("  ...and now reports the window + templates",
      ts["enforces_window"] is True and {"booking_confirmation", "test"} <= kinds,
      f"declared: {sorted(kinds)}")

print("\n=== 'Send test' works on a cold number (hello_world) ===")
COLD = "+923019052602"
check("cold number is outside the window",
      notify.within_session_window(COLD, db_path=DB) is False)
r = notify.send_whatsapp(COLD, "Test message.", kind="test", db_path=DB)
check("test send is no longer blocked", r["status"] == "simulated", f"status={r['status']}")
check("  ...and goes as a template", r["mode"] == "template")
check("  ...using Meta's pre-approved hello_world",
      wa_templates.get("test").name == "hello_world")

store.record_inbound(COLD, db_path=DB)
r = notify.send_whatsapp(COLD, "Typed message.", kind="test", db_path=DB)
check("once they reply, the typed text is sent instead",
      r["mode"] == "session" and r["body"] == "Typed message.")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
