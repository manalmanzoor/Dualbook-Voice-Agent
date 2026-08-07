"""
Shared booking logic. THE single source of truth for both channels.

WhatsApp (run_whatsapp.py) and Voice (run_voice.py) differ only in transport.
Everything that is a *business rule* lives here:

    - which fields make a booking (SLOTS)
    - the tool contract the model calls to finalise one (BOOKING_TOOL_*)
    - the system prompt, including the pre-loaded memory block
    - what "completing a booking" means (complete_booking)

The voice agent does not re-implement any of this: it reuses `build_system_prompt`
and `booking_tool_parameters` when provisioning the Uplift assistant, and calls
`complete_booking` from the RPC handler. The WhatsApp agent uses the same prompt
and tool with the Claude API, plus `ConversationEngine` to drive the loop.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from . import config, llm, memory, settings_store, store, validate

log = logging.getLogger(__name__)

# --- Slot definition ---------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    name: str
    description: str
    required: bool


SLOTS: tuple[Slot, ...] = (
    Slot("customer_name", "The customer's name.", True),
    Slot(
        "vehicle_type",
        "Type/model of vehicle, e.g. 'sedan', 'SUV', 'Toyota Corolla', 'pickup'.",
        True,
    ),
    Slot(
        "preferred_date",
        "The date they want the wash. Resolve relative dates ('tomorrow', "
        "'this Saturday') into an absolute YYYY-MM-DD date before submitting.",
        True,
    ),
    Slot(
        "preferred_time",
        "The time they want, e.g. '9:30 AM', '14:00', 'morning'.",
        True,
    ),
    Slot(
        "contact_details",
        "Best contact number for confirmation, in full digits, e.g. "
        "'+923001234567'. MUST be an actual phone number or email address - "
        "never a phrase like 'this number', 'same as before' or 'the one I'm "
        "calling from'. If they want to be reached on the number they are "
        "contacting you from, submit that number's actual digits.",
        True,
    ),
    Slot(
        "service_type",
        "Which service they want, exactly as named on the menu in your "
        "instructions. Optional - if they have no preference, leave it out.",
        False,
    ),
)

REQUIRED_SLOTS = tuple(s.name for s in SLOTS if s.required)
ALL_SLOTS = tuple(s.name for s in SLOTS)


def missing_slots(collected: dict[str, Any]) -> list[str]:
    """Which required fields are still outstanding. The core of slot filling."""
    return [
        name
        for name in REQUIRED_SLOTS
        if not str(collected.get(name) or "").strip()
    ]


def is_complete(collected: dict[str, Any]) -> bool:
    return not missing_slots(collected)


# --- Tool contract -----------------------------------------------------------
# One schema, rendered into two shapes: Anthropic tools want `input_schema`,
# Uplift assistant tools want `parameters`. Defining it once is what keeps the
# two agents genuinely in sync — adding a slot changes both channels at once.

BOOKING_TOOL_NAME = "save_booking"
BOOKING_TOOL_DESCRIPTION = (
    "Finalise and save the car wash booking. Call this ONLY once you have "
    "confirmed every required detail with the customer. Calling it ends the "
    "booking flow, so do not call it speculatively or with placeholder values."
)


def booking_tool_parameters() -> dict[str, Any]:
    """
    JSON Schema for the booking tool's arguments.

    The service list is read from the owner's live menu rather than hardcoded,
    so renaming a service on the Settings page changes what the model is allowed
    to submit — not just what the prompt suggests.
    """
    menu = settings_store.service_names()
    properties: dict[str, Any] = {}
    for slot in SLOTS:
        schema: dict[str, Any] = {"type": "string", "description": slot.description}
        if slot.name == "service_type" and menu:
            schema["enum"] = menu
            schema["description"] += f" One of: {', '.join(menu)}."
        properties[slot.name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": list(REQUIRED_SLOTS),
    }


def llm_tool() -> dict[str, Any]:
    """
    Provider-neutral tool definition (text channels).

    dualbook/llm.py renders this into whichever wire shape the configured
    provider wants — OpenAI-style `function`/`parameters` for Groq, Gemini,
    OpenRouter and Ollama, or `input_schema` for Anthropic.
    """
    return {
        "name": BOOKING_TOOL_NAME,
        "description": BOOKING_TOOL_DESCRIPTION,
        "parameters": booking_tool_parameters(),
    }


def uplift_tool(timeout: int = 10) -> dict[str, Any]:
    """
    Tool definition in Uplift Realtime Assistant shape (voice channel).

    Uplift executes tools by RPC against the participant that joins the room,
    so the schema is declared here and the handler is registered in
    run_voice.py — but the *contract* is identical to the WhatsApp one.
    """
    return {
        "name": BOOKING_TOOL_NAME,
        "description": BOOKING_TOOL_DESCRIPTION,
        "parameters": booking_tool_parameters(),
        "timeout": timeout,
    }


# --- Prompt ------------------------------------------------------------------

_CHANNEL_STYLE = {
    "whatsapp": (
        "You are chatting over WhatsApp. Keep every reply to 1-2 short "
        "sentences. No markdown, no bullet lists, no headings - plain text "
        "only, the way a person texts."
    ),
    "voice": (
        "You are speaking on a phone call. Keep every reply to one or two "
        "short spoken sentences. Never read out lists, symbols, or formatting. "
        "Say dates and times the way a person says them out loud "
        "('Tuesday the fourteenth, half past nine'). If you did not hear "
        "something clearly, ask them to repeat it rather than guessing."
    ),
}


def build_system_prompt(
    profile: dict[str, Any] | None,
    channel: str = "whatsapp",
    today: str | None = None,
    caller_phone: str | None = None,
    settings: dict[str, Any] | None = None,
    anonymous: bool = False,
    stated_name: str | None = None,
) -> str:
    """
    Build the agent's system prompt.

    Two things are spliced in here, and they are the whole personality of this
    system:

      1. THE BUSINESS (settings_store) — trading name, the priced service menu,
         opening hours. Read live, so an owner who changes their Friday hours
         changes what the agent says next conversation, with no redeploy.

      2. THE CUSTOMER (memory.format_profile_for_prompt) — the pre-loaded
         profile, which is what lets the agent open with "Welcome back, Ali"
         instead of "Hi, can I take your name?".

    `anonymous=True` is the honest case for a web visitor: we do NOT know who
    they are, so the agent is told to introduce itself, ask for a name, and
    only then work towards a number — at which point ConversationEngine can do
    a real lookup and recall them for real. Greeting an unknown visitor by a
    name we guessed from a prefilled field is worse than not greeting them.
    """
    today = today or date.today().isoformat()
    settings = settings or settings_store.get()
    slot_lines = "\n".join(
        f"  - {slot.name}{'' if slot.required else ' (optional)'}: {slot.description}"
        for slot in SLOTS
    )

    # Tell the model the number the customer is actually reaching us from.
    # Without this it has no way to know it — and when the customer says "use
    # this number", the model will happily INVENT a plausible-looking one. A
    # fabricated phone number on a booking is worse than a missing one, because
    # nothing about it looks wrong.
    if caller_phone:
        identity_block = (
            f"THE CUSTOMER IS CONTACTING YOU FROM: {caller_phone}\n"
            "If they say to use 'this number' or 'the same number', submit "
            f"exactly {caller_phone} as contact_details. Never invent a number."
        )
    elif anonymous:
        identity_block = (
            "YOU DO NOT KNOW WHO YOU ARE SPEAKING TO. They have reached you "
            "through the website, so there is no number attached to this "
            "conversation.\n"
            "- Open by greeting them on behalf of the business and asking their "
            "NAME first. Use their name once you have it. Introduce yourself "
            "as the booking assistant — never invent a human name for "
            "yourself.\n"
            "- Then ask for their mobile number, and say why: it is where the "
            "booking confirmation goes.\n"
            "- Never assume you have met them before, and never guess a name "
            "or a number. If they tell you their number and you have served "
            "them before, you will be told so explicitly."
        )
    else:
        identity_block = (
            "You do not know this customer's number. Ask for it, and never "
            "invent one."
        )

    return f"""You are the booking assistant for {settings['business_name']}, a car wash service.
