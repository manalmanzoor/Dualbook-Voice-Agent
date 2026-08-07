#!/usr/bin/env python
"""
Business-owner view: print every booking and every customer profile, and
optionally export the bookings to CSV.

    python view_bookings.py
    python view_bookings.py --csv
    python view_bookings.py --csv --csv-path exports/bookings.csv
"""

from __future__ import annotations

import argparse

from dualbook import store


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {
        col: max(len(col), *(len(str(r.get(col) or "-")) for r in rows))
        for col in columns
    }
    header = "  " + "  ".join(col.upper().ljust(widths[col]) for col in columns)
    rule = "  " + "  ".join("-" * widths[col] for col in columns)
    body = [
        "  " + "  ".join(str(r.get(col) or "-").ljust(widths[col]) for col in columns)
        for r in rows
    ]
    return "\n".join([header, rule, *body])


def main() -> int:
    parser = argparse.ArgumentParser(description="View collected DualBook bookings")
    parser.add_argument("--csv", action="store_true", help="also export bookings to CSV")
    parser.add_argument("--csv-path", default=None, help="override the CSV output path")
    args = parser.parse_args()

    store.init_db()

    bookings = store.list_bookings()
    customers = store.list_customers()

    print(f"\n=== BOOKINGS ({len(bookings)}) ===")
    print(
        _table(
            bookings,
            [
                "id",
                "created_at",
                "channel",
                "customer_name",
                "phone",
                "vehicle_type",
                "service_type",
                "preferred_date",
                "preferred_time",
            ],
        )
    )

    print(f"\n=== CUSTOMER MEMORY PROFILES ({len(customers)}) ===")
    print(
        _table(
            customers,
            [
                "phone",
                "customer_name",
                "vehicle_type",
                "preferred_time_of_day",
                "usual_service",
                "last_booking_date",
                "booking_count",
            ],
        )
    )

    if args.csv:
        path = store.export_csv(args.csv_path)
        print(f"\nExported {len(bookings)} bookings to {path}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
