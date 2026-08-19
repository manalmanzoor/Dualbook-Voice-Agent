"""
Shared booking store — SQLite, two tables.

    bookings   append-only log of every completed booking (both channels)
    customers  the memory profile, keyed by phone (see memory.py)

Both agents write through this module. Nothing else in the codebase opens a
sqlite connection, so the schema has exactly one owner.

Why SQLite rather than a CSV or a Sheet as the source of truth: the memory layer
needs a *keyed upsert* on every completed booking ("has this phone booked
before?"). That is a primary-key lookup, which a row-append format can't do
without a full scan. CSV export is provided separately for the business owner.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    phone             TEXT    NOT NULL,
    customer_name     TEXT    NOT NULL,
    vehicle_type      TEXT    NOT NULL,
    preferred_date    TEXT    NOT NULL,
    preferred_time    TEXT    NOT NULL,
    service_type      TEXT,
    contact_details   TEXT,
    channel           TEXT    NOT NULL,   -- 'whatsapp' | 'voice'
    notes             TEXT,
    created_at        TEXT    NOT NULL,
    -- Operational state the owner actually manages from the dashboard.
    status            TEXT    NOT NULL DEFAULT 'confirmed',  -- confirmed|completed|cancelled
    -- How many customer turns it took to get this booking. This is the metric
    -- that proves the memory layer earns its keep: a returning customer should
    -- book in materially fewer turns than a new one, and now we can show it
    -- instead of asserting it. Written once, at completion.
    turns             INTEGER
);

CREATE INDEX IF NOT EXISTS idx_bookings_phone ON bookings(phone);

-- The customer memory profile. Deliberately a TYPED, NARROW schema: only the
-- fields that let a future conversation start further ahead. No transcripts,
-- no greetings, no free-text "notes about the customer". See MEMORY_DESIGN.md.
CREATE TABLE IF NOT EXISTS customers (
    phone                 TEXT PRIMARY KEY,
    customer_name         TEXT,
    vehicle_type          TEXT,
    preferred_time_of_day TEXT,   -- 'morning' | 'afternoon' | 'evening'
    usual_service         TEXT,
    last_booking_date     TEXT,
    booking_count         INTEGER NOT NULL DEFAULT 0,
    updated_at            TEXT    NOT NULL
);

-- Live conversation state, so the dashboard can show in-flight sessions from a
-- SEPARATE process (the agents hold the transcript in memory; only this
-- summary is shared). Rows are transient - deleted when a booking completes.
CREATE TABLE IF NOT EXISTS conversations (
    phone         TEXT PRIMARY KEY,
    channel       TEXT    NOT NULL,
    status        TEXT    NOT NULL,   -- 'active' | 'completed'
    last_message  TEXT,
    turn_count    INTEGER NOT NULL DEFAULT 0,
    slots_json    TEXT,               -- JSON list of slot names known so far
    is_returning  INTEGER NOT NULL DEFAULT 0,
    customer_name TEXT,
    started_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- Business configuration the OWNER controls from the Settings page: business
-- name, service menu, opening hours. It lives in the database rather than .env
-- because .env is a developer artefact — a car wash owner changing their
-- Saturday hours should not need a redeploy, and the agent must pick the change
-- up on the very next conversation. See settings_store.py.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,           -- JSON-encoded
    updated_at TEXT NOT NULL
);

-- Every customer-facing message the system decided to send, whether or not a
-- transport was configured to carry it. With WhatsApp credentials it records
-- what WAS sent; without them it records what WOULD be sent. Either way the
-- owner can see the confirmation their customer gets, and nothing is silently
-- dropped.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT    NOT NULL,
    channel    TEXT    NOT NULL,        -- 'whatsapp'
    kind       TEXT    NOT NULL,        -- 'booking_confirmation' | 'test'
    body       TEXT    NOT NULL,
    status     TEXT    NOT NULL,        -- sent | simulated | failed | rejected | blocked
    detail     TEXT,                    -- provider id, or why it failed
    booking_id INTEGER,
    created_at TEXT    NOT NULL
);

-- When each number last messaged US. This exists for exactly one reason: Meta
-- only permits free-form ("session") messages within 24 hours of the customer's
-- last inbound message. Outside that window a message must be a pre-approved
-- TEMPLATE, and sending the wrong shape is rejected by Meta at the API.
--
-- Deliberately NOT a column on `customers`: that table is the memory profile,
-- and memory.py is explicit that it holds only facts which shorten a future
-- booking. "When did this number last text us" is transport bookkeeping, not
-- something the agent should ever reason about. Separate concern, separate table.
CREATE TABLE IF NOT EXISTS wa_contacts (
    phone           TEXT PRIMARY KEY,
    last_inbound_at TEXT NOT NULL
);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", and an existing dualbook.db must keep its bookings, so migrate by
# inspecting the table rather than recreating it.
_ADDED_COLUMNS = {
    "bookings": {
        "status": "TEXT NOT NULL DEFAULT 'confirmed'",
        "turns": "INTEGER",
    },
    "outbox": {
        # Which WhatsApp message shape was used: 'session' (free text) or
        # 'template'. Recorded because the two fail for completely different
        # reasons, and "it failed" without knowing which was attempted sends
        # you looking in the wrong place.
        "mode": "TEXT",
    },
}


@dataclass
class Booking:
    """One completed booking. Mirrors the `bookings` table."""

    phone: str
    customer_name: str
    vehicle_type: str
    preferred_date: str
    preferred_time: str
    service_type: str | None = None
    contact_details: str | None = None
    channel: str = "whatsapp"
    notes: str | None = None
    created_at: str | None = None
    status: str = "confirmed"
    turns: int | None = None

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["created_at"] = self.created_at or _now()
        return row


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection with row-dict access and guaranteed close/commit."""
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't exist, and migrate an older file forward."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# --- Bookings -----------------------------------------------------------------


