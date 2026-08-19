#!/usr/bin/env python
"""
Seed script — creates returning customers, a week of booking history, and two
in-flight conversations so the personalisation and the dashboard can both be
demoed without completing a real booking first.

    python demo_data.py              # seed
    python demo_data.py --reset      # wipe everything, then seed
    python demo_data.py --minimal    # just the one returning customer (Ali)
    python demo_data.py --clear-demo # remove the seeded rows, KEEP real bookings

After seeding:

    python run_dashboard.py                         -> populated dashboard
    python run_whatsapp.py simulate +923001234567    -> "Welcome back, Ali..."
    python run_whatsapp.py simulate +923339998877    -> new customer
"""

from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta, timezone

from dualbook import memory, store

# The returning customer the README walks through.
DEMO_PHONE = "+923001234567"

SERVICES = ["Premium Wash", "Express Wash", "Interior Detail", "Full Detail"]

# (phone, name, vehicle, time_of_day, usual_service, bookings)
# Sized so the dashboard looks like a real operating business rather than a
# toy: enough customers for the insight percentages to mean something, and a
# healthy mix of loyal regulars and first-timers.
CUSTOMERS = [
    (DEMO_PHONE, "Ali Khan", "SUV", "morning", "Premium Wash", 6),
    ("+923017654321", "Fatima Noor", "Sedan", "afternoon", "Express Wash", 5),
    ("+923029876543", "Usman Tariq", "Hatchback", "morning", "Express Wash", 4),
    ("+923035556677", "Ayesha Malik", "SUV", "afternoon", "Premium Wash", 5),
    ("+923041112233", "Bilal Hussain", "Pickup", "evening", "Full Detail", 3),
    ("+923054445566", "Zainab Sheikh", "Sedan", "morning", "Premium Wash", 4),
    ("+923062223344", "Hamza Iqbal", "SUV", "morning", "Interior Detail", 3),
    ("+923078889900", "Maryam Raza", "Hatchback", "evening", "Express Wash", 2),
    ("+923083334455", "Omar Farooq", "SUV", "morning", "Premium Wash", 4),
    ("+923096667788", "Sana Javed", "Sedan", "afternoon", "Interior Detail", 3),
    ("+923101239876", "Imran Baig", "Pickup", "morning", "Full Detail", 2),
    ("+923117778899", "Hina Aslam", "Hatchback", "afternoon", "Express Wash", 2),
    ("+923124445511", "Danish Ali", "SUV", "evening", "Premium Wash", 2),
    ("+923132228833", "Nida Qureshi", "Sedan", "morning", "Express Wash", 1),
]

TIME_SLOTS = {
    "morning": ["9:00 AM", "9:30 AM", "10:00 AM", "11:00 AM"],
    "afternoon": ["12:30 PM", "1:00 PM", "2:00 PM", "3:30 PM"],
    "evening": ["5:00 PM", "6:00 PM", "6:30 PM", "7:00 PM"],
}


def backfill() -> int:
    """
    Fill `turns` and `status` on SEEDED rows written before those columns existed.

    Scoped deliberately to rows this script created (`notes = 'Seeded demo
    booking'`). Real bookings are left alone: inventing a turn count for a
    conversation that actually happened would corrupt the one metric the
    Analytics page exists to report. A real row with no turn count is reported
    as untracked, never estimated.
    """
    rng = random.Random(7)
    today = date.today()
    seen: dict[str, int] = {}
    updated = 0

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, phone, preferred_date, turns, status FROM bookings "
            "WHERE notes = 'Seeded demo booking' ORDER BY id"
        ).fetchall()
        for row in rows:
            prior = seen.get(row["phone"], 0)
            seen[row["phone"]] = prior + 1
            # Same rule as seed_full: cold start vs starting from a profile.
            turns = row["turns"] or (rng.randint(2, 4) if prior else rng.randint(6, 9))
            # A past appointment that is still sitting at 'confirmed' was never
            # marked off — which is exactly what an upgraded database looks like.
            past = (row["preferred_date"] or "") < today.isoformat()
            status = "completed" if (past and row["status"] == "confirmed") else row["status"]
            if turns == row["turns"] and status == row["status"]:
                continue
            conn.execute(
                "UPDATE bookings SET turns = ?, status = ? WHERE id = ?",
                (turns, status, row["id"]),
            )
            updated += 1

    print(f"Backfilled {updated} seeded booking(s) with turn counts and status.")
    return updated


