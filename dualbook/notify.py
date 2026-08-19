"""
Outbound customer messaging — the confirmation the customer actually receives.

This is the piece that makes a booking feel real to the person who made it. It
also happens to be the riskiest thing the system does, because it is the only
place where the agent's output leaves the building and lands on a stranger's
phone. So every send goes through four gates, in this order:

    1. VALIDATION   — the number is normalised to E.164 and sanity-checked
                      (validate.phone). A mistyped number is not "best effort":
                      it messages someone uninvolved while the real customer
                      hears nothing and assumes they were never booked.
    2. CONSENT      — the owner's `send_confirmation` setting. Off means off.
    3. SHAPE        — session message or template. WhatsApp permits free text
                      only within 24 hours of the recipient's last inbound
                      message; outside that window it must be a pre-approved
                      template. See `choose_mode`.
    4. TRANSPORT    — a configured WhatsApp provider. With none, the message is
                      SIMULATED rather than skipped: recorded in the outbox with
                      status='simulated', so a demo shows exactly what a
                      customer would receive without a WhatsApp account, and the
                      code path is the same one production uses.

Every attempt is recorded in the `outbox` table either way, which is what lets
the dashboard show "here is the message your customer got" instead of asking
anyone to trust that it happened.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, settings_store, store, validate, wa_templates, whatsapp_client

log = logging.getLogger(__name__)

# How long a customer's inbound message keeps free-form replies legal. Meta's
# rule, not ours, and not adjustable on their side.
SESSION_WINDOW_HOURS = 24

# Providers that enforce the window. Whapi links a real WhatsApp account rather
# than a Business API number, so it has neither a session window nor a template
# requirement — treating it as if it did would block sends that would succeed.
_WINDOWED_PROVIDERS = {"meta"}


def transport_status() -> dict[str, Any]:
    """
    Is a real WhatsApp transport wired up, and which one?

    Reported to the dashboard so the owner sees the truth — "messages are being
    simulated" is useful information, "messages sent!" when nothing left the
    building is a lie the demo would carry into production.
    """
    provider = config.WHATSAPP_PROVIDER
    if provider == "meta":
        configured = bool(config.META_WA_TOKEN and config.META_WA_PHONE_NUMBER_ID)
        missing = [
            name
            for name, value in (
                ("META_WA_TOKEN", config.META_WA_TOKEN),
                ("META_WA_PHONE_NUMBER_ID", config.META_WA_PHONE_NUMBER_ID),
            )
            if not value
        ]
        label = "Meta WhatsApp Cloud API"
    else:
        configured = bool(config.WHAPI_TOKEN)
        missing = [] if configured else ["WHAPI_TOKEN"]
        label = "Whapi.Cloud"

    return {
        "provider": provider,
        "label": label,
        "configured": configured,
        "missing": missing,
        "mode": "live" if configured else "simulated",
        # Whether this provider enforces the 24-hour window, and what is
        # declared to send outside it. The dashboard shows this because "we
        # cannot message a voice caller" is something the owner should learn
        # before a customer does.
        "enforces_window": provider in _WINDOWED_PROVIDERS,
        "templates": wa_templates.describe(),
    }


def within_session_window(phone: str, db_path: str | None = None) -> bool:
    """
    Has this number messaged us recently enough that free text is still legal?

    False for a number we have never heard from — which is the ordinary case for
    a booking taken by PHONE, not an edge case. Their confirmation is an opening
    message, and only a template may open a conversation.
    """
    last = store.last_inbound_at(phone, db_path=db_path)
    if last is None:
        return False
    return datetime.now(timezone.utc) - last < timedelta(hours=SESSION_WINDOW_HOURS)


def choose_mode(
    phone: str,
    use_template: bool | None = None,
    db_path: str | None = None,
) -> str:
    """
    Decide which message shape to use: 'session' or 'template'.

    `use_template` overrides the decision in both directions — True to force a
    template (useful for testing one before a real booking depends on it), False
    to insist on free text. Left as None, the window decides, which is what
    every ordinary send does.
    """
    if use_template is not None:
        return "template" if use_template else "session"
    if config.WHATSAPP_PROVIDER not in _WINDOWED_PROVIDERS:
        return "session"
    return "session" if within_session_window(phone, db_path=db_path) else "template"


def confirmation_values(
    booking: store.Booking, booking_id: int, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    The vocabulary a confirmation is written from.

    Shared by both shapes on purpose: the owner's free-text template and the
    Meta-approved template fill from the SAME names, so the two cannot drift
    into saying different things about one booking.
    """
    settings = settings or settings_store.get()
    return {
        "name": booking.customer_name or "there",
        "business": settings.get("business_name", ""),
        "service": booking.service_type or "car wash",
        "date": booking.preferred_date,
        "time": booking.preferred_time,
        "vehicle": booking.vehicle_type,
        "id": booking_id,
    }