def save_booking(booking: Booking, db_path: str | None = None) -> int:
    """Persist a completed booking and return its row id."""
    row = booking.as_row()
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO bookings (
                phone, customer_name, vehicle_type, preferred_date,
                preferred_time, service_type, contact_details, channel,
                notes, created_at, status, turns
            ) VALUES (
                :phone, :customer_name, :vehicle_type, :preferred_date,
                :preferred_time, :service_type, :contact_details, :channel,
                :notes, :created_at, :status, :turns
            )
            """,
            row,
        )
        return int(cur.lastrowid)


def list_bookings(db_path: str | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM bookings ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_booking(booking_id: int, db_path: str | None = None) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    return dict(row) if row else None


# Sorting is done in SQL, so the column names are interpolated into the query —
# hence a strict allow-list. Never let a query string reach ORDER BY directly.
_SORTABLE = {
    "created_at": "created_at",
    "date": "preferred_date",
    "customer": "customer_name",
    "status": "status",
    "channel": "channel",
}


def query_bookings(
    search: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    service: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "created_at",
    direction: str = "desc",
    limit: int | None = None,
    offset: int = 0,
    db_path: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Filtered, sorted bookings + the total that matched before paging.

    Filtering happens in SQLite rather than in the browser because the owner's
    "show me next week's SUVs" is a question about the whole book, not about the
    25 rows the page happens to be holding.
    """
    where: list[str] = []
    params: list[Any] = []

    if search:
        like = f"%{search.strip()}%"
        where.append(
            "(customer_name LIKE ? OR phone LIKE ? OR vehicle_type LIKE ? "
            "OR service_type LIKE ? OR contact_details LIKE ?)"
        )
        params += [like] * 5
    for column, value in (
        ("channel", channel),
        ("status", status),
        ("service_type", service),
    ):
        if value and value != "all":
            where.append(f"{column} = ?")
            params.append(value)
    if date_from:
        where.append("preferred_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("preferred_date <= ?")
        params.append(date_to)

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    order = _SORTABLE.get(sort, "created_at")
    arrow = "ASC" if str(direction).lower() == "asc" else "DESC"

    with connect(db_path) as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM bookings{clause}", params).fetchone()[0]
        )
        sql = f"SELECT * FROM bookings{clause} ORDER BY {order} {arrow}, id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = params + [limit, offset]
        rows = conn.execute(sql, params).fetchall()

    return [dict(r) for r in rows], total