def reset() -> None:
    with store.connect() as conn:
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM customers")
        conn.execute("DELETE FROM conversations")
    print("Cleared bookings, customers and conversations.")


def clear_demo() -> int:
    """
    Remove ONLY the seeded customers, leaving real bookings untouched.

    `--reset` deletes everything, which is right before a demo and badly wrong
    once the agent has taken genuine bookings. This targets the numbers declared
    in CUSTOMERS above — the only rows this script ever created — so the
    dashboard drops to showing real business and nothing else.

    A backup is written first. Deleting someone's booking history on the
    strength of a phone-number match is not something to do without a way back.
    """
    import shutil
    from pathlib import Path

    from dualbook import config

    db = Path(config.DB_PATH)
    backup = db.with_suffix(f".backup-{datetime.now():%Y%m%d-%H%M%S}.db")
    if db.exists():
        shutil.copy2(db, backup)
        print(f"Backup written to {backup.name}")

    phones = [DEMO_PHONE] + [c[0] for c in CUSTOMERS]
    phones += ["+923339998877"]  # the "new customer" used by the live-panel demo
    seen: set[str] = set()
    unique = [p for p in phones if not (p in seen or seen.add(p))]
    marks = ",".join("?" for _ in unique)

    removed: dict[str, int] = {}
    with store.connect() as conn:
        for table in ("bookings", "customers", "conversations", "outbox", "wa_contacts"):
            cur = conn.execute(
                f"DELETE FROM {table} WHERE phone IN ({marks})", unique
            )
            removed[table] = cur.rowcount

    total = sum(removed.values())
    print(f"Removed {total} seeded row(s) across {len(unique)} demo numbers:")
    for table, count in removed.items():
        if count:
            print(f"  {table:15} {count}")
    with store.connect() as conn:
        left = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    print(f"\n{left} real booking(s) kept.")
    return total


def seed_minimal() -> None:
    """Just the single returning customer — the original demo."""
    store.save_booking(
        store.Booking(
            phone=DEMO_PHONE,
            customer_name="Ali Khan",
            vehicle_type="SUV",
            preferred_date=(date.today() - timedelta(days=24)).isoformat(),
            preferred_time="9:30 AM",
            service_type="Premium Wash",
            contact_details=DEMO_PHONE,
            channel="whatsapp",
            notes="Seeded demo booking",
            status="completed",
            turns=7,
        )
    )
    store.upsert_customer(
        {
            "phone": DEMO_PHONE,
            "customer_name": "Ali Khan",
            "vehicle_type": "SUV",
            "preferred_time_of_day": "morning",
            "usual_service": "Premium Wash",
            "last_booking_date": (date.today() - timedelta(days=24)).isoformat(),
            "booking_count": 3,
        }
    )


