"""
Business settings — the owner's half of the configuration.

WHY THIS EXISTS SEPARATELY FROM config.py
-----------------------------------------
`config.py` holds DEVELOPER configuration: API keys, provider choice, database
path. Changing it means editing `.env` and restarting a process.

This module holds BUSINESS configuration: the trading name, the service menu
with prices, opening hours, how far ahead bookings are taken, what the
confirmation message says. A car wash owner must be able to change their
Saturday hours or put the price of a Full Detail up without a developer, and the
agent has to honour the change on the very next conversation — not the next
deploy. So it lives in SQLite, is edited from the Settings page, and is read
fresh when a conversation starts.

Two things then follow automatically, and they are what make this more than a
settings screen:

  1. The agent QUOTES the current menu, because the prices are in its prompt.
  2. The agent REFUSES a slot outside opening hours, because `validate.py`
     checks the booking against these hours before it is allowed to save.
"""

from __future__ import annotations

import copy
from typing import Any

from . import config, store

# Monday-first, because that is how a rota is read.
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}

# Shipped defaults. A fresh database behaves exactly like the demo in the README
# rather than starting empty and making the agent sound broken.
DEFAULTS: dict[str, Any] = {
    "business_name": config.BUSINESS_NAME,
    "phone": "+92 300 000 0000",
    "address": "Main Boulevard, Gulberg, Lahore",
    "currency": "PKR",
    "services": [
        {"name": "Express Wash", "price": 1200, "minutes": 20,
         "description": "Exterior wash, dry and tyre shine."},
        {"name": "Premium Wash", "price": 2500, "minutes": 45,
         "description": "Exterior wash, wax, interior vacuum and dashboard."},
        {"name": "Interior Detail", "price": 4500, "minutes": 90,
         "description": "Deep clean of seats, carpets and trim."},
        {"name": "Full Detail", "price": 8000, "minutes": 180,
         "description": "Interior detail plus polish, sealant and engine bay."},
    ],
    "hours": {
        "mon": {"open": "09:00", "close": "19:00", "closed": False},
        "tue": {"open": "09:00", "close": "19:00", "closed": False},
        "wed": {"open": "09:00", "close": "19:00", "closed": False},
        "thu": {"open": "09:00", "close": "19:00", "closed": False},
        "fri": {"open": "09:00", "close": "13:00", "closed": False},
        "sat": {"open": "09:00", "close": "19:00", "closed": False},
        "sun": {"open": "10:00", "close": "16:00", "closed": True},
    },
    "max_days_ahead": 30,
    "default_country_code": "+92",
    # Outbound confirmation. Placeholders are filled in notify.py.
    "send_confirmation": True,
    "confirmation_template": (
        "Hi {name}! Your {service} at {business} is confirmed for {date} at "
        "{time} for your {vehicle}. Booking #{id}. Reply here if you need to "
        "change anything — see you soon!"
    ),
    # Appended to the agent's system prompt. The owner's voice, in one line.
    "agent_persona": (
        "Warm, brief and local. Never pushy about upsells — mention a better "
        "service once, then respect the answer."
    ),
}


def get() -> dict[str, Any]:
    """Current settings: shipped defaults with any saved overrides on top."""
    merged = copy.deepcopy(DEFAULTS)
    merged.update(store.get_settings())
    # A partial `hours` override must not lose the days it didn't mention.
    hours = copy.deepcopy(DEFAULTS["hours"])
    hours.update(merged.get("hours") or {})
    merged["hours"] = hours
    return merged


