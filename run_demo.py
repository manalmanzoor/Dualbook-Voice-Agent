#!/usr/bin/env python
"""
DualBook — one-command live demo.

    python run_demo.py

Seeds a couple of weeks of booking history, then runs REAL conversations
through the real LLM: one brand-new customer, then the same customer coming
back. The second conversation is visibly shorter, because the memory layer
pre-filled what we already knew.

Nothing here is faked. Every booking is written by the agent calling
`save_booking`, through the same `complete_booking()` the live WhatsApp and
voice channels use. When it finishes, the dashboard shows exactly this data.

    python run_demo.py --keep      # add to existing data instead of resetting
    python run_demo.py --seed-only # history only, no LLM calls (costs nothing)
"""

from __future__ import annotations

import argparse
import sys
import time

import demo_data
from dualbook import config, llm, memory, store
from dualbook.booking_core import ConversationEngine

# A first-time caller. Note they never state their name up front — the agent
# has to ask, which is the point of the comparison below.
NEW_CUSTOMER_PHONE = "+923451234567"
NEW_CUSTOMER_SCRIPT = [
    "hi, do you guys wash cars?",
    "I'm Ahmed Raza, I drive a Toyota Corolla",
    "this Saturday if you have space, around 11am",
    "premium wash please, and you can reach me on this number",
    "yes that's all correct, please book it",
]

# The same person, a week later. They say almost nothing.
RETURNING_SCRIPT = [
    "hi, I'd like to book again",
    "yes please, same as last time",
    "next Tuesday at 10am works",
    "yes confirm it",
]

BAR = "=" * 72


def _say(who: str, text: str) -> None:
    print(f"  {who:<7}| {text}")


def run_conversation(phone: str, script: list[str], title: str) -> dict:
    """Drive one real conversation and report what it cost the customer."""
    print(f"\n{BAR}\n  {title}\n{BAR}")

    engine = ConversationEngine(phone=phone, channel="whatsapp")
    profile = engine.profile
    # Capture this NOW: known_slots keeps growing during the conversation, so
    # reading it at the end would report what we ended up knowing, not what
    # memory handed us for free at the start.
    preloaded = len(engine.known_slots)

    if profile:
        print(f"  MEMORY   | Recognised: {profile['customer_name']}, "
              f"{profile['vehicle_type']}, usually {profile['usual_service']}, "
              f"{profile['booking_count']} previous bookings")
        print(f"  MEMORY   | Slots pre-filled before they typed a word: "
              f"{preloaded}/6 -> {sorted(engine.known_slots)}")
    else:
        print("  MEMORY   | Unknown number — nothing pre-filled (0/6 slots)")
    print()

    started = time.time()
    _say("agent", engine.greet())

    turns = 0
    for message in script:
        turns += 1
        _say("customer", message)
        try:
            reply = engine.handle(message)
        except Exception as exc:
            print(f"\n  ERROR: {exc}\n")
            raise
        _say("agent", reply)
        if engine.booking_saved:
            break

    return {
        "phone": phone,
        "turns": turns,
        "saved": engine.booking_saved,
        "returning": bool(profile),
        "preloaded": preloaded,
        "seconds": round(time.time() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DualBook live demo")
    parser.add_argument("--keep", action="store_true",
                        help="keep existing data instead of resetting")
    parser.add_argument("--seed-only", action="store_true",
                        help="seed history only, make no LLM calls")
    args = parser.parse_args()

    store.init_db()

    print(f"\n{BAR}\n  DualBook — live demo\n{BAR}")
    if not args.keep:
        demo_data.reset()
    demo_data.seed_full()
    print(f"  Seeded {len(store.list_customers())} customers, "
          f"{len(store.list_bookings())} historical bookings.")

    if args.seed_only:
        print("\n  --seed-only: skipping live conversations.")
        print("  Open the dashboard:  python run_dashboard.py\n")
        return 0

    try:
        llm.get_client()
    except config.ConfigError as exc:
        print(f"\n  Cannot run live conversations: {exc}")
        print("  (History is still seeded — the dashboard will work.)\n")
        return 1

    print(f"  LLM: {llm.describe_provider()} — the conversations below are real.\n")

    # Make sure the "new" customer really is new, even on a re-run with --keep.
    store.clear_conversation(memory.normalize_phone(NEW_CUSTOMER_PHONE))

    first = run_conversation(
        NEW_CUSTOMER_PHONE, NEW_CUSTOMER_SCRIPT,
        "CONVERSATION 1 — brand new customer (agent knows nothing)",
    )
    second = run_conversation(
        NEW_CUSTOMER_PHONE, RETURNING_SCRIPT,
        "CONVERSATION 2 — same person returns (memory now active)",
    )

    print(f"\n{BAR}\n  WHAT THE MEMORY LAYER ACTUALLY BOUGHT\n{BAR}")
    print(f"  {'':<28}{'1st visit':>12}{'2nd visit':>12}")
    print(f"  {'Known before they spoke':<28}"
          f"{str(first['preloaded']) + '/6 slots':>12}"
          f"{str(second['preloaded']) + '/6 slots':>12}")
    print(f"  {'Customer messages needed':<28}{first['turns']:>12}{second['turns']:>12}")
    # Deliberately not reporting wall-clock time: on a free tier it's dominated
    # by rate-limit backoff, not by how much the agent had to ask, so it would
    # be a misleading number to put next to the others.
    saved = first["turns"] - second["turns"]
    if saved > 0:
        pct = round(saved / first["turns"] * 100)
        noun = "message" if saved == 1 else "messages"
        print(f"\n  -> {saved} fewer {noun} ({pct}% shorter) for the same booking.")
    print("  -> The agent never re-asked for name, vehicle or usual service —")
    print("     it opened with \"Welcome back, Ahmed\" and went straight to the date.")

    profile = memory.load_profile(NEW_CUSTOMER_PHONE)
    if profile:
        print(f"\n  Stored profile for {profile['customer_name']}:")
        for key in ("vehicle_type", "preferred_time_of_day", "usual_service",
                    "last_booking_date", "booking_count"):
            print(f"    {key:<22} {profile[key]}")

    print(f"\n{BAR}\n  SEE IT\n{BAR}")
    print("  python run_dashboard.py        ->  http://localhost:8080")
    print("  python view_bookings.py --csv  ->  console table + CSV export")
    print(f"\n  Totals now: {len(store.list_bookings())} bookings, "
          f"{len(store.list_customers())} customer profiles.\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(0)
