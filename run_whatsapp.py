#!/usr/bin/env python
"""
DualBook — WhatsApp booking agent (Component 1).

    python run_whatsapp.py serve                  # FastAPI webhook server
    python run_whatsapp.py simulate +923001234567 # same agent, in your terminal

`simulate` runs the identical ConversationEngine with no WhatsApp credentials
required, so the booking + memory behaviour can be demoed end to end with only
an Anthropic key.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from collections import OrderedDict

# Imported at MODULE level on purpose: with `from __future__ import annotations`
# FastAPI resolves handler annotations against module globals, so a Request
# imported inside build_app() would be invisible and every webhook POST would
# fail with "field required".
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from dualbook import booking_core, config, llm, memory, store, vapi_client, whatsapp_client
from dualbook.booking_core import ConversationEngine
from dualbook.whatsapp_client import WhatsAppClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("dualbook.whatsapp")


# ---------------------------------------------------------------------------
# The brain: local, or Vapi
# ---------------------------------------------------------------------------
# WhatsApp is a TRANSPORT. What thinks behind it is a separate choice:
#
#   local  ConversationEngine — the model called directly through llm.py.
#          Default, free, and the only one that runs with no paid account.
#   vapi   Vapi's Chat API. Vapi has NO WhatsApp channel of its own, so this is
#          the bridge: Meta carries the message in, Vapi does the thinking, Meta
#          carries the reply back out.
#
# Both drive the SAME booking: the prompt and the save_booking contract come
# from booking_core either way, so the two brains ask for the same things and
# write the same rows.
_BRAINS = ("local", "vapi")
_BRAIN = "local"


class VapiBrain:
    """
    Vapi's Chat API wearing ConversationEngine's interface.

    Deliberately duck-typed rather than subclassed: `_handle_message` only ever
    calls `.handle()` and reads `.booking_saved`, so the transport genuinely
    cannot tell the two brains apart.

    WHERE THE BOOKING IS WRITTEN — the part that surprises people. Vapi does not
    hand tool calls back to us; it EXECUTES them by POSTing to VAPI_SERVER_URL.
    So `save_booking` never passes through this process: it lands on the webhook
    in `run_vapi.py serve`, which calls booking_core.complete_booking. With no
    VAPI_SERVER_URL configured the conversation sounds perfect and nothing is
    ever saved, which is why that is checked up front rather than discovered.
    """

    def __init__(self, phone: str, db_path: str | None = None) -> None:
        self.phone = memory.normalize_phone(phone)
        self.db_path = db_path
        self.channel = "whatsapp"
        self.booking_saved = False
        self.turns = 0
        self._chat_id: str | None = None
        self._client = vapi_client.VapiClient()
        # PRE-LOAD READ, exactly as the local brain does it: one indexed lookup
        # at session start, folded into the prompt Vapi is given.
        self.profile = memory.load_profile(self.phone, db_path=db_path)
        # Bookings already on file for this number, so a new row appearing
        # during the conversation is how we learn Vapi's webhook saved one.
        self._bookings_at_start = len(
            store.bookings_for_phone(self.phone, db_path=db_path)
        )

    def _assistant(self) -> dict:
        """
        An INLINE assistant carrying this customer's memory.

        A persisted assistant holds one instruction string shared by every
        customer, so it structurally cannot say "welcome back, Ali". Same
        reasoning as the Uplift adhoc session in run_voice.py.
        """
        prompt = booking_core.build_system_prompt(
            self.profile, channel="whatsapp", caller_phone=self.phone or None
        )
        return self._client.assistant_payload(
            prompt=prompt,
            tools=[booking_core.llm_tool()],
            first_message="",
        )

    def _say(self, text: str) -> str:
        # First turn sends the assistant config; later turns just thread onto
        # the previous chat id, so the prompt isn't re-uploaded every message.
        if self._chat_id:
            response = self._client.chat(text, previous_chat_id=self._chat_id)
        else:
            response = self._client.chat(text, assistant=self._assistant())
        self._chat_id = response.get("id") or self._chat_id

        # Did Vapi's webhook write a booking while we were waiting?
        if len(store.bookings_for_phone(self.phone, db_path=self.db_path)) > self._bookings_at_start:
            self.booking_saved = True

        return self._client.extract_reply(response) or "Sorry, could you say that again?"

    def greet(self) -> str:
        return self._say("<the customer has just started a conversation>")

    def handle(self, user_message: str) -> str:
        self.turns += 1
        return self._say(user_message)


def build_engine(phone: str, db_path: str | None = None):
    """Whichever brain this process was started with."""
    if _BRAIN == "vapi":
        return VapiBrain(phone, db_path=db_path)
    return ConversationEngine(phone=phone, channel="whatsapp", db_path=db_path)


def check_brain() -> None:
    """Fail loudly at startup rather than silently losing bookings."""
    if _BRAIN != "vapi":
        return
    if not config.VAPI_API_KEY:
        raise config.ConfigError(
            "--brain vapi needs VAPI_API_KEY.\n"
            "  https://dashboard.vapi.ai -> Organization -> API Keys (PRIVATE key)"
        )
    if not config.VAPI_SERVER_URL:
        raise config.ConfigError(
            "--brain vapi needs VAPI_SERVER_URL, or NOTHING WILL BE SAVED.\n"
            "  Vapi executes save_booking by POSTing to your server, so without\n"
            "  it the agent will chat happily and never record a booking.\n"
            "    python run_vapi.py serve --port 8002\n"
            "    ngrok http 8002\n"
            "    VAPI_SERVER_URL=https://<your-id>.ngrok.io/vapi/webhook"
        )


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------
# One ConversationEngine per phone number, held in memory. Each engine does its
# memory pre-load exactly once, when it is created — which is precisely the
# "read at session start" half of the memory design.
#
# In-process dict is right for a demo and wrong for production (it dies with
# the worker and won't survive multiple replicas). The durable half — the
# customer profile — is already in SQLite; only the in-flight transcript is
# volatile, and losing that just means the agent re-asks the current question.
#
# Everything below is touched from WORKER THREADS: message handling runs as a
# FastAPI background task so the webhook can ack immediately (see the webhook
# handler), and Starlette runs sync background functions in its threadpool. So
# the registry is guarded by a lock, and each conversation gets its own lock so
# two messages from the same customer can never interleave inside one engine.
_SESSIONS: dict[str, ConversationEngine] = {}
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_SEEN: dict[str, float] = {}
_REGISTRY_LOCK = threading.Lock()
_IDLE_RESET_AFTER_BOOKING = True

# An abandoned conversation ("hi", then nothing) would otherwise sit in the
# registry until the process restarts. Half an hour is far longer than any real
# booking takes and short enough that a customer returning later gets a fresh
# start — which re-reads their profile, so nothing is lost by expiring them.
SESSION_IDLE_TIMEOUT = 30 * 60


def _prune_sessions(now: float) -> None:
    """Drop conversations nobody has touched in a while. Caller holds the lock."""
    stale = [k for k, seen in _SESSION_SEEN.items() if now - seen > SESSION_IDLE_TIMEOUT]
    for key in stale:
        _SESSION_SEEN.pop(key, None)
        _SESSION_LOCKS.pop(key, None)
        if _SESSIONS.pop(key, None) is not None:
            log.info("Expired idle session for %s", key)


def session_lock(phone: str) -> threading.Lock:
    """The lock that serialises turns for one customer.

    Held for the whole turn — model call, tool execution and outbound send —
    because a ConversationEngine owns a mutable transcript. Two threads
    appending to `engine.messages` at once produce a conversation that never
    happened, and on the turn that saves, two bookings.
    """
    key = memory.normalize_phone(phone)
    with _REGISTRY_LOCK:
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = _SESSION_LOCKS[key] = threading.Lock()
        return lock


def get_session(phone: str) -> ConversationEngine:
    key = memory.normalize_phone(phone)
    now = time.monotonic()
    with _REGISTRY_LOCK:
        _prune_sessions(now)
        _SESSION_SEEN[key] = now
        engine = _SESSIONS.get(key)
        if engine is not None:
            return engine

    # Built OUTSIDE the registry lock: the constructor does a SQLite read plus
    # the memory pre-load, and holding a global lock across that would put every
    # other customer in the queue behind it. Two threads racing here is harmless
    # — they are the same customer by definition, so session_lock has already
    # serialised them, and setdefault means the loser's engine is discarded
    # rather than replacing a transcript someone is mid-conversation in.
    engine = build_engine(key)
    with _REGISTRY_LOCK:
        engine = _SESSIONS.setdefault(key, engine)
    log.info(
        "New session for %s (brain: %s, returning customer: %s)",
        key,
        _BRAIN,
        "yes" if engine.profile else "no",
    )
    return engine


def end_session(phone: str) -> None:
    """Drop the transcript once a booking completes so the next message starts
    a fresh conversation — which re-reads the profile we just updated.

    The per-phone lock stays behind deliberately: the thread calling this is
    holding it. `_prune_sessions` reaps it once the number goes quiet.
    """
    with _REGISTRY_LOCK:
        _SESSIONS.pop(memory.normalize_phone(phone), None)


# ---------------------------------------------------------------------------
# Delivery de-duplication
# ---------------------------------------------------------------------------
# Meta (and Whapi) retry a webhook delivery they believe failed — including one
# that simply took too long to ack. Without this, a retry replays the customer's
# message through the engine: they get answered twice, and a retry landing on a
# confirming turn ("yes, that's right") saves the booking twice.
#
# Bounded, so a long-running server cannot grow this without limit. 2,000 ids is
# far more than any retry window needs, and evicting the oldest can only ever
# un-ignore a delivery old enough that no provider is still retrying it.
_SEEN_MESSAGE_IDS: OrderedDict[str, None] = OrderedDict()
_SEEN_LIMIT = 2000


def claim_message(message_id: str | None) -> bool:
    """True if this delivery is new and we should handle it.

    Claiming happens in the webhook handler, BEFORE the work is queued, so a
    retry arriving while the first copy is still being processed is dropped too.
    A message with no id is always handled — silently dropping real messages is
    a worse failure than answering a rare duplicate.
    """
    if not message_id:
        return True
    with _REGISTRY_LOCK:
        if message_id in _SEEN_MESSAGE_IDS:
            return False
        _SEEN_MESSAGE_IDS[message_id] = None
        while len(_SEEN_MESSAGE_IDS) > _SEEN_LIMIT:
            _SEEN_MESSAGE_IDS.popitem(last=False)
    return True


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
        log.info("WhatsApp transport: %s", config.WHATSAPP_PROVIDER)
        if _BRAIN == "vapi":
            log.info("Brain: Vapi Chat API (bookings save via %s)",
                     config.VAPI_SERVER_URL)
        else:
            log.info("Brain: local — %s", llm.describe_provider())
        # Say this out loud rather than leaving it to be assumed. An endpoint
        # that cannot tell a real delivery from a forged one is a fact the
        # operator should learn at startup, not after someone finds the URL.
        if type(client).authenticated():
            log.info("Webhook authentication: ON")
        elif config.WHATSAPP_PROVIDER == "meta":
            log.warning(
                "Webhook authentication: OFF — META_WA_APP_SECRET is not set, so "
                "signatures are not checked and ANYONE who learns this URL can "
                "POST fake bookings. Fine on a private ngrok tunnel for a demo; "
                "set it before this is reachable by anyone else."
            )
        else:
            log.warning(
                "Webhook authentication: OFF — set WHAPI_WEBHOOK_SECRET and add "
                "it as an X-Webhook-Secret header on the Whapi webhook."
            )

    @app.get("/health")
    def health() -> dict[str, object]:
        with _REGISTRY_LOCK:
            sessions = len(_SESSIONS)
        return {
            "status": "ok",
            "transport": config.WHATSAPP_PROVIDER,
            "brain": _BRAIN,
            "webhook_authenticated": type(client).authenticated(),
            "active_sessions": sessions,
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
            log.warning("Meta webhook verification failed — check META_WA_VERIFY_TOKEN")
            raise HTTPException(status_code=403, detail="verification failed")
        log.info("Meta webhook verified")
        return PlainTextResponse(challenge)

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> dict[str, object]:
        # RAW bytes, and verified before anything parses them. Meta signs the
        # exact bytes it sent, so re-serialising a parsed payload to check the
        # HMAC would never match — key order and whitespace differ.
        body = await request.body()
        if not client.verify_webhook(dict(request.headers), body):
            raise HTTPException(status_code=401, detail="bad webhook signature")

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="body is not JSON")

        messages = type(client).parse_incoming(payload)

        queued = duplicates = 0
        for incoming in messages:
            # Claim first, queue second. A retry that arrives while the first
            # copy is still being handled has to be rejected here, not later.
            if not claim_message(incoming.message_id):
                duplicates += 1
                log.info(
                    "Ignoring duplicate delivery %s from %s",
                    incoming.message_id, incoming.phone,
                )
                continue
            background.add_task(_handle_message, client, incoming)
            queued += 1

        # Returning NOW is the point. Handling a message means a model call and
        # an outbound send — seconds, not milliseconds — and a provider that
        # doesn't get a prompt ack treats the delivery as failed and sends it
        # again. The work runs after this response, in Starlette's threadpool.
        return {"received": len(messages), "queued": queued, "duplicates": duplicates}

    return app


def _handle_message(client: WhatsAppClient, incoming) -> None:
    """Handle one customer message. Runs in a worker thread, after the ack.

    Nothing here may raise: there is no request left to return an error to, and
    an escaping exception would be logged by Starlette as an unhandled task
    failure with no indication of which customer it belonged to.
    """
    # One turn at a time per customer. Someone who fires off three messages in
    # a row gets three replies in order, rather than three threads racing on the
    # same transcript.
    with session_lock(incoming.phone):
        try:
            # Note the inbound BEFORE replying: this message is what opens the
            # 24-hour window in which free-text replies are legal, and the
            # booking confirmation later in this same turn reads it.
            store.record_inbound(memory.normalize_phone(incoming.phone))

            engine = get_session(incoming.phone)
            log.info("<- %s: %s", incoming.phone, incoming.text)

            reply = engine.handle(incoming.text)

            log.info("-> %s: %s", incoming.phone, reply)
            client.send_text(to=incoming.chat_id, body=reply)

            if engine.booking_saved and _IDLE_RESET_AFTER_BOOKING:
                end_session(incoming.phone)
        except Exception:
            log.exception("Failed handling message from %s", incoming.phone)


def serve(host: str, port: int) -> None:
    import uvicorn

    check_brain()
    log.info("Webhook endpoint: POST http://%s:%s/webhook", host, port)
    log.info("Point your WhatsApp provider's webhook at that URL (ngrok for local).")
    uvicorn.run(build_app(), host=host, port=port)


# ---------------------------------------------------------------------------
# Terminal simulator
# ---------------------------------------------------------------------------


def simulate(phone: str) -> int:
    """Chat with the WhatsApp agent from the terminal. No Whapi token needed."""
    store.init_db()
    # Fail fast on a missing key so the reviewer sees the fix instead of a
    # banner followed by a stack trace on the first message.
    check_brain()
    if _BRAIN == "local":
        llm.get_client()
    engine = build_engine(phone)

    print(f"\n--- DualBook WhatsApp agent (simulated) | {memory.normalize_phone(phone)} ---")
    print("Brain: " + ("Vapi Chat API" if _BRAIN == "vapi"
                       else f"local, {llm.describe_provider()}"))
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
                  "Your conversation is intact — type your message again.\n",
                  file=sys.stderr)
            continue

        if engine.booking_saved:
            print("(booking saved and customer profile updated - ending session)")
            break

    return 0


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="DualBook WhatsApp booking agent")
    sub = parser.add_subparsers(dest="command")

    def add_brain(p) -> None:
        p.add_argument(
            "--brain",
            choices=_BRAINS,
            default="local",
            help="what thinks behind WhatsApp: 'local' (default, free) or "
            "'vapi' (Vapi Chat API; needs VAPI_API_KEY + VAPI_SERVER_URL)",
        )

    p_serve = sub.add_parser("serve", help="run the webhook server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    add_brain(p_serve)

    p_sim = sub.add_parser("simulate", help="chat with the agent in the terminal")
    p_sim.add_argument("phone", help="customer phone number, e.g. +923001234567")
    add_brain(p_sim)

    args = parser.parse_args()

    global _BRAIN
    _BRAIN = getattr(args, "brain", "local")

    try:
        if args.command == "simulate":
            # Propagated, not discarded: `simulate` exits non-zero when the LLM
            # is unreachable, and a script that checks $? deserves to hear it.
            return simulate(args.phone)
        serve(getattr(args, "host", "0.0.0.0"), getattr(args, "port", 8000))
    except config.ConfigError as exc:
        print(f"\nConfiguration error: {exc}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