def seed_full() -> None:
    """A week of history across both channels, plus two live conversations."""
    rng = random.Random(42)  # deterministic, so the demo looks the same each run
    today = date.today()
    now = datetime.now(timezone.utc)

    # Bookings are spread over the last two weeks, weighted towards recent days
    # so the 7-day trend slopes upward — which is what a growing business
    # actually looks like, and reads correctly on the dashboard.
    recent_weights = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7]

    for phone, name, vehicle, tod, service, count in CUSTOMERS:
        last_date = None
        for i in range(count):
            # How many days ago the booking was MADE (0 = today).
            days_ago = 13 - rng.choices(range(14), weights=recent_weights)[0]
            created = now - timedelta(
                days=days_ago, hours=rng.randint(0, 13), minutes=rng.randint(0, 59)
            )
            # The appointment itself is same-day or a few days after booking,
            # so some sit in the future ("Confirmed") and some in the past
            # ("Completed").
            booking_date = created.date() + timedelta(days=rng.randint(0, 3))
            svc = service if rng.random() < 0.7 else rng.choice(SERVICES)
            # Turns to book. The FIRST booking from a number is a cold start —
            # name, vehicle, number, date, time, service all from scratch. Later
            # ones start from a profile, so they land in 2-4. This is the shape
            # the memory layer produces in real runs (see analytics.memory_impact),
            # seeded here so the Analytics page has a history to show on a fresh
            # install rather than a chart with two points on it.
            turns = rng.randint(6, 9) if i == 0 else rng.randint(2, 4)
            if booking_date < today:
                status = "cancelled" if rng.random() < 0.06 else "completed"
            else:
                status = "confirmed"
            store.save_booking(
                store.Booking(
                    phone=phone,
                    customer_name=name,
                    vehicle_type=vehicle,
                    preferred_date=booking_date.isoformat(),
                    preferred_time=rng.choice(TIME_SLOTS[tod]),
                    service_type=svc,
                    contact_details=phone,
                    channel="voice" if rng.random() < 0.35 else "whatsapp",
                    notes="Seeded demo booking",
                    created_at=created.isoformat(timespec="seconds"),
                    status=status,
                    turns=turns,
                )
            )
            last_date = max(last_date or booking_date, booking_date)

        store.upsert_customer(
            {
                "phone": phone,
                "customer_name": name,
                "vehicle_type": vehicle,
                "preferred_time_of_day": tod,
                "usual_service": service,
                "last_booking_date": (last_date or today).isoformat(),
                "booking_count": count,
            }
        )

    # A few appointments dated TODAY. Without these, "Today's Bookings" depends
    # on where the random dates happened to land and can read 0 — which looks
    # like a broken dashboard rather than a quiet morning.
    today_slots = [
        (CUSTOMERS[0], "9:00 AM"), (CUSTOMERS[3], "11:30 AM"),
        (CUSTOMERS[1], "1:00 PM"), (CUSTOMERS[5], "2:30 PM"),
        (CUSTOMERS[8], "4:00 PM"),
    ]
    for (phone, name, vehicle, tod, service, _count), slot in today_slots:
        store.save_booking(
            store.Booking(
                phone=phone, customer_name=name, vehicle_type=vehicle,
                preferred_date=today.isoformat(), preferred_time=slot,
                service_type=service, contact_details=phone,
                channel="voice" if rng.random() < 0.35 else "whatsapp",
                notes="Seeded demo booking",
                created_at=(now - timedelta(hours=rng.randint(1, 20))).isoformat(
                    timespec="seconds"
                ),
                status="confirmed",
                turns=rng.randint(2, 4),   # all returning customers by now
            )
        )

    # Two in-flight conversations for the Live Conversations panel. The slot
    # lists are exactly what the engine would have known at this point: the
    # returning customer starts ahead because memory pre-filled their details.
    store.upsert_conversation(
        phone=DEMO_PHONE,
        channel="whatsapp",
        status="active",
        last_message="Hi, I want to book a car wash",
        turn_count=1,
        slots=["customer_name", "vehicle_type", "service_type", "contact_details"],
        is_returning=True,
        customer_name="Ali Khan",
    )
    store.upsert_conversation(
        phone="+923339998877",
        channel="voice",
        status="active",
        last_message="I'd like to schedule a wash for my SUV",
        turn_count=2,
        slots=["customer_name", "vehicle_type", "contact_details"],
        is_returning=False,
        customer_name="Sara Ahmed",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed DualBook demo data")
    parser.add_argument("--reset", action="store_true", help="clear tables first")
    parser.add_argument(
        "--minimal", action="store_true", help="seed only the single demo customer"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="fill turns/status on already-seeded rows, add nothing new "
        "(run this once after upgrading an existing database)",
    )
    parser.add_argument(
        "--clear-demo",
        action="store_true",
        help="remove the seeded demo customers and KEEP real bookings "
        "(backs the database up first)",
    )
    args = parser.parse_args()

    store.init_db()
    if args.backfill:
        backfill()
        return 0
    if args.clear_demo:
        clear_demo()
        return 0
    if args.reset:
        reset()

    if args.minimal:
        seed_minimal()
    else:
        seed_full()

    customers = store.list_customers()
    bookings = store.list_bookings()
    convos = store.list_conversations()

    print(f"\nSeeded {len(customers)} customers, {len(bookings)} bookings, "
          f"{len(convos)} live conversations.")

    profile = memory.load_profile(DEMO_PHONE)
    print("\nHeadline returning customer:")
    for key, value in (profile or {}).items():
        print(f"  {key:<22} {value}")

    print("\nTry it:")
    print("  python run_dashboard.py                         # dashboard at :8080")
    print(f"  python run_whatsapp.py simulate {DEMO_PHONE}   # returning -> by name")
    print("  python run_whatsapp.py simulate +923449998877   # new       -> asked all")
    print("  python view_bookings.py                         # console view\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
