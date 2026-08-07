#!/usr/bin/env python
"""
DualBook — the web server behind BOTH surfaces.

    python run_dashboard.py            # http://localhost:8080

TWO AUDIENCES, TWO PAGES, ONE SERVER
------------------------------------
    /        Owner's console. Bookings, customers, analytics, settings, live
             monitoring. Nobody books anything here — this is the person who
             runs the car wash looking at their business.
    /book    Customer booking page. A visitor talks or types to the agent and
             makes a booking. It shows them nothing about anyone else.

Keeping these apart is not cosmetic. The customer page starts ANONYMOUS: no
phone number, no prefilled identity, no profile. The agent introduces itself,
asks for a name, then a mobile number — and only when that number matches a real
profile does it say "welcome back". A booking console that greets every visitor
by the name in an admin text box is a demo trick; this is the actual product.

The browser supplies the voice hardware (Web Speech API: microphone -> text,
text -> speaker), which is why the voice agent needs no telephony credentials to
be talked to. Uplift/Vapi remain the path for real phone calls (run_voice.py),
and the WhatsApp webhook (run_whatsapp.py) is the path for real messages. Every
one of those routes ends in the same booking_core.complete_booking.
"""

from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dualbook import (
    analytics,
    config,
    llm,
    memory,
    notify,
    settings_store,
    store,
    validate,
)
from dualbook.booking_core import ALL_SLOTS, ConversationEngine

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("dualbook.dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "dualbook" / "static"


def _agent_status() -> dict[str, dict[str, str]]:
    """Report what each channel can actually do right now."""
    try:
        brain_ready = bool(llm.get_client())
    except Exception:
        brain_ready = False

    whatsapp_live = bool(
        config.META_WA_TOKEN if config.WHATSAPP_PROVIDER == "meta" else config.WHAPI_TOKEN
    )
    voice_live = bool(config.VAPI_API_KEY or config.UPLIFT_API_KEY)

    def describe(live: bool, live_label: str) -> dict[str, str]:
        if not brain_ready:
            return {"state": "off", "label": "No LLM key"}
        if live:
            return {"state": "live", "label": live_label}
        # No transport configured, but the brain works — so the agent is fully
        # usable from the panel on this page, which is not a lesser thing than
        # "online": same prompt, same tool, same bookings table.
        return {"state": "ready", "label": "Ready — talk on this page"}

    return {
        "whatsapp": describe(whatsapp_live, "Live on WhatsApp"),
        "voice": describe(voice_live, "Live on calls"),
    }


# ---------------------------------------------------------------------------
# Live agent sessions
# ---------------------------------------------------------------------------
# One ConversationEngine per browser session, keyed by an opaque session id
# rather than by phone number — because on the customer page THERE IS NO PHONE
# NUMBER YET. That is the whole point: the visitor is anonymous until they say
# who they are, and the engine attaches the number mid-conversation (see
# ConversationEngine.identify).
#
# Sessions live in this process, so the durable half (the customer profile) is
# in SQLite and only the in-flight transcript is volatile. uvicorn runs sync
# endpoints on a threadpool, so two visitors talking at once really are
# concurrent — hence the lock.

_SESSIONS: dict[str, ConversationEngine] = {}
_SESSIONS_LOCK = threading.Lock()
_MAX_SESSIONS = 200

CHANNELS = ("voice", "whatsapp")


class AgentStart(BaseModel):
    channel: str
    # Present only when the caller's number is known from the transport (a phone
    # call, a WhatsApp thread). A web visitor sends nothing.
    phone: str | None = None


class AgentSay(BaseModel):
    session: str
    text: str


class AgentEnd(BaseModel):
    session: str


# These three live at MODULE level on purpose. With `from __future__ import
# annotations` every annotation is a string, and FastAPI resolves a handler's
# annotations against its module globals — a model defined inside build_app()
# is invisible there, so every request would fail with a 422 that says nothing
# about the real cause. (run_whatsapp.py has the same note about Request.)
class StatusChange(BaseModel):
    status: str


class PhoneCheck(BaseModel):
    phone: str


class TestMessage(BaseModel):
    phone: str
    body: str | None = None


def _new_session(channel: str, phone: str | None) -> tuple[str, ConversationEngine]:
    import uuid

    engine = ConversationEngine(phone=phone, channel=channel)
    # Stash the saved booking so the page can render a confirmation card with
    # the real row id, rather than parsing it back out of the agent's sentence.
    engine.saved_booking = None  # type: ignore[attr-defined]

    def capture(booking: store.Booking, booking_id: int, _e=engine) -> None:
        # as_row() rather than asdict(): it fills created_at the same way the
        # stored row has it, so the page shows the booking as it was saved.
        _e.saved_booking = {**booking.as_row(), "id": booking_id}

    engine.on_booking = capture

    session_id = uuid.uuid4().hex
    with _SESSIONS_LOCK:
        # A long-running demo shouldn't accumulate abandoned conversations for
        # ever. Oldest first, and never the one we just made.
        while len(_SESSIONS) >= _MAX_SESSIONS:
            _SESSIONS.pop(next(iter(_SESSIONS)))
        _SESSIONS[session_id] = engine
    return session_id, engine


def _agent_state(engine: ConversationEngine, session_id: str) -> dict[str, Any]:
    """What the page needs to draw itself after every turn."""
    profile = engine.profile or {}
    return {
        "session": session_id,
        "phone": engine.phone or None,
        "channel": engine.channel,
        # `identified` = we now know their number. `returning` = we knew them
        # already. The customer page shows the recognition moment on the exact
        # turn both become true.
        "identified": bool(engine.phone),
        "returning": bool(engine.profile),
        "recognised": bool(getattr(engine, "recognised", False)),
        # The name they gave THIS session outranks the one on file — otherwise
        # the summary panel relabels "Ali Raza" as "Ali Khan" the moment their
        # number is recognised. See ConversationEngine.stated_name.
        "customerName": (
            getattr(engine, "captured", {}).get("customer_name")
            or profile.get("customer_name")
        ),
        "bookingCount": profile.get("booking_count") or 0,
        "slots": sorted(engine.known_slots),
        "values": dict(getattr(engine, "captured", {})),
        "allSlots": list(ALL_SLOTS),
        "turns": engine.turns,
        "bookingSaved": engine.booking_saved,
        "booking": getattr(engine, "saved_booking", None),
    }


def build_app():
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="DualBook Dashboard", version="1.0.0")

    @app.on_event("startup")
    def _startup() -> None:
        store.init_db()
        log.info("Dashboard reading %s", config.DB_PATH)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """The owner's console."""
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.get("/book", include_in_schema=False)
    def book() -> FileResponse:
        """The customer's booking page — the link you'd put on a real website."""
        return FileResponse(STATIC_DIR / "book.html")

    @app.get("/api/overview")
    def overview() -> JSONResponse:
        """Everything the page needs, in one round-trip."""
        # Stale rows would otherwise show a crashed agent as permanently "active".
        store.prune_conversations(older_than_minutes=60)

        # Today's schedule is a query, not a slice of the recent list: a car
        # booked in three weeks ago is still on today's rota, and would fall
        # off the end of "the 25 newest rows".
        from datetime import date as _date

        today = _date.today().isoformat()
        todays, _ = store.query_bookings(
            date_from=today, date_to=today, sort="date", direction="asc"
        )

        return JSONResponse(
            {
                # The console's own branding. Sent here rather than fetched
                # separately because the overview runs on every page load, while
                # /api/settings only runs if you open the Settings view — which
                # is why the sidebar used to sit on a placeholder name.
                "business_name": settings_store.get()["business_name"],
                "stats": analytics.stats(),
                "trend": analytics.booking_trend(7),
                "insights": analytics.memory_insights(),
                "conversations": store.list_conversations(limit=10),
                "bookings": store.list_bookings()[:25],
                "today": [b for b in todays if b.get("status") != "cancelled"],
                "channels": analytics.channel_split(),
                "impact": analytics.memory_impact(),
                "revenue": analytics.revenue_summary(),
                "spark": analytics.kpi_series(14),
                "status": analytics.status_split(),
                "outbox": store.list_outbound(6),
                "whatsapp": notify.transport_status(),
                # Drives the two status chips. Three honest states, because
                # "Online" would be a lie this process can't verify — it can
                # see configuration, not whether an agent is actually running:
                #   live  - a transport is configured, so real messages/calls
                #           can reach it
                #   ready - no transport, but the brain works, so the
                #           simulators run the full booking + memory flow
                #   off   - no LLM key; nothing can hold a conversation
                "agents": _agent_status(),
            }
        )

    # -- Bookings (the owner's operational view) ---------------------------

    @app.get("/api/bookings")
    def bookings(
        search: str | None = None,
        channel: str | None = None,
        status: str | None = None,
        service: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "created_at",
        direction: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        rows, total = store.query_bookings(
            search=search, channel=channel, status=status, service=service,
            date_from=date_from, date_to=date_to, sort=sort, direction=direction,
            limit=limit, offset=offset,
        )
        return JSONResponse(
            {
                "rows": rows,
                "total": total,
                "offset": offset,
                "limit": limit,
                "services": settings_store.service_names(),
                "statuses": list(store.BOOKING_STATUSES),
            }
        )

    @app.post("/api/bookings/{booking_id}/status")
    def booking_status(booking_id: int, req: StatusChange) -> JSONResponse:
        """Confirm / complete / cancel — a real state change, not a UI toggle."""
        try:
            row = store.set_booking_status(booking_id, req.status)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        if not row:
            return JSONResponse(
                {"ok": False, "error": f"No booking #{booking_id}."}, status_code=404
            )
        log.info("Booking #%s -> %s", booking_id, req.status)
        return JSONResponse({"ok": True, "booking": row})

    # -- Customers (people, not events) ------------------------------------

    @app.get("/api/customers")
    def customers() -> JSONResponse:
        return JSONResponse({"rows": analytics.customer_table()})

    @app.get("/api/customers/{phone}")
    def customer_detail(phone: str) -> JSONResponse:
        """One customer: their remembered profile and everything they've booked."""
        key = memory.normalize_phone(phone)
        profile = store.get_customer(key)
        history = store.bookings_for_phone(key)
        if not profile and not history:
            return JSONResponse(
                {"ok": False, "error": f"No customer {phone}."}, status_code=404
            )
        return JSONResponse(
            {"ok": True, "profile": profile, "bookings": history,
             "messages": [m for m in store.list_outbound(200) if m["phone"] == key][:10]}
        )

    # -- Analytics ----------------------------------------------------------

    @app.get("/api/analytics")
    def analytics_view(days: int = 30) -> JSONResponse:
        days = max(7, min(int(days), 90))
        return JSONResponse(
            {
                "days": days,
                "series": analytics.daily_series(days),
                "channels": analytics.channel_split(),
                "services": analytics.service_mix(),
                "time_of_day": analytics.time_of_day_split(),
                "status": analytics.status_split(),
                "impact": analytics.memory_impact(),
                "revenue": analytics.revenue_summary(),
                "insights": analytics.memory_insights(),
                "stats": analytics.stats(),
            }
        )

    # -- Settings (the owner's own configuration) ---------------------------

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        return JSONResponse(
            {
                "settings": settings_store.get(),
                "days": list(settings_store.DAYS),
                "day_labels": settings_store.DAY_LABELS,
                "whatsapp": notify.transport_status(),
                "llm": llm.describe_provider(),
                "db_path": config.DB_PATH,
                "voice": {
                    "uplift": bool(config.UPLIFT_API_KEY),
                    "vapi": bool(config.VAPI_API_KEY),
                },
            }
        )

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any]) -> JSONResponse:
        """
        Save business settings. Validated first — a malformed opening-hours
        entry would not fail here, it would fail days later as a booking the
        agent should have refused.
        """
        try:
            updated = settings_store.save(payload)
        except settings_store.SettingsError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        log.info("Settings updated: %s", ", ".join(sorted(payload)))
        return JSONResponse({"ok": True, "settings": updated})

    # -- WhatsApp checks ----------------------------------------------------

    @app.post("/api/whatsapp/validate")
    def whatsapp_validate(req: PhoneCheck) -> JSONResponse:
        """Is this a number we could actually deliver to? (No message sent.)"""
        checked = validate.phone(req.phone)
        return JSONResponse(
            {
                "ok": checked.ok,
                "normalised": checked.value,
                "reason": checked.reason,
                "warning": checked.warning,
                "transport": notify.transport_status(),
            }
        )

    @app.post("/api/whatsapp/test")
    def whatsapp_test(req: TestMessage) -> JSONResponse:
        """
        Send a real test message — the check you run before trusting the number
        on a live customer's confirmation. Validated, then sent (or simulated
        and recorded, if no transport is configured).
        """
        settings = settings_store.get()
        body = (req.body or "").strip() or (
            f"Test message from {settings['business_name']} — your WhatsApp "
            "booking confirmations are working."
        )
        result = notify.send_whatsapp(req.phone, body, kind="test")
        return JSONResponse(result)

    @app.get("/api/outbox")
    def outbox(limit: int = 25) -> JSONResponse:
        return JSONResponse({"rows": store.list_outbound(limit)})

    # -- The agent (customer page) -----------------------------------------
    # Deliberately thin: everything that matters (prompt, slot filling, the
    # save_booking tool, the memory write) is in booking_core, exactly as it is
    # for the WhatsApp webhook and the voice RPC handler. This is a transport,
    # not a third agent.

    @app.post("/api/agent/start")
    def agent_start(req: AgentStart) -> JSONResponse:
        """Open a conversation and return the agent's opening line."""
        if req.channel not in CHANNELS:
            return JSONResponse(
                {"ok": False, "error": f"Unknown channel {req.channel!r}."},
                status_code=400,
            )

        session_id, engine = _new_session(req.channel, req.phone)
        try:
            reply = engine.greet()
        except llm.LLMError as exc:
            log.warning("Agent unavailable: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)})

        log.info(
            "[%s] session %s started (%s)",
            engine.channel, session_id[:8],
            engine.phone or "anonymous visitor",
        )
        return JSONResponse(
            {"ok": True, "reply": reply, **_agent_state(engine, session_id)}
        )

    @app.post("/api/agent/say")
    def agent_say(req: AgentSay) -> JSONResponse:
        """One customer turn in, one agent turn out."""
        engine = _SESSIONS.get(req.session)
        if engine is None:
            return JSONResponse(
                {"ok": False, "expired": True,
                 "error": "That conversation has expired — start a new one."},
                status_code=404,
            )

        text = req.text.strip()
        if not text:
            return JSONResponse({"ok": False, "error": "Empty message."}, status_code=400)

        log.info("[%s] <- %s: %s", engine.channel, engine.phone or "?", text)
        try:
            reply = engine.handle(text)
        except llm.LLMError as exc:
            # The transcript survives, so the customer can simply say it again.
            log.warning("Agent unavailable: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)})

        log.info("[%s] -> %s: %s", engine.channel, engine.phone or "?", reply)
        return JSONResponse(
            {"ok": True, "reply": reply, **_agent_state(engine, req.session)}
        )

    @app.post("/api/agent/end")
    def agent_end(req: AgentEnd) -> JSONResponse:
        """Hang up: drop the transcript and the dashboard's live row."""
        with _SESSIONS_LOCK:
            engine = _SESSIONS.pop(req.session, None)
        if engine is not None:
            try:
                store.clear_conversation(engine.conv_key)
            except Exception:  # pragma: no cover - a stale row is pruned anyway
                log.debug("Could not clear conversation row", exc_info=True)
        return JSONResponse({"ok": True})

    @app.get("/api/agent/intro")
    def agent_intro() -> JSONResponse:
        """
        What the customer page shows before anyone says anything: the business
        name, the priced menu and today's hours. Read from settings, so the page
        a customer sees and the prices the agent quotes can never drift apart.
        """
        from datetime import date as _date

        settings = settings_store.get()
        today_key = settings_store.DAYS[_date.today().weekday()]
        today_hours = settings["hours"][today_key]

        # Proof points for the customer page. Aggregate counts only — no other
        # customer's name, number or booking ever reaches this page — and every
        # figure is derived from real rows. There is no satisfaction score here
        # because the system never asks anyone to rate anything; an invented one
        # would be the only dishonest number on the site.
        mix = analytics.service_mix()
        popular = next(
            (row["service"] for row in mix if row["service"] != "Not specified"), None
        )
        counts = analytics.status_split()
        impact = analytics.memory_impact()

        return JSONResponse(
            {
                "business_name": settings["business_name"],
                "popular_service": popular,
                "proof": {
                    "washes_completed": counts.get("completed", 0),
                    "customers_recognised": analytics.memory_insights()["returning_count"],
                    "turns_saved_pct": impact.get("turns_saved_pct"),
                },
                "address": settings.get("address"),
                "phone": settings.get("phone"),
                "currency": settings.get("currency"),
                "services": settings.get("services", []),
                "hours": settings["hours"],
                "day_labels": settings_store.DAY_LABELS,
                "today": {
                    "day": settings_store.DAY_LABELS[today_key],
                    "closed": bool(today_hours.get("closed")),
                    "open": today_hours.get("open"),
                    "close": today_hours.get("close"),
                },
                "ready": _agent_status()["voice"]["state"] != "off",
            }
        )

    @app.post("/api/export")
    def export(payload: dict[str, Any] | None = None) -> JSONResponse:
        """
        Export to CSV. With filters, exports exactly what the owner is looking
        at — handing someone the whole book when they filtered to "cancelled,
        this week" is a quiet way to give them the wrong spreadsheet.
        """
        payload = payload or {}
        filters = {k: v for k, v in payload.items() if k in {
            "search", "channel", "status", "service", "date_from", "date_to"
        } and v}
        rows = store.query_bookings(**filters)[0] if filters else None
        path = store.export_csv(rows=rows)
        count = len(rows) if rows is not None else len(store.list_bookings())
        log.info("Exported %s bookings to %s", count, path)
        return JSONResponse(
            {"ok": True, "rows": count, "path": path, "filtered": bool(filters)}
        )

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="DualBook admin dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    import uvicorn

    print(f"\n  DualBook dashboard  ->  http://{args.host}:{args.port}\n")
    uvicorn.run(build_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
