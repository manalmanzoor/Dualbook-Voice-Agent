"""
Customer memory layer.
=====================

Three deliberate design decisions, one per section below. The long-form
rationale (and the latency/quality tradeoffs) is in MEMORY_DESIGN.md; the
comments here state the *why* at the point where the code makes the choice.

    1. WHAT TO WRITE   -> a structured, typed schema. Not generic extraction.
    2. HOW TO RETRIEVE -> pre-loaded into the system prompt at session start.
                          No per-turn semantic search.
    3. WHERE PROCESSING HAPPENS -> post-processing write after the booking
                          completes + a single pre-load read at session start.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from . import store

# ---------------------------------------------------------------------------
# DECISION 1 — WHAT TO WRITE: a structured schema, not generic extraction.
# ---------------------------------------------------------------------------
# A generic memory layer would take the transcript and ask an LLM to "extract
# anything worth remembering". For a domain-specific agent that is the wrong
# trade: it is non-deterministic, it costs an extra LLM round-trip per booking,
# and it accumulates junk ("customer said thanks", "customer seemed rushed")
# that will never change what the agent asks next turn.
#
# Instead we enumerate the small set of facts that DEMONSTRABLY shorten a future
# booking. Every field below maps to a question the agent would otherwise have
# to ask. If a candidate fact doesn't remove a question, it doesn't belong here.
PROFILE_FIELDS = (
    "customer_name",          # -> lets us greet by name, skip "what's your name?"
    "vehicle_type",           # -> "same SUV as last time?"
    "preferred_time_of_day",  # -> propose a slot instead of asking open-ended
    "usual_service",          # -> "Premium Wash again?"
    "last_booking_date",      # -> recency framing ("you were in on 12 July")
    "booking_count",          # -> lets us distinguish first-timer / regular / VIP
)

# Everything NOT stored, stated explicitly so the boundary is reviewable:
#   - raw transcripts or message history
#   - greetings, small talk, filler, sentiment
#   - anything the customer did not volunteer as a booking fact
#   - free-text impressions of the customer


def normalize_phone(raw: str) -> str:
    """
    Canonical phone key. The memory layer is keyed on phone across BOTH
    channels, so WhatsApp `923001234567@s.whatsapp.net` and a voice call from
    `+92 300 123 4567` must collapse to the same key or memory silently splits
    into two half-profiles.
    """
    if not raw:
        return ""
    raw = raw.split("@", 1)[0]            # strip WhatsApp JID suffix
    digits = re.sub(r"\D", "", raw)       # drop +, spaces, dashes, parens
    return f"+{digits}" if digits else ""


_TIME_OF_DAY_RULES = (
    ("morning", (5, 11)),
    ("afternoon", (12, 16)),
    ("evening", (17, 23)),
)


def derive_time_of_day(preferred_time: str | None) -> str | None:
    """
    Collapse a concrete time ("9:30 AM", "17:00", "half past four") into a
    coarse bucket.

    We store the BUCKET rather than the exact time on purpose. "9:30am on the
    3rd" is a fact about one booking; "this customer books in the morning" is a
    fact about the customer. Only the second one is useful next time, and only
    the second one stays true as dates move.
    """
    if not preferred_time:
        return None
    text = preferred_time.strip().lower()

    for label, _ in _TIME_OF_DAY_RULES:
        if label in text:
            return label
    if "noon" in text or "midday" in text:
        return "afternoon"
    if "night" in text:
        return "evening"

    match = re.search(r"(\d{1,2})\s*[:.]?\s*(\d{2})?\s*(am|pm)?", text)
    if not match:
        return None
    hour = int(match.group(1))
    meridiem = match.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23:
        return None
    for label, (lo, hi) in _TIME_OF_DAY_RULES:
        if lo <= hour <= hi:
            return label
    return None


# ---------------------------------------------------------------------------
# DECISION 2 — HOW TO RETRIEVE: pre-load once, at session start.
# ---------------------------------------------------------------------------
# A booking conversation is short (typically 4-8 turns) and the entire profile
# is well under 100 tokens. Running a semantic search on every turn would add a
# retrieval round-trip to every single reply — on the VOICE channel that lands
# directly in the caller's perceived pause, where Uplift's whole budget is
# ~1 second end to end. Pre-loading pays the lookup once (one indexed SQLite
# read, sub-millisecond) and then costs nothing per turn.
#
# Per-turn retrieval only earns its latency when the corpus is too large to fit
# in context. Ours is one row.


def load_profile(phone: str, db_path: str | None = None) -> dict[str, Any] | None:
    """Pre-load read. Called ONCE at conversation start, on both channels."""
    key = normalize_phone(phone)
    if not key:
        return None
    return store.get_customer(key, db_path=db_path)


def format_profile_for_prompt(
    profile: dict[str, Any] | None, stated_name: str | None = None
) -> str:
    """
    Render the profile as the block that gets injected into the agent's system
    prompt. Returns guidance for a NEW customer when there is no profile.

    Note this returns instructions, not just data — the model needs to know
    that remembered facts are *proposals to confirm*, never assumptions. A
    customer who bought an SUV last year may be driving a sedan today.

    `stated_name` is the name the person has given in THIS conversation, when
    they have given one. It outranks the stored name — see the note below the
    profile block.
    """
    if not profile:
        return (
            "RETURNING CUSTOMER: No. This phone number has never booked before.\n"
            "Greet them as a new customer and collect every field from scratch."
        )

    lines = ["RETURNING CUSTOMER: Yes. Known profile for this phone number:"]
    label_map = {
        "customer_name": "Name",
        "vehicle_type": "Vehicle",
        "preferred_time_of_day": "Usually books",
        "usual_service": "Usual service",
        "last_booking_date": "Last booking",
        "booking_count": "Bookings so far",
    }
    for field, label in label_map.items():
        value = profile.get(field)
        if value not in (None, "", 0):
            lines.append(f"  - {label}: {value}")

    lines.append(
        "\nUse this to greet them by name and OFFER their previous choices as a "
        "one-line suggestion they can accept in a single word "
        '(e.g. "Welcome back, Ali - same Premium Wash for the SUV?"). '
        "Treat every remembered value as a suggestion to confirm, never as a "
        "fact you already have: if they correct you, use the new value. "
        "You still need the date and time for THIS booking - those are never "
        "carried over."
    )

    # WHO YOU ARE TALKING TO BEATS WHO THE ROW SAYS. This profile is keyed on a
    # phone number, and numbers are shared and reassigned all the time — family
    # handsets, office lines, a resold SIM. Every other remembered field belongs
    # to the number and is safe to offer; the NAME belongs to a person, so the
    # one they just gave us is the current truth and the stored one is stale.
    remembered_name = profile.get("customer_name")
    if stated_name and remembered_name and stated_name.lower() != remembered_name.lower():
        lines.append(
            f"\nNAME CONFLICT: this number is on file under {remembered_name}, but "
            f"the person you are speaking to has introduced themselves as "
            f"{stated_name}. Believe THEM. Call them {stated_name} and save the "
            f"booking under {stated_name}. Do not say \"welcome back\", do not "
            f"mention {remembered_name}, and do not ask them to account for the "
            "difference — a customer being corrected about their own name is the "
            "worst possible way to open. The remembered vehicle and usual service "
            "still belong to this number, so you may still offer those as a "
            "suggestion to confirm."
        )
    elif stated_name:
        lines.append(
            f"\nThey have given their name as {stated_name} in this conversation. "
            "If that ever differs from the name above, theirs is the correct one."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DECISION 3 — WHERE PROCESSING HAPPENS: post-processing write.
# ---------------------------------------------------------------------------
# The profile is updated AFTER the booking completes, not incrementally during
# the conversation. Two reasons:
#
#   (a) Correctness. Mid-conversation values are provisional — a customer who
#       says "SUV... actually no, the sedan today" would otherwise leave a
#       wrong fact in permanent memory. Writing from the FINAL confirmed
#       booking means memory only ever records things the customer agreed to.
#   (b) Latency. Nothing in a booking session needs to read back what was
#       written earlier in that same session (the conversation history already
#       holds it). So the write can happen entirely off the response path,
#       after the caller has hung up or the WhatsApp reply has been sent.
#
# In short: sessions need CROSS-session memory, not mid-session recall.


def update_profile_from_booking(
    booking: store.Booking, db_path: str | None = None
) -> dict[str, Any]:
    """
    Upsert the customer profile from a COMPLETED booking.

    New customers get a fresh profile with booking_count = 1. Returning
    customers get their counter incremented and any newly-learned or changed
    field overwritten (store.upsert_customer COALESCEs, so a field this booking
    didn't mention is preserved rather than nulled out).
    """
    key = normalize_phone(booking.phone)
    existing = store.get_customer(key, db_path=db_path)

    profile = {
        "phone": key,
        "customer_name": booking.customer_name,
        "vehicle_type": booking.vehicle_type,
        # Derived, not raw: store the habit, not this booking's clock time.
        "preferred_time_of_day": derive_time_of_day(booking.preferred_time),
        "usual_service": booking.service_type,
        # The date the booking is FOR, which is what "when were you last in"
        # means to a customer. Falls back to today if the agent captured a
        # relative date we couldn't normalize.
        "last_booking_date": booking.preferred_date or date.today().isoformat(),
        "booking_count": (existing or {}).get("booking_count", 0) + 1,
    }
    store.upsert_customer(profile, db_path=db_path)
    return profile
