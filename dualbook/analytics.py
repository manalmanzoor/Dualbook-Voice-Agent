"""
Read-only aggregates for the dashboard.

Every number here is computed from the `bookings` / `customers` tables — there
are no hardcoded or sample figures. If a metric can't be derived from data the
system actually collects, it isn't shown (see the note on satisfaction below).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import store


def _parse_created(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pct_change(current: int, previous: int, min_baseline: int = 3) -> float | None:
    """
    Percentage change, or None when the baseline is too small to be meaningful.

    Going from 1 booking to 17 is "+1600%", which is technically correct and
    completely useless — it says more about the tiny denominator than about the
    business. Below `min_baseline` we return None and the card renders a neutral
    dash instead of a number that would mislead at a glance.
    """
    if previous < min_baseline:
        return None
    return round(((current - previous) / previous) * 100, 1)


def stats(db_path: str | None = None) -> dict[str, Any]:
    """The four headline cards."""
    bookings = store.list_bookings(db_path)
    customers = store.list_customers(db_path)
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()

    created = [(_parse_created(b.get("created_at")), b) for b in bookings]

    def booked_on(day: str) -> int:
        # Cancelled bookings are excluded: "Today's Bookings" is the number of
        # cars the owner expects to see, and counting one they cancelled makes
        # the card disagree with the rota printed right below it.
        return sum(
            1
            for b in bookings
            if (b.get("preferred_date") or "") == day and b.get("status") != "cancelled"
        )

    def created_within(start: datetime, end: datetime) -> int:
        return sum(1 for dt, _ in created if dt and start <= dt < end)

    last7_start = now - timedelta(days=7)
    prev7_start = now - timedelta(days=14)
    last7 = created_within(last7_start, now)
    prev7 = created_within(prev7_start, last7_start)

    returning = [c for c in customers if (c.get("booking_count") or 0) > 1]
    repeat_bookings = sum(
        max((c.get("booking_count") or 0) - 1, 0) for c in customers
    )
    total_bookings = len(bookings)

    # NOTE: there is no "satisfaction rate" card. We never ask customers to rate
    # anything, so any number there would be invented. Memory Hit Rate replaces
    # it — the share of bookings made by someone we already knew, which is the
    # metric this project actually exists to move.
    memory_hit_rate = (
        round((repeat_bookings / total_bookings) * 100) if total_bookings else 0
    )

    return {
        "total_bookings": {
            "value": total_bookings,
            "change": _pct_change(last7, prev7),
            "caption": "vs last 7 days",
        },
        "todays_bookings": {
            "value": booked_on(today),
            "change": _pct_change(booked_on(today), booked_on(yesterday)),
            "caption": "vs yesterday",
        },
        "returning_customers": {
            "value": len(returning),
            "change": None,
            "caption": f"of {len(customers)} total customers",
        },
        "memory_hit_rate": {
            "value": memory_hit_rate,
            "change": None,
            "caption": "bookings from known customers",
        },
    }


def booking_trend(days: int = 7, db_path: str | None = None) -> list[dict[str, Any]]:
    """Bookings created per day for the last N days, oldest first."""
    bookings = store.list_bookings(db_path)
    counts: dict[str, int] = {}
    for booking in bookings:
        dt = _parse_created(booking.get("created_at"))
        if dt:
            counts[dt.date().isoformat()] = counts.get(dt.date().isoformat(), 0) + 1

    today = datetime.now(timezone.utc).date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        series.append(
            {
                "date": key,
                "label": day.strftime("%d %b"),
                "count": counts.get(key, 0),
            }
        )
    return series


def memory_insights(db_path: str | None = None) -> dict[str, Any]:
    """
    "Customer Memory Insights" — aggregates over the memory profiles.

    This panel exists to make the memory layer visible: everything in it comes
    from the `customers` table, i.e. from facts the post-processing write chose
    to keep.
    """
    customers = store.list_customers(db_path)
    returning = [c for c in customers if (c.get("booking_count") or 0) > 1]

    def top(field: str) -> tuple[str | None, int]:
        tally: dict[str, int] = {}
        for customer in customers:
            value = customer.get(field)
            if value:
                tally[value] = tally.get(value, 0) + 1
        if not tally:
            return None, 0
        name, count = max(tally.items(), key=lambda kv: kv[1])
        share = round((count / len(customers)) * 100) if customers else 0
        return name, share

    service, service_share = top("usual_service")
    time_of_day, time_share = top("preferred_time_of_day")
    vehicle, vehicle_share = top("vehicle_type")

    time_windows = {
        "morning": "5 AM - 12 PM",
        "afternoon": "12 PM - 5 PM",
        "evening": "5 PM - 11 PM",
    }

    return {
        "returning_count": len(returning),
        "total_customers": len(customers),
        "top_service": service,
        "top_service_share": service_share,
        "top_time_of_day": time_of_day,
        "top_time_window": time_windows.get(time_of_day or "", ""),
        "top_time_share": time_share,
        "top_vehicle": vehicle,
        "top_vehicle_share": vehicle_share,
    }


def channel_split(db_path: str | None = None) -> dict[str, int]:
    counts = {"whatsapp": 0, "voice": 0}
    for booking in store.list_bookings(db_path):
        channel = booking.get("channel")
        if channel in counts:
            counts[channel] += 1
    return counts


# --- Impact: what the memory layer is actually worth ---------------------------
# Everything above answers "how is the business doing". The rest of this file
# answers the question a client asks after the demo — "so what does this
# actually change?" — and answers it with the system's own data rather than a
# claim on a slide.


def memory_impact(db_path: str | None = None) -> dict[str, Any]:
    """
    Turns-to-book for customers we remembered vs customers we didn't.

    Every booking records how many customer turns it took (`bookings.turns`).
    A booking is "remembered" if the customer already had a profile when it was
    made — which we can reconstruct exactly, because bookings are an append-only
    log: the Nth booking from a phone number had N-1 prior ones.

    This is the single most useful number in the product. Fewer turns is less of
    the customer's time, fewer chances to abandon, and less model spend per
    booking — all from the same conversation, just better informed.
    """
    bookings = sorted(store.list_bookings(db_path), key=lambda b: b["id"])
    seen: dict[str, int] = {}
    first_time: list[int] = []
    returning: list[int] = []

    for booking in bookings:
        phone = booking.get("phone") or ""
        prior = seen.get(phone, 0)
        seen[phone] = prior + 1
        turns = booking.get("turns")
        if not turns:
            continue  # older rows predate turn tracking; excluded, not guessed
        (returning if prior else first_time).append(int(turns))

    def average(values: list[int]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    new_avg, ret_avg = average(first_time), average(returning)
    saved = (
        round(new_avg - ret_avg, 1) if new_avg is not None and ret_avg is not None else None
    )

    return {
        "new_customer_turns": new_avg,
        "returning_customer_turns": ret_avg,
        "turns_saved": saved,
        "turns_saved_pct": (
            round((saved / new_avg) * 100) if saved is not None and new_avg else None
        ),
        "sample_new": len(first_time),
        "sample_returning": len(returning),
        # Bookings that predate turn tracking. Stated, not hidden — a metric
        # with an unexplained sample size is a metric nobody trusts.
        "untracked": len(bookings) - len(first_time) - len(returning),
    }


def service_mix(db_path: str | None = None) -> list[dict[str, Any]]:
    """Bookings and revenue per service, priced from the current menu."""
    from . import settings_store

    prices = {
        s["name"]: s.get("price") or 0 for s in settings_store.get().get("services", [])
    }
    tally: dict[str, int] = {}
    for booking in store.list_bookings(db_path):
        if booking.get("status") == "cancelled":
            continue
        name = booking.get("service_type") or "Not specified"
        tally[name] = tally.get(name, 0) + 1

    return sorted(
        (
            {
                "service": name,
                "count": count,
                # Revenue is only claimed for services still on the menu at a
                # known price; anything else reports None rather than zero,
                # which would read as "earned nothing".
                "revenue": prices[name] * count if name in prices else None,
            }
            for name, count in tally.items()
        ),
        key=lambda row: row["count"],
        reverse=True,
    )


def time_of_day_split(db_path: str | None = None) -> list[dict[str, Any]]:
    """When customers actually want their car washed — the staffing question."""
    from . import memory

    buckets = {"morning": 0, "afternoon": 0, "evening": 0, "unknown": 0}
    for booking in store.list_bookings(db_path):
        bucket = memory.derive_time_of_day(booking.get("preferred_time")) or "unknown"
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return [{"bucket": k, "count": v} for k, v in buckets.items() if k != "unknown" or v]


def status_split(db_path: str | None = None) -> dict[str, int]:
    counts = {status: 0 for status in store.BOOKING_STATUSES}
    for booking in store.list_bookings(db_path):
        status = booking.get("status") or "confirmed"
        counts[status] = counts.get(status, 0) + 1
    return counts


def revenue_summary(db_path: str | None = None) -> dict[str, Any]:
    """Booked value, from the menu prices — cancelled bookings excluded."""
    from . import settings_store

    settings = settings_store.get()
    prices = {s["name"]: s.get("price") or 0 for s in settings.get("services", [])}
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total = month = priced = 0
    for booking in store.list_bookings(db_path):
        if booking.get("status") == "cancelled":
            continue
        price = prices.get(booking.get("service_type") or "")
        if not price:
            continue
        priced += 1
        total += price
        created = _parse_created(booking.get("created_at"))
        if created and created >= month_start:
            month += price

    return {
        "currency": settings.get("currency", ""),
        "total": total,
        "this_month": month,
        "priced_bookings": priced,
        "average": round(total / priced) if priced else 0,
    }


def daily_series(
    days: int = 30, db_path: str | None = None
) -> list[dict[str, Any]]:
    """
    Bookings per day, split by channel, for the analytics chart.

    Same shape as booking_trend but with the channel breakdown the Analytics
    page needs, so the two views can't disagree about a day's total.
    """
    bookings = store.list_bookings(db_path)
    today = datetime.now(timezone.utc).date()
    series = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        rows = [
            b
            for b in bookings
            if (dt := _parse_created(b.get("created_at"))) and dt.date().isoformat() == day
        ]
        series.append(
            {
                "date": day,
                "label": (today - timedelta(days=offset)).strftime("%d %b"),
                "count": len(rows),
                "whatsapp": sum(1 for b in rows if b.get("channel") == "whatsapp"),
                "voice": sum(1 for b in rows if b.get("channel") == "voice"),
            }
        )
    return series


def kpi_series(days: int = 14, db_path: str | None = None) -> list[dict[str, Any]]:
    """
    Per-day bookings, completions and booked value — the sparklines on the KPI
    cards.

    Same source as every other figure, so a card's trend line can never tell a
    different story from the number printed above it.
    """
    from . import settings_store

    prices = {
        s["name"]: s.get("price") or 0 for s in settings_store.get().get("services", [])
    }
    bookings = store.list_bookings(db_path)
    today = datetime.now(timezone.utc).date()

    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        rows = [
            b
            for b in bookings
            if (dt := _parse_created(b.get("created_at"))) and dt.date() == day
        ]
        series.append(
            {
                "date": key,
                "label": day.strftime("%d %b"),
                "bookings": len(rows),
                "completed": sum(1 for b in rows if b.get("status") == "completed"),
                "revenue": sum(
                    prices.get(b.get("service_type") or "", 0)
                    for b in rows
                    if b.get("status") != "cancelled"
                ),
            }
        )
    return series


def customer_table(db_path: str | None = None) -> list[dict[str, Any]]:
    """
    The Customers page: one row per person, not per booking.

    Deliberately different from the bookings table — this is the profile the
    memory layer maintains, plus what it has been worth. A customer is a
    relationship with a history; a booking is one event in it.
    """
    from . import settings_store

    prices = {
        s["name"]: s.get("price") or 0 for s in settings_store.get().get("services", [])
    }
    by_phone: dict[str, list[dict[str, Any]]] = {}
    for booking in store.list_bookings(db_path):
        by_phone.setdefault(booking.get("phone") or "", []).append(booking)

    rows = []
    for profile in store.list_customers(db_path):
        history = by_phone.get(profile["phone"], [])
        spend = sum(
            prices.get(b.get("service_type") or "", 0)
            for b in history
            if b.get("status") != "cancelled"
        )
        channels = sorted({b.get("channel") for b in history if b.get("channel")})
        rows.append(
            {
                **profile,
                "bookings": len(history),
                "lifetime_value": spend,
                "channels": channels,
                "first_seen": min((b.get("created_at") or "" for b in history), default=None),
                "last_seen": max((b.get("created_at") or "" for b in history), default=None),
                "next_booking": min(
                    (
                        b.get("preferred_date")
                        for b in history
                        if (b.get("preferred_date") or "") >= date.today().isoformat()
                        and b.get("status") != "cancelled"
                    ),
                    default=None,
                ),
            }
        )
    rows.sort(key=lambda r: (r["bookings"], r["lifetime_value"]), reverse=True)
    return rows
