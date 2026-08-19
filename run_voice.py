#!/usr/bin/env python
"""
DualBook — Voice booking agent (Component 2), on Uplift AI Realtime Assistants.

    python run_voice.py provision                    # create/update the assistant
    python run_voice.py session +923001234567        # mint a session (memory pre-loaded)
    python run_voice.py session +923001234567 --join # ...and host the booking tool
    python run_voice.py serve                        # backend token endpoint for a client
    python run_voice.py simulate +923001234567       # same booking logic, no credentials

HOW THIS FITS TOGETHER
----------------------
Uplift runs the realtime loop (STT -> LLM -> TTS) on its side and connects the
caller over WebRTC/LiveKit. Tools are NOT server-side webhooks: Uplift invokes
them by RPC against a participant in the room. So to capture the booking we
join the room as a Python participant and register `save_booking`, which calls
straight into booking_core.complete_booking — the same function the WhatsApp
agent's tool handler calls. No business rule is duplicated.

The per-caller memory profile is injected via an ADHOC session, because a
persisted assistant carries one fixed instruction string for every caller and
therefore cannot say "Welcome back, Ali". See uplift_client.create_adhoc_session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from dualbook import booking_core, config, llm, memory, speech, store
from dualbook.booking_core import ConversationEngine
from dualbook.uplift_client import UpliftClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("dualbook.voice")

ASSISTANT_NAME = f"{config.BUSINESS_NAME} - Booking Agent"


# ---------------------------------------------------------------------------
# Prompt construction (shared with WhatsApp via booking_core)
# ---------------------------------------------------------------------------


def voice_instructions(phone: str | None) -> str:
    """
    Build the voice agent's instructions.

    PRE-LOAD READ happens here: one indexed SQLite lookup at session-creation
    time, folded into the system prompt. Nothing is retrieved per turn — on a
    call, a retrieval round-trip would land inside the caller's perceived pause.
    """
    profile = memory.load_profile(phone) if phone else None
    return booking_core.build_system_prompt(profile, channel="voice")


# ---------------------------------------------------------------------------
# provision — create or update the persisted assistant
# ---------------------------------------------------------------------------


def provision() -> int:
    client = UpliftClient()
    # Generic instructions (no caller known yet). Per-caller memory is injected
    # at session time via the adhoc path.
    instructions = voice_instructions(None)

    if config.UPLIFT_ASSISTANT_ID:
        log.info("Updating assistant %s", config.UPLIFT_ASSISTANT_ID)
        result = client.update_assistant(
            config.UPLIFT_ASSISTANT_ID, ASSISTANT_NAME, instructions
        )
    else:
        log.info("Creating a new assistant")
        result = client.create_assistant(ASSISTANT_NAME, instructions)

    assistant_id = result.get("realtimeAssistantId") or result.get("id")
    print("\nAssistant ready.")
    print(f"  realtimeAssistantId: {assistant_id}")
    print(f"\nAdd this to your .env:\n  UPLIFT_ASSISTANT_ID={assistant_id}\n")
    return 0


# ---------------------------------------------------------------------------
# session — mint a session token for a specific caller
# ---------------------------------------------------------------------------


def create_session(
    phone: str,
    use_persisted: bool = False,
    room_name: str | None = None,
    participant_name: str | None = None,
):
    """
    Mint a realtime session for one caller.

    ADHOC IS THE DEFAULT, and that is a memory-layer decision rather than a
    stylistic one. A persisted assistant holds ONE instruction string shared by
    every caller, so it cannot contain "Ali", "SUV" or "Premium Wash" — it is
    structurally incapable of greeting a returning caller by name. The adhoc
    endpoint takes the config inline per session, which is the only place the
    caller's profile can be injected. See MEMORY_DESIGN.md.
    """
    client = UpliftClient()
    profile = memory.load_profile(phone)
    participant = participant_name or (profile or {}).get("customer_name") or "Caller"

    if use_persisted:
        if not config.UPLIFT_ASSISTANT_ID:
            raise config.ConfigError(
                "UPLIFT_ASSISTANT_ID is not set. Run `python run_voice.py provision` "
                "first, or drop --persisted to use an adhoc session."
            )
        log.warning(
            "Using the persisted assistant: its instructions are fixed for all "
            "callers, so this session will NOT greet %s by name.",
            participant,
        )
        return (
            client.create_session(config.UPLIFT_ASSISTANT_ID, participant, room_name),
            profile,
        )

    log.info(
        "Creating adhoc session with pre-loaded memory (returning customer: %s)",
        "yes" if profile else "no",
    )
    return (
        client.create_adhoc_session(voice_instructions(phone), participant, room_name),
        profile,
    )


def session_command(
    phone: str, join: bool, use_persisted: bool, room_name: str | None = None
) -> int:
    store.init_db()
    session, profile = create_session(
        phone,
        use_persisted=use_persisted,
        room_name=room_name,
        # A tool host is a second, non-speaking participant, so give it a
        # distinct identity — LiveKit rejects two participants sharing one.
        participant_name="dualbook-tool-host" if (join and room_name) else None,
    )

    print("\n--- Uplift realtime session ---")
    print(f"  room:   {session.room_name}")
    print(f"  wsUrl:  {session.ws_url}")
    print(f"  token:  {session.token[:32]}...  (truncated)")
    print(
        "  memory: "
        + (
            f"pre-loaded ({profile.get('customer_name')}, "
            f"{profile.get('booking_count')} prior bookings)"
            if profile
            else "none - new caller"
        )
    )

    if not join:
        print(
            "\nHand wsUrl + token to a LiveKit client to talk to the agent.\n"
            "That client must register a `save_booking` RPC handler, OR run a\n"
            "tool host alongside it in the same room:\n"
            f"  python run_voice.py session {phone} --room {session.room_name} --join\n"
        )
        return 0

    return asyncio.run(join_room(session, phone))


# ---------------------------------------------------------------------------
# join — act as the room participant that hosts the booking tool
# ---------------------------------------------------------------------------


async def join_room(session, phone: str) -> int:
    """
    Join the LiveKit room and register the `save_booking` RPC method that
    Uplift's agent invokes when the caller confirms.

    Uplift executes assistant tools as RPC calls against a participant, so
    whichever client joins the room must host the handler. Doing it here keeps
    the booking write in Python, next to the store and the memory layer.

    ON PARTICIPANT IDENTITY (the non-obvious bit)
    ---------------------------------------------
    A session token identifies exactly ONE participant, and LiveKit rejects two
    participants sharing an identity. So there are two valid topologies:

      1. Headless demo  - this process is the only participant. It hosts the
                          tool; there is no separate human client.
      2. Real call      - a browser/mobile client speaks to the caller, and a
                          SECOND token joins the same room as the tool host:
                            run_voice.py session <phone> --room <name> --join

    In topology 2 the agent must direct the RPC at this participant. If your
    client also registers `save_booking`, drop the tool host and let the client
    own it — but the handler still has to call booking_core.complete_booking,
    or the voice channel forks away from the shared booking path.
    """
    try:
        from livekit import rtc
    except ImportError:
        print(
            "\nThe `livekit` package is required for --join.\n"
            "  pip install livekit\n"
            "Without it you can still hand wsUrl+token to a browser/mobile "
            "LiveKit client, but that client must register the save_booking "
            "RPC handler itself.\n",
            file=sys.stderr,
        )
        return 1

    room = rtc.Room()

    async def handle_save_booking(rpc_data: Any) -> str:
        """
        RPC entry point. Returns a stringified JSON result, which is the shape
        Uplift expects back from a tool:
            {"result": ..., "presentationInstructions": ..., "error": ...}
        """
        try:
            args = _extract_rpc_arguments(rpc_data)
            log.info("save_booking invoked with: %s", args)

            # Business rules (opening hours, menu, a dialable number) are
            # enforced in booking_core, not here — the caller hears the reason
            # and can fix it in the same breath.
            try:
                booking_core.check_booking(args, phone=phone)
            except booking_core.BookingRejected as rejection:
                return json.dumps(
                    {
                        "error": str(rejection),
                        "presentationInstructions": (
                            "Tell the caller what the problem is in one short "
                            "sentence, offer the alternative, and try saving "
                            "again once they agree. Do not say it is booked."
                        ),
                    }
                )

            # SAME function the WhatsApp agent calls. Persists the booking and
            # runs the post-processing memory write.
            booking, booking_id = booking_core.complete_booking(
                phone=phone, arguments=args, channel="voice"
            )

            return json.dumps(
                {
                    "result": {
                        "booking_id": booking_id,
                        "customer_name": booking.customer_name,
                        "vehicle_type": booking.vehicle_type,
                        "date": booking.preferred_date,
                        "time": booking.preferred_time,
                        "service": booking.service_type,
                    },
                    "presentationInstructions": (
                        "Confirm the booking to the caller in one short spoken "
                        "sentence, mention the day and time, then thank them "
                        "and end the call."
                    ),
                }
            )
        except Exception as exc:
            log.exception("save_booking failed")
            return json.dumps(
                {
                    "error": str(exc),
                    "presentationInstructions": (
                        "Apologise briefly, say the booking could not be saved, "
                        "and offer to try once more."
                    ),
                }
            )

    log.info("Connecting to %s ...", session.ws_url)
    await room.connect(session.ws_url, session.token)

    # register_rpc_method lives on the local participant, which only exists
    # once the room is connected.
    room.local_participant.register_rpc_method(
        booking_core.BOOKING_TOOL_NAME, handle_save_booking
    )
    identity = getattr(room.local_participant, "identity", "(unknown)")
    log.info(
        "Connected to room %r as participant %r, hosting RPC method %r. "
        "Ctrl-C to disconnect.",
        session.room_name,
        identity,
        booking_core.BOOKING_TOOL_NAME,
    )

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await room.disconnect()
        log.info("Disconnected.")
    return 0


def _extract_rpc_arguments(rpc_data: Any) -> dict[str, Any]:
    """
    Pull the tool arguments out of an RPC invocation.

    Defensive on purpose: the LiveKit SDK hands us `RpcInvocationData.payload`
    as a JSON string, while Uplift's own JS examples show the arguments nested
    at `payload.arguments.raw_arguments`. We accept both rather than betting on
    one shape and failing silently mid-call.
    """
    payload = getattr(rpc_data, "payload", rpc_data)

    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, dict):
        return {}

    # Unwrap the nested forms Uplift may use.
    for key in ("arguments", "input", "parameters"):
        if key in payload:
            inner = payload[key]
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except json.JSONDecodeError:
                    continue
            if isinstance(inner, dict):
                raw = inner.get("raw_arguments", inner)
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                if isinstance(raw, dict):
                    payload = raw
                    break

    return {k: v for k, v in payload.items() if k in booking_core.ALL_SLOTS}


# ---------------------------------------------------------------------------
# serve — backend token endpoint (the production shape)
# ---------------------------------------------------------------------------


def serve(host: str, port: int) -> int:
    """
    Minimal backend for a browser/mobile client.

    Uplift's docs are explicit: "Never expose your API key in client-side code.
    Always create sessions from your backend server." This endpoint is that
    backend — the client sends a phone number and gets back only a scoped,
    short-lived room token.
    """
    from fastapi import FastAPI
    from pydantic import BaseModel
    import uvicorn

    app = FastAPI(title="DualBook Voice Session Broker", version="1.0.0")

    class SessionRequest(BaseModel):
        phone: str

    @app.on_event("startup")
    def _startup() -> None:
        store.init_db()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/session")
    def new_session(req: SessionRequest) -> dict[str, Any]:
        session, profile = create_session(req.phone)
        return {
            "token": session.token,
            "wsUrl": session.ws_url,
            "roomName": session.room_name,
            "returningCustomer": bool(profile),
            # Handy for a client that wants to show "Welcome back, Ali" in the
            # UI as well as hear it.
            "customerName": (profile or {}).get("customer_name"),
        }

    log.info("Token endpoint: POST http://%s:%s/session  {\"phone\": \"+92...\"}", host, port)
    log.info(
        "The joining client must register an RPC method named %r to receive "
        "the booking, or run `run_voice.py session <phone> --join` alongside it.",
        booking_core.BOOKING_TOOL_NAME,
    )
    uvicorn.run(app, host=host, port=port)
    return 0


# ---------------------------------------------------------------------------
# simulate — same booking logic, typed instead of spoken
# ---------------------------------------------------------------------------


def _next_caller_turn(listener) -> str | None:
    """
    Get the caller's next turn — spoken or typed.

    Typing stays available even in voice mode on purpose. Offline recognition
    misfires on names, registration plates and numbers often enough that
    forcing speech would make the agent look broken when the recogniser, not
    the agent, is what failed. Enter listens; anything typed is taken as-is.

    Returns None to end the call.
    """
    if not (listener and listener.available):
        return input("caller> ").strip()

    typed = input("caller> [Enter to speak, or type] ").strip()
    if typed:
        return typed

    print("        listening... (speak now)", flush=True)
    heard = listener.listen()
    if not heard:
        print("        didn't catch that — press Enter to retry, or just type it.")
        return ""
    print(f"        heard: \"{heard}\"")
    return heard


def simulate(phone: str, speak: bool = False, listen: bool = False) -> int:
    """
    Run the voice agent's logic in the terminal, no Uplift/mic required.

    With --speak the replies are also spoken aloud through the operating
    system's own synthesiser, so you can hear the agent without paying a voice
    provider. You still type the caller's side: local speech-to-text is a
    separate dependency, and it's the half a real call provider actually sells.

    This uses the voice system prompt and the voice channel tag, so bookings
    land in the store marked `channel='voice'` — useful for demoing the shared
    memory layer across both channels without provisioning telephony.
    """
    store.init_db()
    llm.get_client()  # fail fast on a missing LLM key, before the banner
    engine = ConversationEngine(phone=phone, channel="voice")

    speaker = speech.Speaker(enabled=speak)
    listener = speech.Listener(enabled=listen)

    if speak and not speaker.available:
        print(
            "  (no text-to-speech backend found — the agent will stay silent.\n"
            "   On Windows this should not happen; on Linux try: apt install espeak-ng)",
            file=sys.stderr,
        )
    if listen and not listener.enabled:
        print(
            "  (speech recognition needs Windows — you can still type your replies)",
            file=sys.stderr,
        )

    parts = []
    if speak and speaker.available:
        parts.append("speaking")
    if listen:
        parts.append("listening")
    mode = " + ".join(parts) if parts else "text only"
    print(f"\n--- DualBook voice agent ({mode}) | {memory.normalize_phone(phone)} ---")
    print(f"LLM: {llm.describe_provider()}")
    if speak and speaker.available:
        print(f"Voice out: {speaker.describe()}")
    if listen and listener.enabled:
        print(f"Voice in : {listener.describe()}")
    print(
        "Returning customer: "
        + ("YES - profile pre-loaded" if engine.profile else "no - new number")
    )
    print("Type what the caller says. Ctrl-C or 'quit' to exit.\n")

    # A network blip shouldn't dump a 60-line traceback at someone who is just
    # trying to talk to the agent — say what happened and stay usable.
    try:
        greeting = engine.greet()
        print(f"agent> {greeting}\n")
        speaker.speak(greeting)
    except llm.LLMError as exc:
        print(f"\n[agent unavailable]\n{exc}\n", file=sys.stderr)
        return 1

    while True:
        try:
            user = _next_caller_turn(listener)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user is None:
            break
        if not user:
            continue
        if user.lower() in {"quit", "exit"}:
            break

        try:
            reply = engine.handle(user)
            print(f"\nagent > {reply}\n")
            speaker.speak(reply)
        except llm.LLMError as exc:
            # Keep the transcript: the customer can just say it again.
            print(f"\n[agent unavailable] {exc}\n"
                  "Your conversation is intact — type your message again.\n",
                  file=sys.stderr)
            continue

        if engine.booking_saved:
            print("(booking saved and customer profile updated - call complete)")
            print("  See it: python view_bookings.py   or   python run_dashboard.py")
            break

    speaker.close()
    listener.close()
    return 0


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="DualBook voice booking agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("provision", help="create or update the Uplift assistant")

    p_session = sub.add_parser("session", help="mint a session for a caller")
    p_session.add_argument("phone", help="caller phone number, e.g. +923001234567")
    p_session.add_argument(
        "--join",
        action="store_true",
        help="join the room and host the save_booking RPC handler",
    )
    p_session.add_argument(
        "--room",
        default=None,
        metavar="NAME",
        help="join an existing room instead of creating one - use with --join to "
        "host the booking tool alongside a separate speaking client",
    )
    p_session.add_argument(
        "--persisted",
        action="store_true",
        help="use the persisted assistant instead of an adhoc, memory-injected "
        "one (cannot greet a returning caller by name)",
    )

    p_serve = sub.add_parser("serve", help="run the backend token endpoint")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8001)

    p_sim = sub.add_parser("simulate", help="run the voice agent in your terminal")
    p_sim.add_argument("phone", help="caller phone number")
    p_sim.add_argument(
        "--voice",
        action="store_true",
        help="full voice mode: agent speaks, you reply by talking (free, offline)",
    )
    p_sim.add_argument(
        "--speak",
        action="store_true",
        help="speak the agent's replies aloud, but you type your replies",
    )
    p_sim.add_argument(
        "--listen",
        action="store_true",
        help="reply by talking into your microphone (Windows, offline)",
    )

    args = parser.parse_args()

    try:
        if args.command == "provision":
            return provision()
        if args.command == "session":
            return session_command(args.phone, args.join, args.persisted, args.room)
        if args.command == "serve":
            return serve(args.host, args.port)
        if args.command == "simulate":
            # --voice is the friendly name for "both halves".
            return simulate(
                args.phone,
                speak=args.speak or args.voice,
                listen=args.listen or args.voice,
            )
    except config.ConfigError as exc:
        print(f"\nConfiguration error: {exc}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
