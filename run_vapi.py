#!/usr/bin/env python
"""
DualBook — Vapi agent (voice calls + text chat).

    python run_vapi.py provision              # create/update the Vapi assistant
    python run_vapi.py serve --port 8002      # webhook that EXECUTES save_booking
    python run_vapi.py chat +923001234567     # text conversation via Vapi Chat API
    python run_vapi.py call +923001234567     # place a real outbound voice call
    python run_vapi.py doctor                 # check keys and config

HOW THE PIECES FIT
------------------
Vapi runs the conversation (STT -> LLM -> TTS) on its side. When the caller has
given every detail, the assistant calls `save_booking`, and Vapi POSTs that tool
call to OUR server (`serve`). The handler calls booking_core.complete_booking() —
the same function the WhatsApp agent calls — so there is exactly one booking
path and one memory write for all channels.

    caller ──speech──► Vapi ──tool call──► run_vapi.py serve
                                                  │
                                     booking_core.complete_booking()
                                        ├─► bookings table
                                        └─► customer memory profile

`serve` must be reachable from the internet, so in development run ngrok and put
that URL in VAPI_SERVER_URL before provisioning.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import requests

# NOTE: fastapi is imported at MODULE level, not inside build_app(). With
# `from __future__ import annotations` every annotation becomes a string, and
# FastAPI resolves them against module globals — a Request imported inside the
# function is invisible there, so FastAPI silently treats it as a query
# parameter and every webhook POST fails with "field required".
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dualbook import booking_core, config, memory, store, vapi_client
from dualbook.vapi_client import VapiClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("dualbook.vapi")


# ---------------------------------------------------------------------------
# provision
# ---------------------------------------------------------------------------


def provision(update: bool = True) -> int:
    """Create (or update) the Vapi assistant from booking_core's prompt+tools."""
    store.init_db()
    client = VapiClient()

    if not config.VAPI_SERVER_URL:
        print(
            "\n  WARNING: VAPI_SERVER_URL is not set.\n"
            "  Without it Vapi has nowhere to send the save_booking tool call,\n"
            "  so conversations will sound fine but NOTHING will be saved.\n"
            "  Start `python run_vapi.py serve`, expose it with ngrok, then set\n"
            "  VAPI_SERVER_URL=https://<id>.ngrok.io/vapi/webhook in .env.\n",
            file=sys.stderr,
        )

    payload = client.build_assistant_payload(
        # No profile: a persisted assistant is shared by every caller, so its
        # baked-in instructions must stay generic. Per-caller memory is injected
        # at call time via assistantOverrides (see place_call).
        instructions=booking_core.build_system_prompt(None, channel="voice"),
        tools=[booking_core.llm_tool()],
        first_message=f"Hi! Thanks for calling {config.BUSINESS_NAME}. "
        "I can book your car wash — may I take your name?",
    )

    if config.VAPI_ASSISTANT_ID and update:
        result = client.update_assistant(config.VAPI_ASSISTANT_ID, payload)
        print(f"\nUpdated Vapi assistant {config.VAPI_ASSISTANT_ID}")
    else:
        result = client.create_assistant(payload)
        assistant_id = result.get("id")
        print(f"\nCreated Vapi assistant: {assistant_id}")
        print("\n  Add this to your .env:")
        print(f"    VAPI_ASSISTANT_ID={assistant_id}\n")

    print(f"  model      {payload['model']['provider']}/{payload['model']['model']}")
    print(f"  voice      {payload['voice']['provider']}/{payload['voice']['voiceId']}")
    print(f"  tools      {[t['function']['name'] for t in payload['model']['tools']]}")
    print(f"  serverUrl  {config.VAPI_SERVER_URL or '(not set — bookings will NOT save)'}")
    return 0


# ---------------------------------------------------------------------------
# serve — the webhook Vapi calls to execute tools
# ---------------------------------------------------------------------------


def build_app():
    app = FastAPI(title="DualBook Vapi webhook", version="1.0.0")

    @app.on_event("startup")
    def _startup() -> None:
        store.init_db()
        log.info("DB ready at %s", config.DB_PATH)
        if not config.VAPI_SERVER_SECRET:
            log.warning(
                "VAPI_SERVER_SECRET is not set — this endpoint will accept tool "
                "calls from anyone who finds the URL."
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "channel": "vapi"}

    @app.post("/vapi/webhook")
    async def webhook(request: Request) -> JSONResponse:
        if not vapi_client.verify_secret(request.headers):
            log.warning("Rejected Vapi webhook: bad x-vapi-secret")
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        payload = await request.json()
        message_type = (payload.get("message") or {}).get("type")
        tool_calls = vapi_client.parse_tool_calls(payload)

        if not tool_calls:
            # Vapi sends many event types here (status-update, end-of-call-report,
            # transcript...). Acknowledge them so it doesn't retry.
            log.debug("Vapi event ignored: %s", message_type)
            return JSONResponse({"received": True})

        phone = vapi_client.caller_phone(payload) or ""
        results: list[tuple[str, str]] = []

        for call in tool_calls:
            log.info("Vapi tool call %s: %s", call.name, call.arguments)
            results.append((call.id, _execute(call, phone)))

        return JSONResponse(vapi_client.tool_results_response(results))

    return app