BOOKING_STATUSES = ("confirmed", "completed", "cancelled")


def set_booking_status(
    booking_id: int, status: str, db_path: str | None = None
) -> dict[str, Any] | None:
    """Owner-side state change from the dashboard (confirm / complete / cancel)."""
    if status not in BOOKING_STATUSES:
        raise ValueError(f"Unknown status {status!r}")
    with connect(db_path) as conn:
        conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    return get_booking(booking_id, db_path=db_path)


def bookings_for_phone(phone: str, db_path: str | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE phone = ? ORDER BY id DESC", (phone,)
        ).fetchall()
    return [dict(r) for r in rows]


def print_booking(booking: Booking, booking_id: int | None = None) -> None:
    """Console output for the business owner watching the process run."""
    header = f" BOOKING CONFIRMED{f' #{booking_id}' if booking_id else ''} "
    print("\n" + header.center(56, "="))
    fields = [
        ("Customer", booking.customer_name),
        ("Phone", booking.phone),
        ("Vehicle", booking.vehicle_type),
        ("Date", booking.preferred_date),
        ("Time", booking.preferred_time),
        ("Service", booking.service_type or "-"),
        ("Contact", booking.contact_details or booking.phone),
        ("Channel", booking.channel),
    ]
    for label, value in fields:
        print(f"  {label:<10} {value}")
    print("=" * 56 + "\n")


# --- Customer profiles (raw table access; policy lives in memory.py) ----------


def get_customer(phone: str, db_path: str | None = None) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE phone = ?", (phone,)
        ).fetchone()
    return dict(row) if row else None


def upsert_customer(profile: dict[str, Any], db_path: str | None = None) -> None:
    """
    Insert or update a profile keyed on phone.

    COALESCE on update means a newer conversation that *didn't* mention (say)
    the service type will not erase what we already knew. Memory should only
    ever get richer unless the customer explicitly changes something.
    """
    profile = {**profile, "updated_at": _now()}
    profile.setdefault("booking_count", 0)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO customers (
                phone, customer_name, vehicle_type, preferred_time_of_day,
                usual_service, last_booking_date, booking_count, updated_at
            ) VALUES (
                :phone, :customer_name, :vehicle_type, :preferred_time_of_day,
                :usual_service, :last_booking_date, :booking_count, :updated_at
            )
            ON CONFLICT(phone) DO UPDATE SET
                customer_name         = COALESCE(excluded.customer_name, customers.customer_name),
                vehicle_type          = COALESCE(excluded.vehicle_type, customers.vehicle_type),
                preferred_time_of_day = COALESCE(excluded.preferred_time_of_day, customers.preferred_time_of_day),
                usual_service         = COALESCE(excluded.usual_service, customers.usual_service),
                last_booking_date     = COALESCE(excluded.last_booking_date, customers.last_booking_date),
                booking_count         = excluded.booking_count,
                updated_at            = excluded.updated_at
            """,
            {
                "phone": profile["phone"],
                "customer_name": profile.get("customer_name"),
                "vehicle_type": profile.get("vehicle_type"),
                "preferred_time_of_day": profile.get("preferred_time_of_day"),
                "usual_service": profile.get("usual_service"),
                "last_booking_date": profile.get("last_booking_date"),
                "booking_count": profile.get("booking_count", 0),
                "updated_at": profile["updated_at"],
            },
        )


def list_customers(db_path: str | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM customers ORDER BY booking_count DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# --- Live conversations (dashboard read model) --------------------------------


def upsert_conversation(
    phone: str,
    channel: str,
    status: str,
    last_message: str | None,
    turn_count: int,
    slots: list[str],
    is_returning: bool,
    customer_name: str | None,
    db_path: str | None = None,
) -> None:
    """Write the shareable summary of an in-flight conversation."""
    now = _now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO conversations (
                phone, channel, status, last_message, turn_count, slots_json,
                is_returning, customer_name, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                channel       = excluded.channel,
                status        = excluded.status,
                last_message  = excluded.last_message,
                turn_count    = excluded.turn_count,
                slots_json    = excluded.slots_json,
                is_returning  = excluded.is_returning,
                customer_name = COALESCE(excluded.customer_name, conversations.customer_name),
                updated_at    = excluded.updated_at
            """,
            (
                phone,
                channel,
                status,
                (last_message or "")[:280],
                turn_count,
                json.dumps(sorted(slots)),
                1 if is_returning else 0,
                customer_name,
                now,
                now,
            ),
        )


