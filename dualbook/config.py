"""
Central configuration. Every secret is read from the environment (see .env.example).

Why a module instead of scattered os.getenv calls: the two agents (WhatsApp and
Voice) and the CLI tools all need the same keys, and we want a *single* place
that produces a clear, actionable error when a key is missing rather than an
opaque 401 from a third-party API three call-frames deep.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Load .env from the project root (parent of this package) so the entry
    # points work no matter which directory the reviewer runs them from.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover - dotenv is in requirements.txt
    pass


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    # Treat empty strings as unset — a blank line in .env is a common mistake.
    return value if value else None


def require(name: str) -> str:
    """Fetch a required env var or fail with a message that says how to fix it."""
    value = _get(name)
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name!r}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def get_env(name: str, default: str | None = None) -> str | None:
    """Public read for provider-specific keys resolved at runtime (see llm.py)."""
    return _get(name, default)


# --- LLM (conversation brain) -------------------------------------------------
# Provider-swappable; see dualbook/llm.py for the registry. Default is Groq's
# free tier so the project can be run end to end without paid credits.
LLM_PROVIDER = _get("LLM_PROVIDER", "groq")
# Leave unset to take the provider's default model.
LLM_MODEL = _get("LLM_MODEL")
# Optional override for self-hosted / proxied OpenAI-compatible endpoints.
LLM_BASE_URL = _get("LLM_BASE_URL")

# --- Vapi (voice calls + text chat) -------------------------------------------
# Vapi is a VOICE platform. Its text channel is the Chat API (and SMS, US only);
# it has NO native WhatsApp channel, so WhatsApp is bridged - see vapi_client.py.
VAPI_API_KEY = _get("VAPI_API_KEY")            # private key: server-side only
VAPI_PUBLIC_KEY = _get("VAPI_PUBLIC_KEY")      # safe to ship to a browser/web SDK
VAPI_API_URL = _get("VAPI_API_URL", "https://api.vapi.ai")
VAPI_ASSISTANT_ID = _get("VAPI_ASSISTANT_ID")  # filled in by `run_vapi.py provision`
VAPI_PHONE_NUMBER_ID = _get("VAPI_PHONE_NUMBER_ID")  # needed only for real calls

# Vapi charges model usage at cost, and $0 if you bring your own key. Pointing it
# at your free Groq key is what keeps the running cost to Vapi's own per-minute
# fee instead of that plus model tokens.
VAPI_LLM_PROVIDER = _get("VAPI_LLM_PROVIDER", "groq")
VAPI_LLM_MODEL = _get("VAPI_LLM_MODEL", "openai/gpt-oss-120b")
VAPI_VOICE_PROVIDER = _get("VAPI_VOICE_PROVIDER", "vapi")
VAPI_VOICE_ID = _get("VAPI_VOICE_ID", "Elliot")
VAPI_TRANSCRIBER_PROVIDER = _get("VAPI_TRANSCRIBER_PROVIDER", "deepgram")
VAPI_TRANSCRIBER_MODEL = _get("VAPI_TRANSCRIBER_MODEL", "nova-2")
VAPI_TRANSCRIBER_LANGUAGE = _get("VAPI_TRANSCRIBER_LANGUAGE", "en")
# Public HTTPS URL Vapi calls to execute save_booking (ngrok in development).
VAPI_SERVER_URL = _get("VAPI_SERVER_URL")
# Shared secret Vapi echoes back as x-vapi-secret so we can reject forgeries.
VAPI_SERVER_SECRET = _get("VAPI_SERVER_SECRET")

# --- WhatsApp transport selection ---------------------------------------------
# 'meta' = Meta WhatsApp Cloud API (free tier) — the default, since it's the
# only WhatsApp option you can run without paying. 'whapi' = Whapi.Cloud.
WHATSAPP_PROVIDER = _get("WHATSAPP_PROVIDER", "meta").lower()

# Meta WhatsApp Cloud API (free test number + 1,000 free service conversations)
META_WA_TOKEN = _get("META_WA_TOKEN")
META_WA_PHONE_NUMBER_ID = _get("META_WA_PHONE_NUMBER_ID")
META_WA_VERIFY_TOKEN = _get("META_WA_VERIFY_TOKEN")  # you invent this string
META_WA_API_URL = _get("META_WA_API_URL", "https://graph.facebook.com/v21.0")
# App secret from the Meta dashboard. Meta signs every webhook DELIVERY with it
# (X-Hub-Signature-256), which is the only thing that proves a POST came from
# Meta and not from whoever guessed your public URL. The verify token gates the
# one-time subscription handshake and does nothing for individual messages —
# these two are not interchangeable.
META_WA_APP_SECRET = _get("META_WA_APP_SECRET")

# --- WhatsApp (Whapi.Cloud) ---------------------------------------------------
WHAPI_TOKEN = _get("WHAPI_TOKEN")
WHAPI_API_URL = _get("WHAPI_API_URL", "https://gate.whapi.cloud")
# Optional shared secret you configure on the Whapi webhook so strangers can't
# POST fake bookings into your store.
WHAPI_WEBHOOK_SECRET = _get("WHAPI_WEBHOOK_SECRET")

# --- Voice (Uplift AI Realtime Assistants) ------------------------------------
UPLIFT_API_KEY = _get("UPLIFT_API_KEY")
UPLIFT_API_URL = _get("UPLIFT_API_URL", "https://api.upliftai.org")
UPLIFT_ASSISTANT_ID = _get("UPLIFT_ASSISTANT_ID")

# Uplift runs its own realtime LLM/STT/TTS stack; these pick the providers.
# Groq by default here too — it's the free-tier option Uplift supports.
UPLIFT_LLM_PROVIDER = _get("UPLIFT_LLM_PROVIDER", "groq")
UPLIFT_LLM_MODEL = _get("UPLIFT_LLM_MODEL", "openai/gpt-oss-120b")
UPLIFT_STT_PROVIDER = _get("UPLIFT_STT_PROVIDER", "deepgram")
UPLIFT_STT_MODEL = _get("UPLIFT_STT_MODEL", "nova-2")
UPLIFT_STT_LANGUAGE = _get("UPLIFT_STT_LANGUAGE", "en")
UPLIFT_TTS_PROVIDER = _get("UPLIFT_TTS_PROVIDER", "upliftai")
UPLIFT_TTS_VOICE_ID = _get("UPLIFT_TTS_VOICE_ID", "v_meklc281")
UPLIFT_TTS_OUTPUT_FORMAT = _get("UPLIFT_TTS_OUTPUT_FORMAT", "MP3_22050_32")

# --- Storage ------------------------------------------------------------------
# Default lives next to the package so a fresh clone "just works".
DB_PATH = _get("DB_PATH") or str(Path(__file__).resolve().parent / "data" / "dualbook.db")
CSV_EXPORT_PATH = _get("CSV_EXPORT_PATH") or str(
    Path(__file__).resolve().parent / "data" / "bookings.csv"
)

# --- Business config ----------------------------------------------------------
BUSINESS_NAME = _get("BUSINESS_NAME", "SparkleWash Car Care")
SERVICE_MENU = [
    "Express Wash",
    "Premium Wash",
    "Interior Detail",
    "Full Detail",
]
