"""
WhatsApp transport — Whapi.Cloud or the Meta WhatsApp Cloud API.

THIS IS THE ONLY FILE THAT KNOWS WHICH WHATSAPP PROVIDER WE USE. Everything
outside it sees the neutral `IncomingMessage` dataclass, which is what made
adding a second provider a change to one file.

Pick with WHATSAPP_PROVIDER in .env:

    whapi  - Whapi.Cloud (paid, trial available)
    meta   - Meta WhatsApp Cloud API (FREE test number, 1,000 free service
             conversations/month) -> the free option

A note on Vapi, since it comes up: Vapi has no WhatsApp channel. Its text
surface is the Chat API and SMS (US only). To use Vapi as the *brain* behind
WhatsApp you keep a transport from this file and route the text through
`vapi_client.chat()`; `run_whatsapp.py --brain vapi` does exactly that.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from . import config, wa_templates

log = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    """Provider-neutral inbound message."""

    chat_id: str      # what we reply to (provider-specific address)
    phone: str        # raw sender number; normalised by memory.normalize_phone
    text: str
    message_id: str | None = None


class WhatsAppClient:
    def __init__(self, token: str | None = None, api_url: str | None = None) -> None:
        self.token = token or config.WHAPI_TOKEN
        self.api_url = (api_url or config.WHAPI_API_URL).rstrip("/")

    # -- Outbound ------------------------------------------------------------

    def send_text(self, to: str, body: str) -> dict[str, Any]:
        """
        Send a WhatsApp text message.

        Whapi: POST /messages/text  {"to": "<chat_id|phone>", "body": "..."}
        """
        if not self.token:
            raise config.ConfigError(
                "WHAPI_TOKEN is not set - cannot send WhatsApp messages."
            )

        response = requests.post(
            f"{self.api_url}/messages/text",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            json={"to": to, "body": body},
            timeout=20,
        )
        if response.status_code >= 400:
            log.error("Whapi send failed %s: %s", response.status_code, response.text)
        response.raise_for_status()
        return response.json()

    def send_template(
        self, to: str, template: "wa_templates.Template", values: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Send a pre-approved template message.

        Whapi links a real WhatsApp account rather than a Business API number,
        so it is not subject to Meta's 24-hour session window and does not need
        an approved template to open a conversation. Sending the rendered text
        is therefore the correct behaviour here, not a workaround — but it is
        reported as 'session' by the caller, because that is what left.
        """
        return self.send_text(to=to, body=template.render(values))

    # -- Inbound -------------------------------------------------------------

    @staticmethod
    def parse_incoming(payload: dict[str, Any]) -> list[IncomingMessage]:
        """
        Turn a Whapi webhook body into provider-neutral messages.

        Whapi posts:
            {"messages": [{"id": "...", "from_me": false,
                           "chat_id": "923001234567@s.whatsapp.net",
                           "from": "923001234567", "type": "text",
                           "text": {"body": "hi"}}],
             "event": {...}}

        We ignore anything that isn't an inbound text: status callbacks and
        echoes of our own outbound messages (`from_me`) would otherwise make
        the agent reply to itself.
        """
        results: list[IncomingMessage] = []

        for raw in payload.get("messages") or []:
            if raw.get("from_me"):
                continue
            if raw.get("type") not in (None, "text"):
                continue

            body = (raw.get("text") or {}).get("body") or raw.get("body") or ""
            if not str(body).strip():
                continue

            chat_id = raw.get("chat_id") or raw.get("from") or ""
            phone = raw.get("from") or chat_id
            if not chat_id:
                continue

            results.append(
                IncomingMessage(
                    chat_id=str(chat_id),
                    phone=str(phone),
                    text=str(body).strip(),
                    message_id=raw.get("id"),
                )
            )

        return results

    @staticmethod
    def authenticated() -> bool:
        """Is this transport able to prove a webhook POST really came from the
        provider? Reported at startup and on /health so an unauthenticated
        endpoint is visible rather than assumed."""
        return bool(config.WHAPI_WEBHOOK_SECRET)

    @staticmethod
    def verify_webhook(headers: dict[str, str], body: bytes = b"") -> bool:
        """
        Optional shared-secret check on the webhook.

        Whapi lets you attach custom headers to webhook deliveries; set
        WHAPI_WEBHOOK_SECRET and configure the same value as an
        `X-Webhook-Secret` header so an open endpoint can't be spammed with
        fabricated bookings. If no secret is configured we accept everything
        (fine for local ngrok testing, not for production).

        `body` is the raw request bytes. Unused here — Whapi authenticates with
        a static header — but part of the interface because Meta signs the body
        itself and cannot verify from headers alone.
        """
        expected = config.WHAPI_WEBHOOK_SECRET
        if not expected:
            return True
        lowered = {k.lower(): v for k, v in headers.items()}
        supplied = lowered.get("x-webhook-secret") or ""
        # compare_digest, not ==, so the comparison doesn't leak the secret one
        # character at a time to anyone willing to time a few thousand requests.
        return hmac.compare_digest(supplied, expected)