def list_conversations(
    limit: int = 20, db_path: str | None = None
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["slots"] = json.loads(item.pop("slots_json") or "[]")
        except json.JSONDecodeError:
            item["slots"] = []
        item["is_returning"] = bool(item["is_returning"])
        out.append(item)
    return out


def clear_conversation(phone: str, db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM conversations WHERE phone = ?", (phone,))


def prune_conversations(older_than_minutes: int = 60, db_path: str | None = None) -> int:
    """
    Drop stale rows. A crashed agent would otherwise leave a conversation
    showing as 'active' forever on the dashboard.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    ).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,))
        return cur.rowcount


# --- Settings (owner-editable business config) --------------------------------


def get_settings(db_path: str | None = None) -> dict[str, Any]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            out[row["key"]] = row["value"]
    return out


def put_settings(values: dict[str, Any], db_path: str | None = None) -> None:
    now = _now()
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            [(k, json.dumps(v), now) for k, v in values.items()],
        )


# --- Outbox (what the customer was actually told) -----------------------------


def record_outbound(
    phone: str,
    body: str,
    status: str,
    kind: str = "booking_confirmation",
    channel: str = "whatsapp",
    detail: str | None = None,
    booking_id: int | None = None,
    mode: str | None = None,
    db_path: str | None = None,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO outbox (phone, channel, kind, body, status, detail,
                                booking_id, mode, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (phone, channel, kind, body, status, detail, booking_id, mode, _now()),
        )
        return int(cur.lastrowid)


# --- Inbound contact timestamps (the 24-hour session window) ------------------


def record_inbound(phone: str, db_path: str | None = None) -> None:
    """
    Note that this number just messaged us, opening a 24-hour session window.

    Called on every inbound WhatsApp message. Cheap enough to do unconditionally:
    one indexed upsert against a two-column table.
    """
    if not phone:
        return
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO wa_contacts (phone, last_inbound_at) VALUES (?, ?)
            ON CONFLICT(phone) DO UPDATE SET last_inbound_at = excluded.last_inbound_at
            """,
            (phone, _now()),
        )


def last_inbound_at(phone: str, db_path: str | None = None) -> datetime | None:
    """When this number last messaged us, or None if it never has."""
    if not phone:
        return None
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_inbound_at FROM wa_contacts WHERE phone = ?", (phone,)
        ).fetchone()
    if not row or not row["last_inbound_at"]:
        return None
    try:
        parsed = datetime.fromisoformat(row["last_inbound_at"])
    except ValueError:
        return None
    # Rows are written by _now() in UTC, but a naive value read back from an
    # older file would compare wrongly against an aware "now" — so normalise.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def list_outbound(limit: int = 25, db_path: str | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Export -------------------------------------------------------------------

CSV_COLUMNS = [
    "id",
    "created_at",
    "channel",
    "status",
    "customer_name",
    "phone",
    "contact_details",
    "vehicle_type",
    "service_type",
    "preferred_date",
    "preferred_time",
    "turns",
    "notes",
]


def export_csv(
    path: str | None = None,
    db_path: str | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> str:
    """
    Write bookings to CSV (what the business owner opens in Excel).

    `rows` lets the dashboard export exactly what the owner is looking at —
    exporting the whole book when they've filtered to "cancelled, this week" is
    a quiet way to hand someone the wrong spreadsheet.
    """
    out = Path(path or config.CSV_EXPORT_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = list_bookings(db_path) if rows is None else rows
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(out)