Your one job is to book a car wash through natural conversation.

{_CHANNEL_STYLE.get(channel, _CHANNEL_STYLE['whatsapp'])}
{settings.get('agent_persona') or ''}

Today's date is {today}.

{identity_block}

THE BUSINESS
Services (quote the price if they ask, never invent one):
{settings_store.menu_for_prompt(settings)}

Opening hours:
{settings_store.hours_for_prompt(settings)}

- Bookings are taken up to {settings.get('max_days_ahead', 30)} days ahead.
- Never offer or accept a slot outside these hours. If they ask for one, say
  when you ARE open that day and offer the nearest time inside it.

INFORMATION TO COLLECT
{slot_lines}

HOW TO COLLECT IT
- Ask for at most two missing details per message. Rattling off all five at
  once reads like a form, not a conversation.
- Never re-ask for something the customer already gave you, and never re-ask
  for something in their remembered profile that they just confirmed.
- Accept natural answers. "tomorrow morning" gives you a date AND a time;
  don't ask again for either.
- Resolve relative dates against today's date before saving.
- service_type is optional. Offer the menu once; if they don't care, move on
  rather than pressing.

FINISHING
- When you have all the required details, read them back in one short
  confirmation and ask them to confirm.
- Once the customer confirms, call the `{BOOKING_TOOL_NAME}` tool with the
  final values. Do not claim the booking is saved before that tool call
  succeeds.
