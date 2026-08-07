"""
Vapi provider — voice calls and text chat.

WHAT VAPI IS (and isn't)
------------------------
Vapi is a voice-AI platform. Verified against its own docs, its channels are:

    * voice calls   (phone + web)
    * Chat API      (text, POST /chat)
    * SMS           (US only)

There is **no native WhatsApp channel**. Every "Vapi + WhatsApp" offering is a
third-party bridge (Make, Pabbly, n8n) that shuttles messages between the Meta
WhatsApp Cloud API and Vapi. So WhatsApp here means: WhatsApp transport in
(`whatsapp_client.py`) -> Vapi Chat API as the brain -> WhatsApp transport out.
`chat()` below is that brain.

COST
----
Vapi bills $0.05/min for voice and $0.005/msg for chat. Model/STT/TTS are billed
"at cost, $0 if you bring your own API key" — which is why the assistant payload
sends your own (free) Groq key through. That makes model tokens free; Vapi's own
platform fee still applies.

Everything provider-specific lives in this one file, so swapping Vapi out means
touching nothing else — the same isolation `whatsapp_client.py` and
`uplift_client.py` have.
"""

from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)


class VapiError(RuntimeError):
    pass


@dataclass
class VapiToolCall:
    """One tool invocation Vapi is asking us to execute."""

    id: str
    name: str
    arguments: dict[str, Any]


class VapiClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or config.VAPI_API_KEY
        if not self.api_key:
            raise config.ConfigError(
                "VAPI_API_KEY is not set.\n"
                "  Get it: https://dashboard.vapi.ai -> Organization -> API Keys\n"
                "  Use the PRIVATE key (server-side). Then put VAPI_API_KEY=... in .env"
            )
        self.base_url = (base_url or config.VAPI_API_URL).rstrip("/")

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        response = requests.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if response.status_code >= 400:
            log.error("Vapi %s %s -> %s: %s", method, path, response.status_code, response.text)
            raise VapiError(
                f"Vapi {method} {path} returned {response.status_code}: {response.text[:400]}"
            )
        return response.json() if response.content else {}

    # -- Assistants ---------------------------------------------------------

    def build_assistant_payload(
        self,
        instructions: str,
        tools: list[dict[str, Any]],
        first_message: str,
        server_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Translate our provider-neutral prompt + tool definitions into Vapi's
        assistant schema. booking_core stays the single source of truth for
        both, so the Vapi agent enforces exactly the same slots as WhatsApp.
        """
        model: dict[str, Any] = {
            "provider": config.VAPI_LLM_PROVIDER,
            "model": config.VAPI_LLM_MODEL,
            "messages": [{"role": "system", "content": instructions}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    },
                    # Vapi executes the tool by POSTing to this server. Without
                    # it the model can "call" save_booking and nothing persists.
                    **(
                        {"server": self._server_block(server_url)}
                        if (server_url or config.VAPI_SERVER_URL)
                        else {}
                    ),
                }
                for tool in tools
            ],
        }

        # Bring-your-own-key: makes model usage $0 on the Vapi bill.
        byo_key = config.get_env(
            {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY",
             "anthropic": "ANTHROPIC_API_KEY"}.get(config.VAPI_LLM_PROVIDER, "")
            or "GROQ_API_KEY"
        )
        if byo_key:
            log.info(
                "Using your own %s key for the Vapi model (model tokens billed at $0)",
                config.VAPI_LLM_PROVIDER,
            )

        payload: dict[str, Any] = {
            "name": f"{config.BUSINESS_NAME} — Booking Agent",
            "model": model,
            "voice": {
                "provider": config.VAPI_VOICE_PROVIDER,
                "voiceId": config.VAPI_VOICE_ID,
            },
            "transcriber": {
                "provider": config.VAPI_TRANSCRIBER_PROVIDER,
                "model": config.VAPI_TRANSCRIBER_MODEL,
                "language": config.VAPI_TRANSCRIBER_LANGUAGE,
            },
            "firstMessage": first_message,
        }
        if server_url or config.VAPI_SERVER_URL:
            payload["server"] = self._server_block(server_url)
        return payload

    @staticmethod
    def _server_block(server_url: str | None = None) -> dict[str, Any]:
        block: dict[str, Any] = {"url": server_url or config.VAPI_SERVER_URL}
        if config.VAPI_SERVER_SECRET:
            # Vapi echoes this back as the x-vapi-secret header so the webhook
            # can reject requests that didn't come from Vapi.
            block["secret"] = config.VAPI_SERVER_SECRET
        return block

    def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/assistant", payload)

    def update_assistant(self, assistant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/assistant/{assistant_id}", payload)

    def get_assistant(self, assistant_id: str) -> dict[str, Any]:
        return self._request("GET", f"/assistant/{assistant_id}")

    def list_assistants(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/assistant")
        return result if isinstance(result, list) else result.get("results", [])

    # -- Chat (the text brain, incl. the WhatsApp bridge) -------------------

    def chat(
        self,
        message: str,
        assistant_id: str | None = None,
        assistant: dict[str, Any] | None = None,
        previous_chat_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        One text turn through Vapi's Chat API.

        Multi-turn context is Vapi's to keep: pass `previous_chat_id` (or a
        `session_id`) and it threads the conversation server-side, so we don't
        resend history. Note the two are mutually exclusive in Vapi's schema.
        """
        payload: dict[str, Any] = {"input": message}
        if assistant_id:
            payload["assistantId"] = assistant_id
        elif assistant:
            payload["assistant"] = assistant

        if session_id and previous_chat_id:
            raise VapiError("Pass session_id OR previous_chat_id, not both")
        if session_id:
            payload["sessionId"] = session_id
        elif previous_chat_id:
            payload["previousChatId"] = previous_chat_id

        # Vapi requires at least one of these four; continuing an existing
        # thread with previousChatId alone is valid and is the cheap path,
        # since it doesn't resend the assistant config on every turn.
        if not any(
            k in payload for k in ("assistantId", "assistant", "sessionId", "previousChatId")
        ):
            raise VapiError(
                "chat() needs one of: assistant_id, assistant, session_id, previous_chat_id"
            )

        return self._request("POST", "/chat", payload)

    @staticmethod
    def extract_reply(chat_response: dict[str, Any]) -> str:
        """
        Pull the assistant's text out of a chat response.

        Vapi's `output` is a list of message objects; content is occasionally a
        list of parts rather than a plain string, so both are handled instead of
        assuming one and silently returning "".
        """
        parts: list[str] = []
        for message in chat_response.get("output") or []:
            if message.get("role") not in (None, "assistant"):
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for chunk in content:
                    if isinstance(chunk, str):
                        parts.append(chunk)
                    elif isinstance(chunk, dict) and chunk.get("text"):
                        parts.append(chunk["text"])
        return "\n".join(p for p in parts if p).strip()

    # -- Calls --------------------------------------------------------------

    def create_call(
        self,
        customer_number: str,
        assistant_id: str | None = None,
        phone_number_id: str | None = None,
        assistant_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place an outbound voice call."""
        payload: dict[str, Any] = {
            "assistantId": assistant_id or config.VAPI_ASSISTANT_ID,
            "phoneNumberId": phone_number_id or config.VAPI_PHONE_NUMBER_ID,
            "customer": {"number": customer_number},
        }
        if not payload["assistantId"]:
            raise config.ConfigError(
                "VAPI_ASSISTANT_ID is not set. Run `python run_vapi.py provision` first."
            )
        if not payload["phoneNumberId"]:
            raise config.ConfigError(
                "VAPI_PHONE_NUMBER_ID is not set. Buy or import a number at "
                "https://dashboard.vapi.ai -> Phone Numbers, then put its id in .env"
            )
        if assistant_overrides:
            # This is how a *persisted* Vapi assistant still greets a returning
            # caller by name: per-call overrides carry the memory profile.
            payload["assistantOverrides"] = assistant_overrides
        return self._request("POST", "/call", payload)


# --- Webhook side (Vapi -> us) ------------------------------------------------


def verify_secret(headers: Any) -> bool:
    """
    Constant-time check of the x-vapi-secret header.

    Returns True when no secret is configured, so local development works, but
    the webhook logs a warning in that case — an unauthenticated tool endpoint
    lets anyone POST fabricated bookings.
    """
    expected = config.VAPI_SERVER_SECRET
    if not expected:
        return True
    provided = ""
    for key in ("x-vapi-secret", "X-Vapi-Secret"):
        try:
            provided = headers.get(key) or provided
        except Exception:
            pass
    return hmac.compare_digest(str(provided), str(expected))


def parse_tool_calls(payload: dict[str, Any]) -> list[VapiToolCall]:
    """
    Pull tool calls out of a Vapi server webhook.

    Vapi wraps everything in `message`, and the arguments arrive as either a
    JSON string or an already-decoded object depending on the model — so both
    are handled rather than betting on one and failing mid-call.
    """
    message = payload.get("message") or payload
    if message.get("type") not in (None, "tool-calls", "function-call"):
        return []

    raw_calls = message.get("toolCalls") or message.get("toolCallList") or []
    # Older shape: a single functionCall object.
    if not raw_calls and message.get("functionCall"):
        raw_calls = [{"id": message.get("id", "call_0"), "function": message["functionCall"]}]

    calls: list[VapiToolCall] = []
    for index, raw in enumerate(raw_calls):
        function = raw.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                log.warning("Un-parseable Vapi tool arguments: %r", arguments)
                arguments = {}
        calls.append(
            VapiToolCall(
                id=raw.get("id") or f"call_{index}",
                name=function.get("name") or "",
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


def tool_results_response(results: list[tuple[str, str]]) -> dict[str, Any]:
    """Build the response body Vapi expects: {"results": [{toolCallId, result}]}."""
    return {
        "results": [
            {"toolCallId": call_id, "result": result} for call_id, result in results
        ]
    }


def caller_phone(payload: dict[str, Any]) -> str | None:
    """Best-effort extraction of the caller's number from a Vapi webhook."""
    message = payload.get("message") or payload
    for path in (
        ("call", "customer", "number"),
        ("customer", "number"),
        ("call", "from"),
    ):
        node: Any = message
        for key in path:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        if node:
            return str(node)
    return None
