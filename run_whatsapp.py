#!/usr/bin/env python
"""
DualBook â€” WhatsApp booking agent (Component 1).

    python run_whatsapp.py serve                  # FastAPI webhook server
    python run_whatsapp.py simulate +923001234567 # same agent, in your terminal

`simulate` runs the identical ConversationEngine with no WhatsApp credentials
required, so the booking + memory behaviour can be demoed end to end with only
an Anthropic key.
"""

from __future__ import annotations

import argparse
import logging
import sys

# Imported at MODULE level on purpose: with `from __future__ import annotations`
# FastAPI resolves handler annotations against module globals, so a Request
# imported inside build_app() would be invisible and every webhook POST would
# fail with "field required".
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from dualbook import config, llm, memory, store, whatsapp_client
from dualbook.booking_core import ConversationEngine
from dualbook.whatsapp_client import WhatsAppClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("dualbook.whatsapp")


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------
# One ConversationEngine per phone number, held in memory. Each engine does its
# memory pre-load exactly once, when it is created â€” which is precisely the
# "read at session start" half of the memory design.
#
# In-process dict is right for a demo and wrong for production (it dies with
# the worker and won't survive multiple replicas). The durable half â€” the
# customer profile â€” is already in SQLite; only the in-flight transcript is
# volatile, and losing that just means the agent re-asks the current question.
_SESSIONS: dict[str, ConversationEngine] = {}
_IDLE_RESET_AFTER_BOOKING = True


def get_session(phone: str) -> ConversationEngine:
    key = memory.normalize_phone(phone)
    engine = _SESSIONS.get(key)
    if engine is None:
        engine = ConversationEngine(phone=key, channel="whatsapp")
        _SESSIONS[key] = engine
        log.info(
            "New session for %s (returning customer: %s)",
            key,
            "yes" if engine.profile else "no",
        )
    return engine


def end_session(phone: str) -> None:
    """Drop the transcript once a booking completes so the next message starts
    a fresh conversation â€” which re-reads the profile we just updated."""
    _SESSIONS.pop(memory.normalize_phone(phone), None)


# ---------------------------------------------------------------------------
# FastAPI webhook server
# ---------------------------------------------------------------------------


def build_app():
    app = FastAPI(title="DualBook WhatsApp Agent", version="1.0.0")
    # Transport is chosen by WHATSAPP_PROVIDER (whapi | meta). Everything below
    # this line is provider-neutral.
    client = whatsapp_client.get_client()

    @app.on_event("startup")
    def _startup() -> None:
        store.init_db()
        log.info("DB ready at %s", config.DB_PATH)
        log.info("LLM provider: %s", llm.describe_provider())
        log.info("WhatsApp transport: %s", config.WHATSAPP_PROVIDER)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "transport": config.WHATSAPP_PROVIDER,
            "active_sessions": len(_SESSIONS),
        }

    @app.get("/webhook")
    def verify(request: Request):
        """
        Meta's one-time subscription handshake. Meta GETs this URL with
        hub.challenge and expects it echoed back as plain text; anything else
        and the webhook simply won't save. Whapi doesn't use it, so this is a
        no-op there.
        """
        if config.WHATSAPP_PROVIDER != "meta":
            return {"status": "ok"}
        challenge = whatsapp_client.MetaWhatsAppClient.verify_subscription(
            dict(request.query_params)
        )
        if challenge is None:
            log.warning("Meta webhook verification failed â€” check META_WA_VERIFY_TOKEN")
            raise HTTPException(status_code=403, detail="verification failed")
        log.info("Meta webhook verified")
        return PlainTextResponse(challenge)

    @app.post("/webhook")
    async def webhook(request: Request) -> dict[str, object]:
        if not client.verify_webhook(dict(request.headers)):
            raise HTTPException(status_code=401, detail="bad webhook secret")

        payload = await request.json()
        messages = type(client).parse_incoming(payload)

        handled = 0
        for incoming in messages:
            try:
                _handle_message(client, incoming)
                handled += 1
            except Exception:
                # Never 500 back to the provider: most vendors retry on error,
                # which would replay the same customer message repeatedly.
                log.exception("Failed handling message from %s", incoming.phone)

        return {"received": len(messages), "handled": handled}

    return app


def _handle_message(client: WhatsAppClient, incoming) -> None:
    engine = get_session(incoming.phone)
    log.info("<- %s: %s", incoming.phone, incoming.text)

    reply = engine.handle(incoming.text)

    log.info("-> %s: %s", incoming.phone, reply)
    client.send_text(to=incoming.chat_id, body=reply)

    if engine.booking_saved and _IDLE_RESET_AFTER_BOOKING:
        end_session(incoming.phone)


def serve(host: str, port: int) -> None:
    import uvicorn

    log.info("Webhook endpoint: POST http://%s:%s/webhook", host, port)
    log.info("Point your Whapi.Cloud webhook at that URL (use ngrok for local).")
    uvicorn.run(build_app(), host=host, port=port)


# ---------------------------------------------------------------------------
# Terminal simulator
# ---------------------------------------------------------------------------


def simulate(phone: str) -> None:
    """Chat with the WhatsApp agent from the terminal. No Whapi token needed."""
    store.init_db()
    # Fail fast on a missing/invalid LLM key so the reviewer sees the fix
    # instead of a banner followed by a stack trace on the first message.
    llm.get_client()
    engine = ConversationEngine(phone=phone, channel="whatsapp")

    print(f"\n--- DualBook WhatsApp agent (simulated) | {memory.normalize_phone(phone)} ---")
    print(f"LLM: {llm.describe_provider()}")
    print(
        "Returning customer: "
        + ("YES - profile pre-loaded" if engine.profile else "no - new number")
    )
    print("Type your messages. Ctrl-C or 'quit' to exit.\n")

    # Same as the voice simulator: a dropped connection is a message, not a
    # traceback, and the transcript survives so the customer can retype.
    try:
        print(f"agent> {engine.greet()}\n")
    except llm.LLMError as exc:
        print(f"\n[agent unavailable]\n{exc}\n", file=sys.stderr)
        return 1

    while True:
        try:
            user = input("you  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in {"quit", "exit"}:
            break

        try:
            print(f"\nagent> {engine.handle(user)}\n")
        except llm.LLMError as exc:
            print(f"\n[agent unavailable] {exc}\n"
                  "Your conversation is intact â€” type your message again.\n",
                  file=sys.stderr)
            continue

        if engine.booking_saved:
            print("(booking saved and customer profile updated - ending session)")
            break


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="DualBook WhatsApp booking agent")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the webhook server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)

    p_sim = sub.add_parser("simulate", help="chat with the agent in the terminal")
    p_sim.add_argument("phone", help="customer phone number, e.g. +923001234567")

    args = parser.parse_args()

    try:
        if args.command == "simulate":
            simulate(args.phone)
        else:
            serve(getattr(args, "host", "0.0.0.0"), getattr(args, "port", 8000))
    except config.ConfigError as exc:
        print(f"\nConfiguration error: {exc}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
