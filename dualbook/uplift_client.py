"""
Voice transport — Uplift AI Realtime Assistants (docs.upliftai.org).

Like whatsapp_client.py, this file is the only place that knows the vendor's
HTTP surface. Three endpoints matter:

    POST /v1/realtime-assistants                        create an assistant
    POST /v1/realtime-assistants/{id}                   update an assistant
    POST /v1/realtime-assistants/{id}/createSession     mint a session token
    POST /v1/realtime-assistants/adhoc/createSession    one-off inline config

createSession returns {token, wsUrl, roomName}: a LiveKit JWT plus the room to
join. Uplift's docs are explicit that sessions must be minted server-side —
the API key must never reach a browser — which is why this lives in the
backend and run_voice.py exposes a token endpoint rather than shipping the key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from . import booking_core, config

log = logging.getLogger(__name__)


@dataclass
class Session:
    """What a client needs to join the realtime room."""

    token: str
    ws_url: str
    room_name: str

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "Session":
        return cls(
            token=data["token"],
            ws_url=data.get("wsUrl") or data.get("ws_url", ""),
            room_name=data.get("roomName") or data.get("room_name", ""),
        )


class UpliftClient:
    def __init__(self, api_key: str | None = None, api_url: str | None = None) -> None:
        self.api_key = api_key or config.UPLIFT_API_KEY
        self.api_url = (api_url or config.UPLIFT_API_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise config.ConfigError(
                "UPLIFT_API_KEY is not set - cannot reach Uplift AI."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self.api_url}{path}", headers=self._headers(), json=body, timeout=30
        )
        if response.status_code >= 400:
            log.error("Uplift %s failed %s: %s", path, response.status_code, response.text)
        response.raise_for_status()
        return response.json()

    # -- Assistant configuration --------------------------------------------

    @staticmethod
    def build_config(
        instructions: str,
        greeting_instructions: str | None = None,
        session_ttl: int = 900,
    ) -> dict[str, Any]:
        """
        Assemble the assistant config block.

        `instructions` and the tool schema both come from booking_core, so the
        voice agent asks for exactly the same fields, in the same order, with
        the same completion rule as the WhatsApp agent. That is the whole point
        of the split: this module owns the vendor payload shape, booking_core
        owns the behaviour.
        """
        return {
            "session": {"ttl": session_ttl, "roomPrefix": "dualbook"},
            "agent": {
                "instructions": instructions,
                # Let the agent speak first — on a phone call, silence after
                # pickup reads as a dropped line, and the greeting is where the
                # returning-customer personalisation lands.
                "initialGreeting": True,
                "greetingInstructions": greeting_instructions
                or (
                    "Greet the caller in one short sentence and offer to book "
                    "their car wash. If the pre-loaded customer memory shows "
                    "this is a returning customer, greet them by name and "
                    "offer their usual vehicle and service in the same "
                    "sentence."
                ),
                "tools": [booking_core.uplift_tool()],
            },
            "stt": {
                "default": {
                    "provider": config.UPLIFT_STT_PROVIDER,
                    "model": config.UPLIFT_STT_MODEL,
                    "language": config.UPLIFT_STT_LANGUAGE,
                }
            },
            "tts": {
                "default": {
                    "provider": config.UPLIFT_TTS_PROVIDER,
                    "voiceId": config.UPLIFT_TTS_VOICE_ID,
                    "outputFormat": config.UPLIFT_TTS_OUTPUT_FORMAT,
                }
            },
            "llm": {
                "default": {
                    "provider": config.UPLIFT_LLM_PROVIDER,
                    "model": config.UPLIFT_LLM_MODEL,
                }
            },
        }

    def create_assistant(
        self, name: str, instructions: str, description: str | None = None
    ) -> dict[str, Any]:
        return self._post(
            "/v1/realtime-assistants",
            {
                "name": name,
                "description": description or "DualBook car wash voice booking agent",
                "config": self.build_config(instructions),
            },
        )

    def update_assistant(
        self, assistant_id: str, name: str, instructions: str
    ) -> dict[str, Any]:
        return self._post(
            f"/v1/realtime-assistants/{assistant_id}",
            {"name": name, "config": self.build_config(instructions)},
        )

    def get_assistant(self, assistant_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{self.api_url}/v1/realtime-assistants/{assistant_id}",
            headers=self._headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    # -- Sessions ------------------------------------------------------------

    def create_session(
        self, assistant_id: str, participant_name: str, room_name: str | None = None
    ) -> Session:
        """Session against a persisted assistant."""
        body: dict[str, Any] = {"participantName": participant_name}
        if room_name:
            body["roomName"] = room_name
        return Session.from_response(
            self._post(f"/v1/realtime-assistants/{assistant_id}/createSession", body)
        )

    def create_adhoc_session(
        self,
        instructions: str,
        participant_name: str,
        room_name: str | None = None,
    ) -> Session:
        """
        Session with an inline, non-persisted config.

        This is the important one for the memory layer: the instructions carry
        the CALLER'S pre-loaded profile, so they differ per call. A persisted
        assistant has one fixed instruction string shared by every caller,
        which cannot say "Welcome back, Ali". Adhoc sessions let us inject
        per-caller memory at session-creation time — still one lookup, still
        zero per-turn retrieval.
        """
        body: dict[str, Any] = {
            "participantName": participant_name,
            "config": self.build_config(instructions),
        }
        if room_name:
            body["roomName"] = room_name
        return Session.from_response(
            self._post("/v1/realtime-assistants/adhoc/createSession", body)
        )