def _execute(call: vapi_client.VapiToolCall, phone: str) -> str:
    """Run one tool call. Returns the string Vapi reads back to the caller."""
    if call.name != booking_core.BOOKING_TOOL_NAME:
        return f"Unknown tool {call.name!r}."

    args = dict(call.arguments or {})
    # Fall back to the number Vapi reports for the call if the model didn't
    # capture contact details explicitly.
    if phone and not str(args.get("contact_details") or "").strip():
        args["contact_details"] = phone

    missing = booking_core.missing_slots(args)
    if missing:
        # Refuse rather than persist a half-filled booking; the wording tells
        # the assistant how to recover mid-conversation.
        return (
            "Booking NOT saved — still missing: "
            f"{', '.join(missing)}. Please ask the customer for these, then call "
            "save_booking again."
        )

    try:
        booking, booking_id = booking_core.complete_booking(
            phone=phone or args.get("contact_details") or "unknown",
            arguments=args,
            channel="voice",
        )
    except Exception as exc:  # returned to the model, not raised at the caller
        log.exception("Vapi booking failed")
        return f"Failed to save booking: {exc}"

    return (
        f"Booking #{booking_id} confirmed for {booking.customer_name} "
        f"({booking.vehicle_type}) on {booking.preferred_date} at "
        f"{booking.preferred_time}. Read this back to the customer."
    )


def serve(host: str, port: int) -> int:
    import uvicorn

    print(f"\n  Vapi webhook -> http://{host}:{port}/vapi/webhook")
    print("  Expose it:      ngrok http", port)
    print("  Then set:       VAPI_SERVER_URL=https://<id>.ngrok.io/vapi/webhook")
    print("  And re-run:     python run_vapi.py provision\n")
    uvicorn.run(build_app(), host=host, port=port)
    return 0


# ---------------------------------------------------------------------------
# chat — text conversation (this is the WhatsApp brain)
# ---------------------------------------------------------------------------


def chat_session(phone: str) -> int:
    """
    Interactive text chat through Vapi's Chat API.

    Vapi threads context server-side via previousChatId, so each turn sends only
    the new message. Memory is still ours: the caller's profile is injected into
    the per-request assistant config, exactly like the voice path.
    """
    store.init_db()
    client = VapiClient()
    normalised = memory.normalize_phone(phone)
    profile = memory.load_profile(normalised)

    # Inline assistant so this caller's memory rides along on every turn. A
    # persisted assistantId would share one prompt across all customers.
    assistant = client.build_assistant_payload(
        instructions=booking_core.build_system_prompt(profile, channel="whatsapp"),
        tools=[booking_core.llm_tool()],
        first_message="Hi! I can book your car wash — may I take your name?",
    )

    print(f"\n--- DualBook via Vapi Chat | {normalised} ---")
    print(
        "Customer: "
        + (
            f"RETURNING — {profile.get('customer_name')}, "
            f"{profile.get('booking_count')} prior bookings (memory pre-loaded)"
            if profile
            else "new number"
        )
    )
    print("Type your messages. Ctrl-C or 'quit' to exit.\n")

    previous_chat_id: str | None = None
    while True:
        try:
            text = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text or text.lower() in {"quit", "exit"}:
            break

        try:
            if previous_chat_id:
                # Thread continues server-side; no need to resend the config.
                response = client.chat(message=text, previous_chat_id=previous_chat_id)
            else:
                # First turn carries the assistant config with this caller's memory.
                response = client.chat(message=text, assistant=assistant)
        except Exception as exc:
            print(f"agent> [error] {exc}\n")
            continue

        previous_chat_id = response.get("id") or previous_chat_id
        print(f"agent> {client.extract_reply(response) or '(no reply)'}\n")

    return 0


# ---------------------------------------------------------------------------
# call — outbound voice
# ---------------------------------------------------------------------------


