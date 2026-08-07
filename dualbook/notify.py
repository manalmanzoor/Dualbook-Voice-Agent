"""
Outbound customer messaging — the confirmation the customer actually receives.

This is the piece that makes a booking feel real to the person who made it. It
also happens to be the riskiest thing the system does, because it is the only
place where the agent's output leaves the building and lands on a stranger's
phone. So every send goes through three gates, in this order:

    1. VALIDATION   — the number is normalised to E.164 and sanity-checked
                      (validate.phone). A mistyped number is not "best effort":
                      it messages someone uninvolved while the real customer
                      hears nothing and assumes they were never booked.
    2. CONSENT      — the owner's `send_confirmation` setting. Off means off.
    3. TRANSPORT    — a configured WhatsApp provider. With none, the message is
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
from typing import Any

from . import config, settings_store, store, validate, whatsapp_client

log = logging.getLogger(__name__)


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
    }


def render_confirmation(
    booking: store.Booking, booking_id: int, settings: dict[str, Any] | None = None
) -> str:
    """Fill the owner's confirmation template with this booking's values."""
    settings = settings or settings_store.get()
    template = settings.get("confirmation_template") or ""
    values = {
        "name": booking.customer_name or "there",
        "business": settings.get("business_name", ""),
        "service": booking.service_type or "car wash",
        "date": booking.preferred_date,
        "time": booking.preferred_time,
        "vehicle": booking.vehicle_type,
        "id": booking_id,
    }
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
) -> dict[str, Any]:
    """
    Send one WhatsApp message, or record why it wasn't sent.

    Never raises: the caller is usually finishing a booking that is already
    durable, and a messaging problem must not undo it. The returned dict — and
    the outbox row — is where the failure surfaces.
    """
    settings = settings or settings_store.get()

    checked = validate.phone(to, settings.get("default_country_code"))
    if not checked.ok:
        store.record_outbound(
            phone=str(to), body=body, status="rejected", kind=kind,
            detail=checked.reason, booking_id=booking_id,
        )
        log.warning("Refusing to send to %r: %s", to, checked.reason)
        return {"ok": False, "status": "rejected", "reason": checked.reason}

    number = checked.value or ""
    status = transport_status()

    if not status["configured"]:
        store.record_outbound(
            phone=number, body=body, status="simulated", kind=kind,
            detail=f"No {status['label']} credentials — message not sent",
            booking_id=booking_id,
        )
        log.info("[simulated WhatsApp -> %s] %s", number, body)
        return {
            "ok": True,
            "status": "simulated",
            "to": number,
            "body": body,
            "reason": (
                f"{status['label']} is not configured "
                f"({', '.join(status['missing'])}), so the message was recorded "
                "but not delivered."
            ),
            "warning": checked.warning,
        }

    try:
        response = whatsapp_client.get_client().send_text(to=number, body=body)
    except Exception as exc:
        store.record_outbound(
            phone=number, body=body, status="failed", kind=kind,
            detail=str(exc)[:400], booking_id=booking_id,
        )
        log.error("WhatsApp send to %s failed: %s", number, exc)
        return {"ok": False, "status": "failed", "to": number, "reason": str(exc)}

    store.record_outbound(
        phone=number, body=body, status="sent", kind=kind,
        detail=str(response)[:400], booking_id=booking_id,
    )
    log.info("WhatsApp confirmation sent to %s", number)
    return {"ok": True, "status": "sent", "to": number, "body": body,
            "warning": checked.warning}


def booking_confirmation(
    booking: store.Booking, booking_id: int, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send the post-booking confirmation, if the owner has it switched on."""
    settings = settings or settings_store.get()
    if not settings.get("send_confirmation", True):
        return {"ok": True, "status": "disabled"}

    body = render_confirmation(booking, booking_id, settings)
    return send_whatsapp(
        to=booking.contact_details or booking.phone,
        body=body,
        kind="booking_confirmation",
        booking_id=booking_id,
        settings=settings,
    )