class MetaSendError(RuntimeError):
    """
    A Meta rejection, carrying the reason Meta actually gave.

    `raise_for_status()` produces "400 Client Error: Bad Request for url: ..."
    and throws the response body away — but the body is the ONLY part that says
    what was wrong. That difference is the difference between an outbox row an
    owner can act on and one that just says something broke.
    """


def _meta_error(response: Any) -> MetaSendError:
    """Turn Meta's error body into a sentence, with the fix where one is known."""
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        error = {}

    code = error.get("code")
    message = error.get("message") or response.text[:200] or "unknown error"
    detail = (error.get("error_data") or {}).get("details")

    # The three that account for nearly every failed send in practice.
    hint = {
        131047: "The 24-hour window has closed — this needs an approved "
                "template, not free text.",
        132001: "No APPROVED template with that name and language. Check both "
                "against Meta -> WhatsApp -> Manage templates.",
        132000: "The number of variables sent does not match the approved "
                "template.",
        131030: "The recipient is not on your test number's allowed list. "
                "Meta -> API Setup -> To -> Manage phone number list.",
        190: "The access token has expired. Temporary tokens last 24 hours.",
    }.get(code)

    parts = [f"Meta {response.status_code}"]
    if code:
        parts.append(f"(error {code})")
    parts.append(f": {message}")
    if detail:
        parts.append(f" — {detail}")
    if hint:
        parts.append(f"  FIX: {hint}")
    return MetaSendError("".join(parts))


