import os, sys
from datetime import datetime, date, timedelta
sys.path.insert(0, os.path.abspath("."))
from dualbook import validate, settings_store

s = settings_store.get()
PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))

# Wednesday 19 Aug 2026, 18:30. Business is open 09:00-19:00 that day.
EVENING = datetime(2026, 8, 19, 18, 30)
TODAY = EVENING.date().isoformat()

print("\n=== same-day times that have passed (now = 18:30) ===")
for t in ["09:00", "10:00 AM", "2:00 PM", "6:00 PM"]:
    r = validate.booking_slot(TODAY, t, s, now=EVENING)
    check(f"rejects {t} today", not r.ok, (r.reason or "")[:70])

print("\n=== same-day times still ahead ===")
r = validate.booking_slot(TODAY, "6:45 PM", s, now=EVENING)
check("accepts 6:45 PM today", r.ok, r.reason or "")

print("\n=== after closing, it should point at another day ===")
LATE = datetime(2026, 8, 19, 19, 30)
r = validate.booking_slot(TODAY, "10:00 AM", s, now=LATE)
check("rejects and says no slots left", not r.ok and "no slots left today" in (r.reason or ""),
      (r.reason or "")[:90])

print("\n=== future days are unaffected ===")
tom = (EVENING.date() + timedelta(days=1)).isoformat()   # Thu 20 Aug
r = validate.booking_slot(tom, "09:00", s, now=EVENING)
check("accepts 09:00 tomorrow", r.ok, r.reason or "")
r = validate.booking_slot(tom, "10:00 AM", s, now=EVENING)
check("accepts a morning slot tomorrow", r.ok, r.reason or "")

print("\n=== previously-working rules still hold ===")
r = validate.booking_slot("2026-08-01", "11:00", s, now=EVENING)
check("past DATE still rejected", not r.ok and "in the past" in (r.reason or ""))
r = validate.booking_slot("2026-08-23", "11:00", s, now=EVENING)   # Sunday
check("closed day still rejected", not r.ok and "closed on Sunday" in (r.reason or ""))
r = validate.booking_slot(tom, "8:30 PM", s, now=EVENING)
check("outside opening hours still rejected", not r.ok and "outside our" in (r.reason or ""))
r = validate.booking_slot("2027-01-01", "11:00", s, now=EVENING)
check("beyond booking horizon still rejected", not r.ok and "days ahead" in (r.reason or ""))
r = validate.booking_slot("not-a-date", "11:00", s, now=EVENING)
check("unparseable date still rejected", not r.ok and "not a usable date" in (r.reason or ""))

print("\n=== boundary: exactly now ===")
r = validate.booking_slot(TODAY, "6:30 PM", s, now=EVENING)
check("rejects a slot at exactly the current minute", not r.ok)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