def place_call(phone: str) -> int:
    store.init_db()
    client = VapiClient()
    normalised = memory.normalize_phone(phone)
    profile = memory.load_profile(normalised)

    overrides = None
    if profile:
        # A persisted assistant has ONE fixed instruction string, so this is how
        # a returning caller still gets greeted by name — the profile rides in
        # as a per-call override. Same pre-loaded-context idea as everywhere
        # else, just expressed in Vapi's vocabulary.
        overrides = {
            "model": {
                "provider": config.VAPI_LLM_PROVIDER,
                "model": config.VAPI_LLM_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": booking_core.build_system_prompt(
                            profile, channel="voice"
                        ),
                    }
                ],
            },
            "firstMessage": (
                f"Welcome back {profile.get('customer_name')}! "
                f"Same {profile.get('usual_service')} for the "
                f"{profile.get('vehicle_type')}?"
            ),
        }

    result = client.create_call(customer_number=normalised, assistant_overrides=overrides)
    print(f"\nCall queued: {result.get('id')}")
    print(f"  to        {normalised}")
    print(f"  memory    {'pre-loaded — ' + str(profile.get('customer_name')) if profile else 'new caller'}")
    print(f"  status    {result.get('status')}\n")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def preflight() -> int:
    """
    Prove the whole loop works BEFORE spending a cent of Vapi credit.

    The expensive failure mode is provisioning against a webhook URL that isn't
    actually reachable — the call sounds perfect, the model calls save_booking,
    Vapi can't reach you, and the booking silently vanishes. You only find out
    after the minutes are billed.

    So this POSTs a DELIBERATELY INCOMPLETE tool call to the public
    VAPI_SERVER_URL. A correct setup replies "Booking NOT saved - still
    missing: ..." which proves the URL is reachable, the secret matches, and
    our handler is executing — while writing nothing to the database.
    """
    url = config.VAPI_SERVER_URL
    print("\n  Vapi preflight\n")

    if not url:
        print("  [FAIL] VAPI_SERVER_URL is not set.")
        print("         Nothing would ever be saved. Start the webhook and expose it:")
        print("           python run_vapi.py serve --port 8002")
        print("           ngrok http 8002")
        print("         then set VAPI_SERVER_URL and re-run `provision`.\n")
        return 1

    if not url.startswith("https://"):
        print(f"  [FAIL] VAPI_SERVER_URL must be https, got: {url}\n")
        return 1

    print(f"  Testing {url}")
    headers = {"Content-Type": "application/json"}
    if config.VAPI_SERVER_SECRET:
        headers["x-vapi-secret"] = config.VAPI_SERVER_SECRET

    probe = {
        "message": {
            "type": "tool-calls",
            "call": {"customer": {"number": "+000000000000"}},
            "toolCalls": [
                {
                    "id": "preflight",
                    # Missing everything on purpose: exercises the handler
                    # without creating a booking.
                    "function": {"name": "save_booking", "arguments": "{}"},
                }
            ],
        }
    }

    try:
        response = requests.post(url, headers=headers, json=probe, timeout=25)
    except requests.RequestException as exc:
        print(f"  [FAIL] Could not reach it: {exc}")
        print("         Is `run_vapi.py serve` running, and is ngrok still up?")
        print("         (ngrok gives a NEW url each restart — re-run provision.)\n")
        return 1

    if response.status_code == 401:
        print("  [FAIL] 401 — VAPI_SERVER_SECRET here doesn't match the one the")
        print("         assistant was provisioned with. Re-run `provision`.\n")
        return 1
    if response.status_code != 200:
        print(f"  [FAIL] HTTP {response.status_code}: {response.text[:200]}\n")
        return 1

    try:
        result = (response.json().get("results") or [{}])[0].get("result", "")
    except ValueError:
        print(f"  [FAIL] Response wasn't JSON: {response.text[:200]}\n")
        return 1

    if "NOT saved" not in result:
        print(f"  [WARN] Reached it, but got an unexpected reply: {result[:160]}\n")
        return 1

    print("  [OK]   Webhook reachable, secret accepted, handler executing.")
    print("  [OK]   Nothing was written to the database (probe was incomplete).")

    bookings_before = len(store.list_bookings())
    print(f"  [OK]   Bookings in DB: {bookings_before} (unchanged)")
    print("\n  You're safe to spend credit. Cheapest way to test real audio:")
    print("    https://dashboard.vapi.ai -> Assistants -> your assistant -> Talk")
    print("    (browser mic, no phone number needed, ~$0.05/min)\n")
    return 0