def save(values: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist. Returns the settings as they now stand."""
    clean = validate(values)
    if clean:
        store.put_settings(clean)
    return get()


class SettingsError(ValueError):
    """Raised with a message meant for the person typing into the form."""


def _valid_hhmm(value: str) -> bool:
    parts = str(value).split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return False
    hour, minute = int(parts[0]), int(parts[1])
    return 0 <= hour <= 23 and 0 <= minute <= 59


def validate(values: dict[str, Any]) -> dict[str, Any]:
    """
    Keep only known keys, and reject values that would break the agent.

    This runs before anything is written, because a malformed opening-hours
    entry would not fail here — it would fail three days later, silently, as a
    booking the agent should have refused.
    """
    clean: dict[str, Any] = {}

    for key in ("business_name", "phone", "address", "currency",
                "agent_persona", "confirmation_template", "default_country_code"):
        if key in values:
            text = str(values[key] or "").strip()
            if key == "business_name" and not text:
                raise SettingsError("Business name cannot be empty.")
            clean[key] = text

    if "confirmation_template" in clean and "{" in clean["confirmation_template"]:
        allowed = {"name", "business", "service", "date", "time", "vehicle", "id"}
        import re

        unknown = set(re.findall(r"{(\w+)}", clean["confirmation_template"])) - allowed
        if unknown:
            raise SettingsError(
                f"Unknown placeholder(s) in the confirmation message: "
                f"{', '.join('{'+u+'}' for u in sorted(unknown))}. "
                f"Available: {', '.join('{'+a+'}' for a in sorted(allowed))}."
            )

    if "services" in values:
        services = []
        for raw in values["services"] or []:
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            try:
                price = int(float(raw.get("price") or 0))
                minutes = int(float(raw.get("minutes") or 0))
            except (TypeError, ValueError):
                raise SettingsError(f"Price and duration for {name!r} must be numbers.")
            if price < 0 or minutes < 0:
                raise SettingsError(f"Price and duration for {name!r} cannot be negative.")
            services.append({
                "name": name, "price": price, "minutes": minutes,
                "description": str(raw.get("description") or "").strip(),
            })
        if not services:
            raise SettingsError("Keep at least one service on the menu.")
        clean["services"] = services

    if "hours" in values:
        hours = copy.deepcopy(get()["hours"])
        for day, spec in (values["hours"] or {}).items():
            if day not in DAYS:
                continue
            opens = str(spec.get("open") or "09:00")
            closes = str(spec.get("close") or "18:00")
            if not (_valid_hhmm(opens) and _valid_hhmm(closes)):
                raise SettingsError(
                    f"{DAY_LABELS[day]}: times must look like 09:00 or 17:30."
                )
            if not spec.get("closed") and opens >= closes:
                raise SettingsError(
                    f"{DAY_LABELS[day]}: closing time must be after opening time."
                )
            hours[day] = {"open": opens, "close": closes, "closed": bool(spec.get("closed"))}
        clean["hours"] = hours

    if "max_days_ahead" in values:
        try:
            days = int(values["max_days_ahead"])
        except (TypeError, ValueError):
            raise SettingsError("Booking window must be a whole number of days.")
        if not 1 <= days <= 365:
            raise SettingsError("Booking window must be between 1 and 365 days.")
        clean["max_days_ahead"] = days

    if "send_confirmation" in values:
        clean["send_confirmation"] = bool(values["send_confirmation"])

    return clean


# --- Views the agent needs ----------------------------------------------------


def service_names(settings: dict[str, Any] | None = None) -> list[str]:
    settings = settings or get()
    return [s["name"] for s in settings.get("services", []) if s.get("name")]


def menu_for_prompt(settings: dict[str, Any] | None = None) -> str:
    """The service menu as the agent should be able to quote it."""
    settings = settings or get()
    currency = settings.get("currency") or ""
    lines = []
    for service in settings.get("services", []):
        bits = [f"  - {service['name']}"]
        if service.get("price"):
            bits.append(f"{currency} {service['price']:,}")
        if service.get("minutes"):
            bits.append(f"~{service['minutes']} min")
        line = " — ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0]
        if service.get("description"):
            line += f". {service['description']}"
        lines.append(line)
    return "\n".join(lines)


def hours_for_prompt(settings: dict[str, Any] | None = None) -> str:
    settings = settings or get()
    hours = settings["hours"]
    return "\n".join(
        f"  - {DAY_LABELS[day]}: "
        + ("CLOSED" if hours[day].get("closed") else f"{hours[day]['open']}–{hours[day]['close']}")
        for day in DAYS
    )