def render_confirmation(
    booking: store.Booking, booking_id: int, settings: dict[str, Any] | None = None
) -> str:
    """Fill the owner's confirmation template with this booking's values."""
    settings = settings or settings_store.get()
    template = settings.get("confirmation_template") or ""
    values = confirmation_values(booking, booking_id, settings)
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        # A template the owner half-edited must not stop the customer being
        # told. Fall back to something plainly correct.
        log.warning("Confirmation template is malformed — using the fallback text")
        return (
            f"Hi {values['name']}! Your booking at {values['business']} is "
            f"confirmed for {values['date']} at {values['time']}. "
            f"Booking #{booking_id}."
        )


def send_whatsapp(
    to: str,
    body: str,
    kind: str = "booking_confirmation",
    booking_id: int | None = None,
    settings: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    use_template: bool | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """
    Send one WhatsApp message, or record why it wasn't sent.

    `body` is the free-text wording, used when a session message is legal.
    `values` supplies the template variables for when it is not — pass both and
    this function picks the shape the recipient can actually receive.

    Never raises: the caller is usually finishing a booking that is already
    durable, and a messaging problem must not undo it. The returned dict — and
    the outbox row — is where the failure surfaces.
    """
    settings = settings or settings_store.get()

    checked = validate.phone(to, settings.get("default_country_code"))
    if not checked.ok:
        store.record_outbound(
            phone=str(to), body=body, status="rejected", kind=kind,
            detail=checked.reason, booking_id=booking_id, db_path=db_path,
        )
        log.warning("Refusing to send to %r: %s", to, checked.reason)
        return {"ok": False, "status": "rejected", "reason": checked.reason}

    number = checked.value or ""
    status = transport_status()

    # -- Gate 3: which shape can this recipient legally receive? --------------
    mode = choose_mode(number, use_template=use_template, db_path=db_path)
    template = wa_templates.get(kind) if mode == "template" else None

    if mode == "template" and template is None:
        # Outside the window with nothing approved to send. Recording this as
        # 'blocked' rather than attempting it is the honest outcome: Meta would
        # reject it with error 131047, and a 'failed' row would send whoever
        # reads the outbox looking for a fault that isn't there.
        reason = (
            f"No approved WhatsApp template is declared for {kind!r}, and "
            f"{number} has not messaged us in the last {SESSION_WINDOW_HOURS} "
            "hours — so free text cannot be delivered. Register a template with "
            "Meta and add it to dualbook/templates/whatsapp.json."
        )
        store.record_outbound(
            phone=number, body=body, status="blocked", kind=kind,
            detail=reason, booking_id=booking_id, mode=mode, db_path=db_path,
        )
        log.warning("Blocked %s to %s: outside the session window, no template",
                    kind, number)
        return {"ok": False, "status": "blocked", "to": number, "mode": mode,
                "reason": reason}

    # What the customer will actually read. For a template, Meta renders the
    # approved body from our parameters, so preview it the same way.
    outbox_body = template.render(values or {}) if template else body

    if not status["configured"]:
        store.record_outbound(
            phone=number, body=outbox_body, status="simulated", kind=kind,
            detail=f"No {status['label']} credentials — message not sent",
            booking_id=booking_id, mode=mode, db_path=db_path,
        )
        log.info("[simulated WhatsApp %s -> %s] %s", mode, number, outbox_body)
        return {
            "ok": True,
            "status": "simulated",
            "to": number,
            "body": outbox_body,
            "mode": mode,
            "reason": (
                f"{status['label']} is not configured "
                f"({', '.join(status['missing'])}), so the message was recorded "
                "but not delivered."
            ),
            "warning": checked.warning,
        }

    try:
        client = whatsapp_client.get_client()
        if template is not None:
            response = client.send_template(
                to=number, template=template, values=values or {}
            )
        else:
            response = client.send_text(to=number, body=body)
    except Exception as exc:
        store.record_outbound(
            phone=number, body=outbox_body, status="failed", kind=kind,
            detail=str(exc)[:400], booking_id=booking_id, mode=mode, db_path=db_path,
        )
        log.error("WhatsApp %s send to %s failed: %s", mode, number, exc)
        return {"ok": False, "status": "failed", "to": number, "mode": mode,
                "reason": str(exc)}

    store.record_outbound(
        phone=number, body=outbox_body, status="sent", kind=kind,
        detail=str(response)[:400], booking_id=booking_id, mode=mode, db_path=db_path,
    )
    log.info("WhatsApp %s message sent to %s", mode, number)
    return {"ok": True, "status": "sent", "to": number, "body": outbox_body,
            "mode": mode, "warning": checked.warning}


def booking_confirmation(
    booking: store.Booking,
    booking_id: int,
    settings: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """
    Send the post-booking confirmation, if the owner has it switched on.

    Both shapes are prepared and the recipient decides which is used. That
    matters most for a VOICE booking: the caller has never messaged our WhatsApp
    number, so their confirmation is an opening message and can only go as a
    template.
    """
    settings = settings or settings_store.get()
    if not settings.get("send_confirmation", True):
        return {"ok": True, "status": "disabled"}

    return send_whatsapp(
        to=booking.contact_details or booking.phone,
        body=render_confirmation(booking, booking_id, settings),
        kind="booking_confirmation",
        booking_id=booking_id,
        settings=settings,
        values=confirmation_values(booking, booking_id, settings),
        db_path=db_path,
    )