class MetaWhatsAppClient(WhatsAppClient):
    """
    Meta WhatsApp Cloud API — the free transport.

    Free tier: a test number that can message up to 5 verified recipients, plus
    1,000 service conversations/month once you attach a real number. Good enough
    to build and demo the whole flow without paying anyone.

    Same interface as WhatsAppClient, so run_whatsapp.py can't tell them apart.
    """

    def __init__(self, token: str | None = None, api_url: str | None = None,
                 phone_number_id: str | None = None) -> None:
        self.token = token or config.META_WA_TOKEN
        self.api_url = (api_url or config.META_WA_API_URL).rstrip("/")
        self.phone_number_id = phone_number_id or config.META_WA_PHONE_NUMBER_ID

    @staticmethod
    def _to_msisdn(value: str) -> str:
        """'+92 300 123 4567' or '923...@s.whatsapp.net' -> '923001234567'."""
        return re.sub(r"\D", "", str(value).split("@")[0])

    def send_text(self, to: str, body: str) -> dict[str, Any]:
        """POST /{phone-number-id}/messages with a text payload."""
        if not self.token or not self.phone_number_id:
            raise config.ConfigError(
                "META_WA_TOKEN and META_WA_PHONE_NUMBER_ID must both be set.\n"
                "  Get them: https://developers.facebook.com/apps -> your app ->\n"
                "  WhatsApp -> API Setup (temporary token + test number id)"
            )
        response = requests.post(
            f"{self.api_url}/{self.phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                # Meta wants bare E.164 digits — no '+', spaces or punctuation.
                # Our own numbers are stored as "+92 300 123 4567", so strip to
                # digits rather than just dropping the leading '+'.
                "to": self._to_msisdn(to),
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
            timeout=20,
        )
        if response.status_code >= 400:
            error = _meta_error(response)
            log.error("Meta text send to %s failed — %s", to, error)
            raise error
        return response.json()

    def send_template(
        self, to: str, template: "wa_templates.Template", values: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Send an approved template message — the only shape allowed to OPEN a
        conversation, or to reach a number whose 24-hour window has closed.

        Same endpoint as send_text, different payload: `type: template` with the
        registered name, its language code, and the body parameters positionally
        filling {{1}}, {{2}} ... The text itself is NOT sent — Meta renders it
        from the approved body it already holds, which is exactly why the name
        and language must match the registration.

        Failure modes worth recognising in the log:
          132001  template name/language does not match anything approved
          132000  parameter count differs from the approved body
          131047  (on send_text) the window is closed — you needed this method
        """
        if not self.token or not self.phone_number_id:
            raise config.ConfigError(
                "META_WA_TOKEN and META_WA_PHONE_NUMBER_ID must both be set."
            )

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self._to_msisdn(to),
            "type": "template",
            "template": {
                "name": template.name,
                "language": {"code": template.language},
                "components": template.components(values),
            },
        }
        response = requests.post(
            f"{self.api_url}/{self.phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if response.status_code >= 400:
            error = _meta_error(response)
            log.error(
                "Meta template send to %s failed (template=%r lang=%r) — %s",
                to, template.name, template.language, error,
            )
            raise error
        return response.json()

    @staticmethod
    def parse_incoming(payload: dict[str, Any]) -> list[IncomingMessage]:
        """
        Meta nests messages deeply:
            {"entry": [{"changes": [{"value": {
                "messages": [{"from": "923...", "id": "wamid...",
                              "type": "text", "text": {"body": "hi"}}],
                "statuses": [...] }}]}]}

        `statuses` callbacks (delivered/read) arrive on the same webhook and
        carry no `messages`, so they fall through and produce nothing.
        """
        results: list[IncomingMessage] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for raw in value.get("messages") or []:
                    if raw.get("type") != "text":
                        continue
                    body = (raw.get("text") or {}).get("body") or ""
                    sender = raw.get("from")
                    if not str(body).strip() or not sender:
                        continue
                    results.append(
                        IncomingMessage(
                            chat_id=str(sender),
                            phone=str(sender),
                            text=str(body).strip(),
                            message_id=raw.get("id"),
                        )
                    )
        return results

    @staticmethod
    def authenticated() -> bool:
        return bool(config.META_WA_APP_SECRET)

    @staticmethod
    def verify_webhook(headers: dict[str, str], body: bytes = b"") -> bool:
        """
        Verify Meta's `X-Hub-Signature-256` over the RAW request body.

        This is the only check that authenticates an individual message. The
        GET handshake (verify_subscription) authenticates the *subscription*
        once, when you first save the webhook URL in Meta's dashboard — after
        that it never runs again, so on its own it leaves the endpoint open to
        anyone who learns the URL. An ngrok URL is not a secret.

        Meta signs the exact bytes it sent, so the body must be verified BEFORE
        it is parsed and must never be re-serialised first: `json.dumps` of a
        parsed payload differs from the original in whitespace and key order,
        and the HMAC would never match.

        With no META_WA_APP_SECRET configured we accept — otherwise setting up
        the free tier would hard-fail before the reviewer ever gets a message —
        but `authenticated()` reports it so the state is visible at startup and
        on /health instead of being quietly assumed.
        """
        secret = config.META_WA_APP_SECRET
        if not secret:
            return True

        lowered = {k.lower(): v for k, v in headers.items()}
        supplied = (lowered.get("x-hub-signature-256") or "").strip()
        if not supplied.startswith("sha256="):
            log.warning("Webhook POST carried no X-Hub-Signature-256 header")
            return False

        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied[len("sha256="):]):
            log.warning("Webhook signature did not match — rejecting")
            return False
        return True

    @staticmethod
    def verify_subscription(params: dict[str, str]) -> str | None:
        """
        Meta's one-time GET handshake: echo hub.challenge back if the verify
        token matches. Returns None to signal "reject with 403".
        """
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token")
            and params.get("hub.verify_token") == config.META_WA_VERIFY_TOKEN
        ):
            return params.get("hub.challenge")
        return None


def get_client() -> WhatsAppClient:
    """Build whichever transport WHATSAPP_PROVIDER selects."""
    if config.WHATSAPP_PROVIDER == "meta":
        return MetaWhatsAppClient()
    return WhatsAppClient()