- If the tool comes back with a problem (a closed day, a time outside opening
  hours, an unusable number), tell the customer plainly, offer a workable
  alternative, and call the tool again once they agree. Never present a
  rejected booking as confirmed.
- After it succeeds, give a brief confirmation and stop.

--- CUSTOMER MEMORY (pre-loaded at the start of this conversation) ---
{memory.format_profile_for_prompt(profile, stated_name)}
"""


# --- Completing a booking ----------------------------------------------------


def resolve_contact(raw: Any, phone: str, said: str | None = None) -> str:
    """
    Guarantee `contact_details` holds a number the customer ACTUALLY gave.

    Two separate failure modes, both seen in real runs against a live model:

    1. Non-answers. The model echoes the customer's words — "this number",
       "same as before" — which is useless to a business owner reading the CSV.

    2. FABRICATION, which is worse. Asked for a contact number the model was
       never told, one model confidently invented `+923001234567` for a
       customer whose actual number was `+923451234567`. Nothing about a
       fabricated number looks wrong, so it silently corrupts the record.

    `said` is the customer's own words this session. When supplied, any phone
    number that does not appear there — and is not the number they contacted us
    from — is treated as invented and discarded. A prompt can ask the model not
    to make things up; this makes it structurally unable to.
    """
    text = str(raw or "").strip()
    fallback = memory.normalize_phone(phone)

    if not text:
        return fallback

    # An email is a legitimate contact — but only if they actually said it.
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}", text):
        if said is None or text.lower() in said.lower():
            return text
        log.warning("contact_details %r never appeared in the conversation", text)
        return fallback

    # A usable phone number needs enough digits to dial. 7 is the shortest real
    # subscriber number; "same as before" has none, "9:30 AM" has three.
    digits = re.sub(r"\D", "", text)
    if len(digits) < 7:
        log.info("contact_details %r is not reachable — using %s", text, fallback)
        return fallback

    if said is not None and not _digits_were_said(digits, said, fallback):
        log.warning(
            "contact_details %r was never said by the customer — using %s instead",
            text, fallback,
        )
        return fallback

    return memory.normalize_phone(text)


# How many trailing digits must agree for two written forms to be the same
# number. 9 covers the national significant number (operator code + subscriber)
# so "+923001234567", "923001234567" and "03001234567" all match, while genuinely
# different numbers do not. Matching on only 7 is too loose: +923001234567 and
# +923451234567 share the suffix "1234567" and would wrongly compare equal.
_SIGNIFICANT_DIGITS = 9

# A written phone number: a digit run that may contain spaces, dashes, brackets
# or a leading '+'. Used to pull candidate numbers out of what the customer said.
_NUMBER_IN_TEXT = re.compile(r"\+?\d[\d\s\-().]{5,}\d")


def _same_number(a: str, b: str) -> bool:
    """Do two written forms denote the same phone number?"""
    a_digits, b_digits = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    if not a_digits or not b_digits:
        return False
    if a_digits == b_digits:
        return True
    if len(a_digits) >= _SIGNIFICANT_DIGITS and len(b_digits) >= _SIGNIFICANT_DIGITS:
        return a_digits[-_SIGNIFICANT_DIGITS:] == b_digits[-_SIGNIFICANT_DIGITS:]
    return False


def _digits_were_said(digits: str, said: str, caller: str) -> bool:
    """
    True if this number is the caller's own, or genuinely appears in what the
    customer said.

    Candidate numbers are extracted from the text and compared one at a time.
    Flattening the whole transcript to a digit string instead would invent
    matches across unrelated values — a date and a time sitting next to each
    other can spell out a plausible phone number that nobody ever said.
    """
    if _same_number(digits, caller):
        return True
    return any(
        _same_number(digits, candidate.group())
        for candidate in _NUMBER_IN_TEXT.finditer(said)
    )


class BookingRejected(ValueError):
    """
    A booking the business cannot honour.

    Carries a message written for the MODEL to relay, not for a log file: the
    tool handler feeds it straight back into the conversation so the agent can
    fix the problem with the customer ("we're closed Sundays — Saturday at 10?")
    instead of failing silently or, worse, confirming something impossible.
    """


def check_booking(
    arguments: dict[str, Any],
    phone: str | None = None,
    said: str | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Validate a proposed booking and return the cleaned values.

    Raises BookingRejected with a fixable explanation. This is the gate the
    model cannot talk its way past: the prompt asks it to respect opening hours,
    but only this function actually enforces them, and it runs on every channel
    because every channel goes through complete_booking.
    """
    settings = settings or settings_store.get()
    clean = {name: str(arguments.get(name) or "").strip() for name in ALL_SLOTS}

    missing = missing_slots(clean)
    if missing:
        raise BookingRejected(
            f"Still missing: {', '.join(missing)}. Ask the customer for these, "
            "then call the tool again."
        )

    # 1. A contact number that is real, and that the customer actually gave.
    contact = resolve_contact(clean["contact_details"], phone or "", said=said)
    checked = validate.phone(contact)
    if not checked.ok:
        raise BookingRejected(
            f"{checked.reason} Ask the customer to say their mobile number "
            "digit by digit, then call the tool again."
        )
    clean["contact_details"] = checked.value

    # 2. A slot the business can honour.
    slot = validate.booking_slot(clean["preferred_date"], clean["preferred_time"], settings)
    if not slot.ok:
        raise BookingRejected(slot.reason or "That slot is not available.")

    # 3. A service that is actually on the menu. An off-menu string would reach
    #    the owner's job sheet as work nobody has priced.
    menu = settings_store.service_names(settings)
    if clean["service_type"] and menu:
        match = next(
            (m for m in menu if m.lower() == clean["service_type"].lower()), None
        )
        if not match:
            raise BookingRejected(
                f"{clean['service_type']!r} is not on the menu. Offer one of: "
                f"{', '.join(menu)}."
            )
        clean["service_type"] = match

    return clean