def doctor() -> int:
    """Tell the user exactly what is and isn't configured, and what it unlocks."""
    checks = [
        ("VAPI_API_KEY", config.VAPI_API_KEY, "required for everything Vapi",
         "https://dashboard.vapi.ai -> Organization -> API Keys (PRIVATE key)"),
        ("VAPI_ASSISTANT_ID", config.VAPI_ASSISTANT_ID, "required for `call`",
         "run: python run_vapi.py provision"),
        ("VAPI_SERVER_URL", config.VAPI_SERVER_URL,
         "required or NOTHING SAVES", "ngrok http 8002 -> https://<id>.ngrok.io/vapi/webhook"),
        ("VAPI_SERVER_SECRET", config.VAPI_SERVER_SECRET,
         "recommended — blocks forged bookings", "invent any random string"),
        ("VAPI_PHONE_NUMBER_ID", config.VAPI_PHONE_NUMBER_ID, "required for real calls",
         "https://dashboard.vapi.ai -> Phone Numbers"),
        ("GROQ_API_KEY", config.get_env("GROQ_API_KEY"),
         "makes Vapi model usage $0 (bring your own key)",
         "https://console.groq.com/keys — free"),
    ]

    print(f"\n  Vapi configuration ({config.VAPI_API_URL})\n")
    missing_required = 0
    for name, value, why, how in checks:
        ok = bool(value)
        mark = "OK  " if ok else "MISS"
        shown = (str(value)[:12] + "...") if ok else "-"
        print(f"  [{mark}] {name:<22} {shown:<18} {why}")
        if not ok:
            print(f"         -> {how}")
            if "required" in why:
                missing_required += 1

    print(f"\n  WhatsApp transport: {config.WHATSAPP_PROVIDER}")
    if config.WHATSAPP_PROVIDER == "meta":
        for name, value in (
            ("META_WA_TOKEN", config.META_WA_TOKEN),
            ("META_WA_PHONE_NUMBER_ID", config.META_WA_PHONE_NUMBER_ID),
            ("META_WA_VERIFY_TOKEN", config.META_WA_VERIFY_TOKEN),
            ("META_WA_APP_SECRET", config.META_WA_APP_SECRET),
        ):
            print(f"  [{'OK  ' if value else 'MISS'}] {name}")
        if not config.META_WA_APP_SECRET:
            print("         -> webhook signatures NOT checked: anyone who finds")
            print("            your URL can POST fake bookings. App settings ->")
            print("            Basic -> App secret.")

        # Templates: the difference between confirming a voice booking and
        # silently not confirming it.
        from dualbook import wa_templates

        declared = wa_templates.describe()
        by_kind = {t["kind"]: t for t in declared}
        confirm = by_kind.get("booking_confirmation")
        print(f"  [{'OK  ' if confirm else 'MISS'}] template: booking_confirmation")
        if confirm:
            print(f"         -> {confirm['name']} ({confirm['language']}), "
                  f"{len(confirm['variables'])} variables")
            print("         -> must be APPROVED in Meta, not just declared here")
        else:
            print("         -> without it, confirmations for PHONE bookings are")
            print("            blocked: the caller never messaged us, so free")
            print("            text is not allowed. See SETUP.md.")
    else:
        print(f"  [{'OK  ' if config.WHAPI_TOKEN else 'MISS'}] WHAPI_TOKEN")
        if not config.WHAPI_WEBHOOK_SECRET:
            print("  [MISS] WHAPI_WEBHOOK_SECRET")
            print("         -> webhook not authenticated; add it as an")
            print("            X-Webhook-Secret header on the Whapi webhook.")

    print()
    return 1 if missing_required else 0


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="DualBook Vapi agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("provision", help="create or update the Vapi assistant")

    p_serve = sub.add_parser("serve", help="webhook that executes save_booking")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8002)

    p_chat = sub.add_parser("chat", help="text conversation via the Vapi Chat API")
    p_chat.add_argument("phone")

    p_call = sub.add_parser("call", help="place an outbound voice call")
    p_call.add_argument("phone")

    sub.add_parser("doctor", help="check which keys are set and what they unlock")
    sub.add_parser(
        "preflight",
        help="verify the webhook loop works BEFORE spending Vapi credit",
    )

    args = parser.parse_args()

    try:
        if args.command == "provision":
            return provision()
        if args.command == "preflight":
            return preflight()
        if args.command == "serve":
            return serve(args.host, args.port)
        if args.command == "chat":
            return chat_session(args.phone)
        if args.command == "call":
            return place_call(args.phone)
        if args.command == "doctor":
            return doctor()
    except config.ConfigError as exc:
        print(f"\nConfiguration error: {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
