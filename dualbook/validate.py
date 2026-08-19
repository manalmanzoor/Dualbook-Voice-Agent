"""
Validation — the checks that stand between a conversation and a real commitment.

Two things get validated here, and both exist because of the same failure: an
LLM will happily produce a confident, plausible, WRONG value, and by the time
anyone notices, a customer has been told something untrue.

    1. PHONE NUMBERS (`phone`)
       A WhatsApp confirmation is sent to a real handset. Sending it to a
       mistyped or fabricated number is worse than sending nothing: the message
       goes to a stranger, and the actual customer hears nothing and assumes
       they were never booked. So a number is checked for shape, length and
       plausibility BEFORE any transport is handed it.

    2. BOOKING SLOTS (`booking_slot`)
       "Sunday at 8pm" is a perfectly well-formed date and time that the
       business cannot honour. The agent is told the opening hours, but a prompt
       is guidance, not a guarantee — this is the check that actually holds, and
       it runs inside the save_booking tool, so the model's only way past it is
       to ask the customer for a time that works.

Both return a small result object rather than raising: the caller is usually the
tool handler, which needs to hand the REASON back to the model so it can fix the
problem in conversation rather than apologise for an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import settings_store

# E.164 allows at most 15 digits including the country code; ITU sets no formal
# minimum, but nothing dialable is shorter than 8 once a country code is on it.
MIN_DIGITS = 8
MAX_DIGITS = 15

# How many digits follow the country code, per country we actually serve.
#
# The generic 8-digit floor above is a backstop for countries we don't know, and
# it is far too loose for one we do: speech recognition heard "03124 567" and
# that passed as +923124567, nine digits, three short of any real Pakistani
# number. It would have reached the customer's booking as a contact nobody can
# call. Length is the only cheap check that catches a plausible-looking
# mis-transcription, so it is worth being specific where we can be.
#
# Pakistani mobiles are 03XX-XXXXXXX and landlines 0XX-XXXXXXXX; both are ten
# digits once the leading 0 is replaced by the country code.
_NSN_LENGTHS: dict[str, set[int]] = {
    "92": {10},        # Pakistan
    "1": {10},         # US / Canada
    "44": {10},        # UK
    "91": {10},        # India
    "971": {8, 9},     # UAE
    "966": {9},        # Saudi Arabia
    "880": {10},       # Bangladesh
}


@dataclass
class Result:
    ok: bool
    value: str | None = None
    reason: str | None = None
    warning: str | None = None

    def __bool__(self) -> bool:  # `if validate.phone(x):`
        return self.ok


# --- Phone numbers ------------------------------------------------------------


def phone(raw: str, default_country: str | None = None) -> Result:
    """
    Normalise a phone number to E.164, or explain why it can't be.

    Accepts what people actually type — `0300 123 4567`, `(+92) 300-123-4567`,
    `00923001234567`, a WhatsApp JID — and produces `+923001234567`.

    A local number starting `0` is only promoted to international form using the
    configured country code. Guessing a country for a bare 9-digit string would
    be inventing information, which is the exact failure this module exists to
    prevent, so that case is rejected instead.
    """
    text = str(raw or "").strip()
    if not text:
        return Result(False, reason="No phone number given.")

    text = text.split("@", 1)[0]                      # WhatsApp JID -> number
    if re.search(r"[a-zA-Z]", text):
        return Result(False, reason=f"{raw!r} is not a phone number.")

    country = (default_country or settings_store.get()["default_country_code"]).strip()
    country_digits = re.sub(r"\D", "", country)
    had_plus = text.lstrip().startswith("+")
    digits = re.sub(r"\D", "", text)

    if not digits:
        return Result(False, reason=f"{raw!r} contains no digits.")

    if digits.startswith("00"):                       # 0092... -> 92...
        digits = digits[2:]
    elif digits.startswith("0"):                      # local: 0300... -> 92300...
        if not country_digits:
            return Result(
                False,
                reason="Local numbers need a default country code set in Settings.",
            )
        digits = country_digits + digits.lstrip("0")
    elif not had_plus and country_digits and not digits.startswith(country_digits):
        # A bare national number with no leading 0 and no '+'. Only safe to
        # assume the local country when the length matches a national number.
        if len(digits) <= 10:
            digits = country_digits + digits

    if len(digits) < MIN_DIGITS:
        return Result(False, reason=f"{raw!r} is too short to be a phone number.")
    if len(digits) > MAX_DIGITS:
        return Result(False, reason=f"{raw!r} has too many digits to be dialable.")

    # Where we know the country, check the number is the right LENGTH for it.
    # Longest matching prefix first, so "971" wins over "97" if both were listed.
    for code in sorted(_NSN_LENGTHS, key=len, reverse=True):
        if digits.startswith(code):
            expected = _NSN_LENGTHS[code]
            actual = len(digits) - len(code)
            if actual not in expected:
                want = " or ".join(str(n) for n in sorted(expected))
                return Result(
                    False,
                    reason=(
                        f"{raw!r} is not a complete number — +{code} numbers have "
                        f"{want} digits after the country code, this has {actual}. "
                        "Ask them to repeat it digit by digit."
                    ),
                )
            break
    if len(set(digits)) <= 2:
        # 0000000000 / 1212121212 — passes every length check, reaches no one.
        return Result(False, reason=f"{raw!r} does not look like a real number.")

    result = Result(True, value=f"+{digits}")
    if country_digits and not digits.startswith(country_digits):
        result.warning = (
            f"+{digits} is outside your default country code (+{country_digits}) — "
            "international rates may apply."
        )
    return result


def same_number(a: str, b: str) -> bool:
    """Do two written forms denote the same number? (Compares E.164 forms.)"""
    first, second = phone(a), phone(b)
    return bool(first and second and first.value == second.value)


# --- Booking slots ------------------------------------------------------------

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_time(value: str) -> str | None:
    """'9:30 AM', '0930', '17:00', 'half past nine' -> '09:30' (or None)."""
    text = str(value or "").strip().lower()
    if not text:
        return None

    match = re.search(r"(\d{1,2})\s*[:.]?\s*(\d{2})?\s*(am|pm)?", text)
    if not match:
        # Word-only answers still carry a usable slot for a business that only
        # trades in whole hours; anything vaguer is rejected upstream by the
        # required-slot check, not silently invented here.
        for word, hhmm in (("noon", "12:00"), ("midday", "12:00"),
                           ("morning", "10:00"), ("afternoon", "14:00"),
                           ("evening", "17:00")):
            if word in text:
                return hhmm
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and hour < 8:
        # "at 5" for a business open 09:00-19:00 means 17:00, not 05:00.
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def booking_slot(
    preferred_date: str,
    preferred_time: str,
    settings: dict[str, Any] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> Result:
    """
    Is this date+time something the business can actually honour?

    Checks, in the order a person would: is it a real date, is it in the past,
    is it too far out, are we open that day, are we open at that time, and —
    if it is today — has that time already gone by.

    `now` is injectable so the past-time rule can be tested at a fixed instant.
    Without it a test asserting "09:00 today is rejected" would pass all evening
    and fail every morning, which is worse than no test at all.
    """
    settings = settings or settings_store.get()
    now = now or datetime.now()
    today = today or now.date()

    text = str(preferred_date or "").strip()
    try:
        when = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return Result(
            False,
            reason=f"{text!r} is not a usable date. Resolve it to an absolute "
                   "date (YYYY-MM-DD) before saving.",
        )

    if when < today:
        return Result(False, reason=f"{text} is in the past. Ask for a future date.")

    horizon = int(settings.get("max_days_ahead", 30))
    if when > today + timedelta(days=horizon):
        return Result(
            False,
            reason=f"We only take bookings up to {horizon} days ahead "
                   f"(until {(today + timedelta(days=horizon)).isoformat()}).",
        )

    day_key = _WEEKDAY_KEYS[when.weekday()]
    day = settings["hours"][day_key]
    label = settings_store.DAY_LABELS[day_key]

    if day.get("closed"):
        open_days = [
            settings_store.DAY_LABELS[d]
            for d in _WEEKDAY_KEYS
            if not settings["hours"][d].get("closed")
        ]
        return Result(
            False,
            reason=f"We are closed on {label}. We are open "
                   f"{', '.join(open_days)} — offer one of those instead.",
        )

    clock = parse_time(preferred_time)
    if not clock:
        return Result(
            False,
            reason=f"{preferred_time!r} is not a usable time. Ask for a clock "
                   "time such as '9:30 AM'.",
        )

    if not (day["open"] <= clock <= day["close"]):
        return Result(
            False,
            reason=f"{clock} is outside our {label} hours "
                   f"({day['open']}–{day['close']}). Offer a time inside them.",
        )

    # A time that has already gone by TODAY. The date check above cannot catch
    # this: "today at 9am" said at 6pm is a perfectly valid future-or-present
    # date, and without this it books a slot that is ten hours gone.
    #
    # Compared against `now.date()` rather than `today` on purpose — a caller
    # that fakes `today` for a different reason must not have real wall-clock
    # time applied to a date it never meant to be today.
    if when == now.date():
        current = now.strftime("%H:%M")
        if clock <= current:
            # Is anything left today, or should the agent move to another day?
            if current < day["close"]:
                hint = (
                    f"It is already {current}. We are open until {day['close']} "
                    f"today, so offer a time after {current} — or another day."
                )
            else:
                hint = (
                    f"It is already {current} and we close at {day['close']}, so "
                    "there are no slots left today. Offer the next open day."
                )
            return Result(False, reason=f"{clock} today has already passed. {hint}")

    return Result(True, value=f"{when.isoformat()} {clock}")