def complete_booking(
    phone: str | None,
    arguments: dict[str, Any],
    channel: str,
    db_path: str | None = None,
    notes: str | None = None,
    said: str | None = None,
    turns: int | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[store.Booking, int]:
    """
    Persist a finished booking and update the customer's memory profile.

    Called from EVERY channel — the WhatsApp tool-use loop, the voice RPC
    handler, and the web agents. This function is the entire definition of "a
    booking happened", which is why no transport writes to the store directly.

    `phone` may be None for a web visitor who arrived with no number attached.
    In that case the number they gave in conversation becomes their memory key,
    which is exactly right: identity here IS the mobile number, however we
    came to learn it.
    """
    settings = settings or settings_store.get()
    clean = check_booking(arguments, phone=phone, said=said, settings=settings)

    key = memory.normalize_phone(phone) if phone else clean["contact_details"]

    booking = store.Booking(
        phone=key,
        customer_name=clean["customer_name"],
        vehicle_type=clean["vehicle_type"],
        preferred_date=clean["preferred_date"],
        preferred_time=clean["preferred_time"],
        service_type=clean["service_type"] or None,
        contact_details=clean["contact_details"],
        channel=channel,
        notes=notes,
        turns=turns,
    )

    booking_id = store.save_booking(booking, db_path=db_path)

    # DECISION 3 in practice: the memory write happens here, after the booking
    # is durable, from the confirmed values only. See memory.py.
    memory.update_profile_from_booking(booking, db_path=db_path)

    # Tell the customer, on WhatsApp, in writing. Best-effort and never allowed
    # to fail the booking — the row is already durable by this point.
    try:
        from . import notify

        notify.booking_confirmation(booking, booking_id, settings=settings)
    except Exception:  # pragma: no cover - notification must not break booking
        log.exception("Confirmation message failed for booking %s", booking_id)

    store.print_booking(booking, booking_id)
    return booking, booking_id


# --- Recognising a name the customer gives us in conversation ----------------
# The profile is keyed on the PHONE NUMBER, and a number changes hands: family
# handsets, office lines, resold SIMs. So when the person in front of us says
# who they are, theirs is the current truth and the stored name may be stale.
# Detecting that is what stops the agent greeting Ali Raza as "Ali Khan".
#
# Deterministic for the same reason _detect_phone is: this decides whose name
# goes on the booking, and a model call to ask "did they give a name?" would be
# both slower and less reliable than the patterns below. Everything here is
# conservative — a missed name costs one extra question, an invented one puts a
# stranger's name on a real booking.

_STATED_NAME_PATTERNS = (
    re.compile(r"\bmy name(?:'s| is)\s+(?P<name>[^,.!?;]+)", re.I),
    re.compile(r"\bi\s*(?:'m|m|am)\s+(?P<name>[^,.!?;]+)", re.I),
    re.compile(r"\bthis is\s+(?P<name>[^,.!?;]+)", re.I),
    re.compile(r"\b(?:call me|name'?s)\s+(?P<name>[^,.!?;]+)", re.I),
    re.compile(r"\bname\s*[:\-]\s*(?P<name>[^,.!?;]+)", re.I),
)

# Words that sit exactly where a name would but never are one. Without this,
# "I'm looking for a wash tomorrow" books a customer called "Looking".
_NOT_A_NAME = frozenset(
    """
    a an the and or but so just still also only very really quite too
    i me my mine myself you your yours we us our they them their
    here there now then soon later today tomorrow tonight yesterday
    morning afternoon evening night noon midday midnight am pm oclock
    monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october
    november december
    from at in on with about for to of by
    looking interested wondering trying hoping thinking calling ringing
    booking booked book wanting want need needing after free available busy
    sorry sure fine good great ok okay yes yeah yep no nope not never
    new back again first next last ready done finished waiting
    urgent important possible correct right wrong easy hard difficult
    expensive cheap perfect terrible amazing strange normal usual regular
    different same fast slow quick late early
    wash washing clean cleaning car vehicle sedan suv hatchback van truck bike
    express premium basic full standard interior exterior detail detailing
    service customer client anyone someone nobody everyone
    """.split()
)


def _clean_name(raw: str) -> str | None:
    """
    Validate a candidate name pulled out of free text. Returns None for
    anything doubtful — see the note above on which way to fail.
    """
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    # Cut a clause the pattern over-captured: "Ali and I need a wash" -> "Ali".
    text = re.split(r"\b(?:and|but|so|because|who|which|that|calling|from)\b",
                    text, maxsplit=1, flags=re.I)[0]
    text = text.strip(" .,-'\"")
    if not text or len(text) > 40:
        return None

    words = text.split()
    if not 1 <= len(words) <= 3:
        return None
    for word in words:
        bare = word.strip("'-")
        if len(bare) < 2 or not bare.replace("'", "").replace("-", "").isalpha():
            return None
        if bare.lower() in _NOT_A_NAME:
            return None
    # "ali raza" -> "Ali Raza", but leave "McDonald" and "RAZA" alone.
    return " ".join(w[:1].upper() + w[1:] for w in words)


# --- LLM-driven conversation engine (text channels) --------------------------


class ConversationEngine:
    """
    Drives a booking conversation with Claude, one user message at a time.

    Used by the WhatsApp agent (and by the offline simulators, which is how the
    same logic can be demoed with no third-party credentials at all). The voice
    channel does NOT use this class — Uplift runs its own realtime LLM — but it
    uses the same prompt and the same tool contract above, so the two agents
    ask for the same things in the same order.
    """

    def __init__(
        self,
        phone: str | None = None,
        channel: str = "whatsapp",
        db_path: str | None = None,
        on_booking: Callable[[store.Booking, int], None] | None = None,
    ) -> None:
        # ANONYMOUS SESSIONS. A WhatsApp message and a phone call both arrive
        # WITH a number, so the profile can be pre-loaded before the first word.
        # A website visitor does not: nobody has identified themselves yet. That
        # is a real difference, and pretending otherwise is how you end up
        # greeting a stranger as "Ali" because a form field was prefilled.
        self.phone = memory.normalize_phone(phone) if phone else ""
        self.anonymous = not self.phone
        # Live conversations are keyed by phone; an unidentified visitor still
        # needs a stable key to appear on the dashboard under.
        self.conv_key = self.phone or f"web-{uuid.uuid4().hex[:8]}"
        self.channel = channel
        self.db_path = db_path
        self.on_booking = on_booking
        self.messages: list[dict[str, Any]] = []
        self.booking_saved = False
        self.turns = 0
        # True once an anonymous session turns out to be someone we know — the
        # customer page uses it to show "we recognised you" at the right moment.
        self.recognised = False
        # The name this person has given us in THIS conversation, which outranks
        # the one stored against their number. Set before _seed_known_slots so
        # the seeding below can defer to it.
        self.stated_name: str | None = None
        self.settings = settings_store.get()

        # PRE-LOAD READ: one indexed lookup, once, at session start — when we
        # have a number to look up.
        self.profile = memory.load_profile(self.phone, db_path=db_path) if self.phone else None
        self._rebuild_prompt()

        # Slots we can already consider known. A returning customer starts with
        # name/vehicle/service pre-filled from memory, which is exactly why
        # their booking is shorter - and why the dashboard shows them further
        # along the progress dots before they've said anything.
        self.known_slots: set[str] = set()
        # The values behind those slot names, so a UI can show "Honda Civic"
        # rather than a tick. Display bookkeeping only — nothing reads this back
        # into the conversation, and the booking is still written from the
        # model's confirmed tool arguments.
        self.captured: dict[str, str] = {}
        self._seed_known_slots()

        self._client = None  # lazily built so importing this module needs no key

    @property
    def name_conflict(self) -> bool:
        """
        True when this number is on file under a different name from the one the
        person has just given us. The booking still goes ahead under THEIR name;
        what this suppresses is the "welcome back" moment, because we can no
        longer honestly claim to know who is on the other end.
        """
        remembered = (self.profile or {}).get("customer_name")
        return bool(
            self.stated_name
            and remembered
            and self.stated_name.strip().lower() != str(remembered).strip().lower()
        )

    def _rebuild_prompt(self) -> None:
        """(Re)build the system prompt — also called when a caller is recognised."""
        self.system_prompt = build_system_prompt(
            self.profile,
            channel=self.channel,
            caller_phone=self.phone or None,
            settings=self.settings,
            anonymous=self.anonymous and not self.phone,
            stated_name=self.stated_name,
        )

    def _seed_known_slots(self) -> None:
        if not self.profile:
            return
        for slot, field in (
            ("customer_name", "customer_name"),
            ("vehicle_type", "vehicle_type"),
            ("service_type", "usual_service"),
        ):
            # A name given in THIS conversation wins over the stored one. The
            # vehicle and the usual service belong to the number and are still
            # worth offering; the name belongs to a person.
            if slot == "customer_name" and self.stated_name:
                continue
            if self.profile.get(field):
                self.known_slots.add(slot)
                self.captured.setdefault(slot, str(self.profile[field]))
        if self.phone:
            # We have a number to reach them on: they're contacting us from it.
            self.known_slots.add("contact_details")
            self.captured.setdefault("contact_details", self.phone)

    # -- Identity ------------------------------------------------------------

    def identify(self, raw_phone: str) -> dict[str, Any] | None:
        """
        Attach a number to an anonymous session, and recall them if we know it.

        This is the one place mid-session memory retrieval is worth its latency
        (MEMORY_DESIGN.md argues against per-turn retrieval, and still does):
        the pre-load could not run at session start because nobody had said who
        they were yet. It runs exactly once, on the turn the number appears, and
        it is a single indexed lookup.

        Returns the profile if this is someone we have served before.
        """
        checked = validate.phone(raw_phone)
        if not checked.ok:
            return None

        key = checked.value or ""
        if key == self.phone:
            return self.profile

        old_key = self.conv_key
        self.phone = key
        self.conv_key = key
        self.anonymous = False
        self.profile = memory.load_profile(key, db_path=self.db_path)
        self._seed_known_slots()
        self.known_slots.add("contact_details")
        self._rebuild_prompt()

        # The live row was filed under a placeholder key; move it, or the
        # dashboard shows the same person twice.
        if old_key != key:
            try:
                store.clear_conversation(old_key, db_path=self.db_path)
            except Exception:  # pragma: no cover - telemetry only
                log.debug("Could not clear placeholder conversation", exc_info=True)

        log.info(
            "Session identified as %s (%s)",
            key,
            "returning customer" if self.profile else "new customer",
        )
        return self.profile

    def _recall_note(self) -> str:
        """
        The nudge handed to the model on the turn we recognise someone.

        It is appended to the customer's own message rather than sent as a
        separate turn, because Anthropic requires strictly alternating roles and
        this has to work on every provider.
        """
        if not self.profile:
            return (
                "\n\n[system: that number is new to us — no profile on file. "
                "Carry on collecting the booking normally, and do not imply you "
                "have met them before.]"
            )
        remembered = self.profile.get("customer_name")
        bits = [f"name {remembered}"] if remembered else []
        for field, label in (("vehicle_type", "drives a"), ("usual_service", "usually books"),
                             ("preferred_time_of_day", "usually comes in the")):
            if self.profile.get(field):
                bits.append(f"{label} {self.profile[field]}")
        count = self.profile.get("booking_count") or 0
        opening = (
            "\n\n[system: that number matches an existing customer — "
            + ", ".join(bits)
            + f", {count} previous booking{'s' if count != 1 else ''}. "
        )

        # They told us a different name a moment ago. Trust the live human over
        # the stored row, and say nothing about the mismatch.
        if self.name_conflict:
            return opening + (
                f"BUT they introduced themselves as {self.stated_name} in this "
                f"conversation, so the stored name is out of date or belongs to "
                f"someone who shares the number. Call them {self.stated_name}, and "
                f"submit {self.stated_name} as customer_name when you save. Do not "
                f"say \"welcome back\", do not mention the name on file, and do not "
                "ask them to explain it. You may still offer the remembered vehicle "
                "and usual service as a suggestion to confirm. Still ask for the "
                "date and time of THIS booking.]"
            )
        return opening + (
            "Acknowledge them by name warmly in your very next sentence and "
            "offer their usual as a one-word confirmation. Still ask for the "
            "date and time of THIS booking.]"
        )

    _PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s\-().]{6,}\d")

    def _detect_phone(self, text: str) -> str | None:
        """
        Pull a phone number out of what the customer just said.

        Deterministic on purpose. Asking the model to call an `identify` tool
        would add a round-trip and a failure mode to the single most important
        moment in the conversation; a regex plus validate.phone() cannot forget.
        """
        for candidate in self._PHONE_IN_TEXT.finditer(text or ""):
            checked = validate.phone(candidate.group())
            if checked.ok:
                return checked.value
        return None

    def _detect_stated_name(self, text: str) -> str | None:
        """Pull a name out of what the customer just said. See _clean_name."""
        for pattern in _STATED_NAME_PATTERNS:
            match = pattern.search(text or "")
            if match:
                name = _clean_name(match.group("name"))
                if name:
                    return name
        # A bare reply to "what's your name?" — the first thing the anonymous
        # flow asks, so it is worth catching. Restricted to that first turn,
        # where a lone "Ali Raza" cannot plausibly be an answer about a date,
        # a service or a vehicle instead.
        if self.turns <= 1 and "customer_name" not in self.known_slots:
            return _clean_name(text)
        return None

    # -- LLM client ----------------------------------------------------------

    @property
    def client(self):
        """Built on first use so importing this module never requires a key."""
        if self._client is None:
            self._client = llm.get_client()
        return self._client

    # -- Public API ----------------------------------------------------------

    def greet(self) -> str:
        """
        Produce the opening line. For a returning customer this is where the
        personalisation shows up, so it is worth a real model call rather than
        a hardcoded string.
        """
        self.messages.append(
            {
                "role": "user",
                "content": "<the customer has just started a conversation>",
            }
        )
        reply = self._run_turn()
        # Publish immediately, so a session shows up on the dashboard's live
        # panel the moment it opens rather than one customer turn later.
        self._publish_state()
        return reply

    def customer_words(self) -> str:
        """Everything the customer themselves said — never the agent's turns."""
        return "\n".join(
            str(m.get("content") or "")
            for m in self.messages
            if m.get("role") == "user"
        )

    def handle(self, user_message: str) -> str:
        """Feed one customer message in, get the agent's reply out."""
        self.turns += 1
        content = user_message

        # Whatever name they give US outranks the one filed against their
        # number. Read BEFORE the lookup below, so identify() and _recall_note()
        # both already know it and can defer to it on the same turn.
        stated = self._detect_stated_name(user_message)
        if stated:
            self.stated_name = stated
            self.known_slots.add("customer_name")
            self.captured["customer_name"] = stated
            self._rebuild_prompt()

        # THE RECOGNITION MOMENT. If we don't yet know who this is and they
        # just said a number, look them up now — before the model composes its
        # reply, so it can greet them by name in the very next sentence.
        if not self.phone:
            found = self._detect_phone(user_message)
            if found:
                self.identify(found)
                content = user_message + self._recall_note()
                # Recognition is a claim about WHO this is, so we only make it
                # when the name we hold matches the one they gave. The profile
                # is still loaded either way — the vehicle and usual service
                # belong to the number, and remain useful suggestions.
                self.recognised = bool(self.profile) and not self.name_conflict

        self.messages.append({"role": "user", "content": content})
        reply = self._run_turn()
        self._publish_state(last_message=user_message)
        return reply

    # -- Dashboard read model ------------------------------------------------

    def _publish_state(self, last_message: str | None = None) -> None:
        """
        Publish a summary of this conversation so a separate process (the
        dashboard) can display it. Best-effort: a failure here must never break
        a customer conversation, so it is swallowed and logged.
        """
        try:
            if self.booking_saved:
                store.clear_conversation(self.conv_key, db_path=self.db_path)
                return
            store.upsert_conversation(
                phone=self.conv_key,
                channel=self.channel,
                status="active",
                last_message=last_message,
                turn_count=sum(1 for m in self.messages if m["role"] == "user"),
                slots=sorted(self.known_slots),
                is_returning=bool(self.profile),
                # Session truth first: the owner watching this panel should see
                # the name the customer actually gave, not the one on the row.
                customer_name=(
                    self.captured.get("customer_name")
                    or (self.profile or {}).get("customer_name")
                ),
                db_path=self.db_path,
            )
        except Exception:  # pragma: no cover - telemetry must not break booking
            log.debug("Could not publish conversation state", exc_info=True)

    # -- Internals -----------------------------------------------------------

    def _run_turn(self) -> str:
        """
        One agent turn: call the model, execute the booking tool if requested,
        loop until the model produces text for the customer.
        """
        reply_parts: list[str] = []

        # Bounded loop: the model gets at most a couple of tool round-trips per
        # turn. A booking needs exactly one, so anything more is a malfunction
        # and we'd rather answer the customer than spin.
        for _ in range(4):
            response = self.client.complete(
                system=self.system_prompt,
                messages=self.messages,
                tools=[llm_tool()],
            )

            if response.refused:
                return (
                    "Sorry, I can't help with that. Shall we carry on with "
                    "your car wash booking?"
                )

            if response.text:
                reply_parts.append(response.text)

            # Echo the assistant turn (tool calls included) before any results.
            if response.raw_assistant_message:
                self.messages.append(response.raw_assistant_message)

            if not response.tool_calls:
                break  # model is done talking for this turn

            for tool_call in response.tool_calls:
                result_text = self._execute_tool(tool_call)
                self.messages.append(
                    self.client.tool_result_message(tool_call, result_text)
                )

        return "\n".join(p for p in reply_parts if p) or "Sorry, could you say that again?"

    def _execute_tool(self, tool_call: "llm.ToolCall") -> str:
        """Run the booking tool; returns the text fed back to the model."""
        if tool_call.name != BOOKING_TOOL_NAME:
            return f"Unknown tool {tool_call.name!r}."

        args = dict(tool_call.arguments or {})

        # Record what the model has actually captured. Even a REJECTED call is
        # informative — it tells us which slots are filled — so progress stays
        # accurate without spending an extra LLM round-trip to ask.
        for name in ALL_SLOTS:
            value = str(args.get(name) or "").strip()
            if value:
                self.known_slots.add(name)
                self.captured[name] = value

        # If they gave the number to the tool but never to us in conversation,
        # this is still the moment we learn who they are.
        if not self.phone and args.get("contact_details"):
            self.identify(str(args["contact_details"]))

        try:
            booking, booking_id = complete_booking(
                phone=self.phone or None,
                arguments=args,
                channel=self.channel,
                db_path=self.db_path,
                # Everything the customer actually said this session, so a
                # number they never gave can be rejected as fabricated.
                said=self.customer_words(),
                turns=self.turns,
                settings=self.settings,
            )
        except BookingRejected as exc:
            # A business rule said no — closed on Sunday, time outside hours,
            # unusable number. Hand the reason back so the model fixes it WITH
            # the customer instead of apologising for a failure they can't see.
            return f"Booking NOT saved. {exc} Do not tell the customer it is confirmed."
        except Exception as exc:  # surfaced to the model, not to the customer
            log.exception("Booking failed")
            return f"Failed to save booking: {exc}"

        self.booking_saved = True
        if self.on_booking:
            self.on_booking(booking, booking_id)

        return (
            f"Booking #{booking_id} saved for {booking.customer_name} "
            f"({booking.vehicle_type}) on {booking.preferred_date} "
            f"at {booking.preferred_time}. Confirm this to the customer."
        )
