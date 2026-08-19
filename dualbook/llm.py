"""
LLM abstraction — the conversation brain, provider-swappable.

WHY THIS FILE EXISTS
--------------------
Slot filling needs a model with reliable *function calling* (the agent has to
emit `save_booking` with structured arguments, not describe it in prose). Several
providers offer that on a genuinely free tier, so the project defaults to one
rather than requiring paid credits to run the demo.

Most providers speak the OpenAI-compatible `/chat/completions` shape, so a single
client covers Groq, OpenRouter, Gemini and local Ollama. Anthropic uses a
different wire format and gets its own adapter. Both return the same neutral
`LLMResponse`, so booking_core never learns which provider is behind it.

Set LLM_PROVIDER in .env to switch. No other file changes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)


# --- Neutral types -----------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The raw assistant turn, in whatever shape the provider needs echoed back
    # on the next request. booking_core stores it opaquely.
    raw_assistant_message: Any = None
    refused: bool = False


class LLMError(RuntimeError):
    pass


# --- OpenAI-compatible providers (Groq, OpenRouter, Gemini, Ollama, OpenAI) ---


class OpenAICompatibleClient:
    """
    One client for every provider that implements POST /chat/completions.

    Internal message history uses the OpenAI shape, which is also what
    booking_core stores:
        {"role": "user", "content": "..."}
        {"role": "assistant", "content": ..., "tool_calls": [...]}
        {"role": "tool", "tool_call_id": "...", "content": "..."}
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        fallbacks: list[str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = self.MAX_TOKENS
        # Set to False once a provider tells us it doesn't accept the parameter.
        self._send_reasoning_param = True
        # Smaller models on the same provider, tried in order if `model` runs
        # out of daily quota. See _downgrade_model.
        self.fallbacks = list(fallbacks or [])


    # A booking reply is one or two sentences. 1024 was ample for that — and far
    # too little for a REASONING model, which spends the same budget thinking
    # before it writes anything. qwen3 was observed burning all 1024 tokens on
    # hidden reasoning and returning EMPTY content with finish_reason='length',
    # which surfaced to the customer as "Sorry, could you say that again?" on
    # every turn, forever. Two defences below, because that failure is silent.
    MAX_TOKENS = 2048

    # Models whose "thinking" is pure cost here. Slot filling is not a reasoning
    # task: the work is capturing what the customer said and calling one tool.
    # Turning it off cut a turn from ~300 completion tokens to ~15, and tool
    # calling was verified unaffected.
    _NO_REASONING_MODELS = ("qwen",)

    def _reasoning_params(self) -> dict[str, Any]:
        """Ask for no chain-of-thought, where the model understands the request."""
        if not self._send_reasoning_param:
            return {}
        if any(tag in self.model.lower() for tag in self._NO_REASONING_MODELS):
            return {"reasoning_effort": "none"}
        return {}

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            # Low temperature: slot filling wants consistent, literal capture of
            # what the customer said, not creative paraphrasing of their name.
            "temperature": 0.3,
            "max_tokens": self.max_tokens,
            **self._reasoning_params(),
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"

        response = self._post_with_retry(
            f"{self.base_url}/chat/completions", headers, body
        )
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        # Ran out of budget before saying anything. Without this the turn
        # returns nothing and the agent falls back to "Sorry, could you say that
        # again?" — every turn, with no clue why. One retry with real headroom.
        # Note this fires on a PARTIAL answer too, not only an empty one: a reply
        # cut off as "Thanks, Kunal. I've" is sent to the customer mid-sentence,
        # which is worse than a short pause while we ask again properly.
        if (
            choice.get("finish_reason") == "length"
            and not message.get("tool_calls")
        ):
            widened = min(self.max_tokens * 4, 8192)
            log.warning(
                "%s ran out of room at %d tokens (answer was %s) — retrying with %d",
                self.model, self.max_tokens,
                "empty" if not (message.get("content") or "").strip() else "cut off",
                widened,
            )
            body["max_tokens"] = widened
            response = self._post_with_retry(
                f"{self.base_url}/chat/completions", headers, body
            )
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}

        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    log.warning("Un-parseable tool arguments: %r", args)
                    args = {}
            tool_calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{len(tool_calls)}",
                    name=fn.get("name") or "",
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            # Echo the provider's own assistant message back verbatim; some
            # providers reject a reconstructed one whose tool_call ids differ.
            raw_assistant_message={
                "role": "assistant",
                "content": message.get("content") or "",
                **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {}),
            },
        )

    # Free tiers are rate-limited by tokens-per-minute, and a booking sends the
    # whole transcript plus the tool schema on every turn — so a multi-turn
    # conversation hits the ceiling routinely. A 429 here is a normal, expected
    # condition rather than a failure, so we wait and retry instead of dropping
    # the customer mid-booking.
    MAX_RETRIES = 4

    def _post_with_retry(self, url: str, headers: dict, body: dict):
        last_error = ""
        for attempt in range(self.MAX_RETRIES):
            try:
                response = requests.post(url, headers=headers, json=body, timeout=60)
            except requests.exceptions.RequestException as exc:
                # Dropped TLS handshakes, DNS blips and read timeouts never
                # reach a status code, so the status-based branch below can't
                # see them. On a home/mobile connection these are common enough
                # that not retrying means a customer's booking dies mid-sentence.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self.MAX_RETRIES - 1:
                    log.error("LLM connection failed: %s", last_error)
                    raise LLMError(
                        f"Could not reach {self.base_url} after "
                        f"{self.MAX_RETRIES} attempts.\n"
                        f"  Last error: {type(exc).__name__}\n"
                        "  This is a network problem, not a bad API key — check "
                        "your connection, VPN, or firewall/antivirus TLS "
                        "inspection, then try again."
                    ) from exc
                delay = min(2.0 ** attempt, 15.0)
                log.warning(
                    "LLM connection error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1, self.MAX_RETRIES, type(exc).__name__, delay,
                )
                time.sleep(delay)
                continue

            if response.status_code < 400:
                return response

            last_error = response.text[:400]
            retryable = response.status_code == 429 or response.status_code >= 500

            # A malformed tool call is the model's mistake, and a re-roll
            # usually produces a valid one. Treating it as fatal ends the
            # booking over something that fixes itself on the next attempt.
            if (
                response.status_code == 400
                and "reasoning_effort" in last_error
                and self._send_reasoning_param
            ):
                # An optimisation must never be the reason a booking fails.
                log.warning(
                    "%s does not accept reasoning_effort — dropping it and retrying",
                    self.model,
                )
                self._send_reasoning_param = False
                body.pop("reasoning_effort", None)
                continue

            if response.status_code == 400 and "tool_use_failed" in last_error:
                log.warning(
                    "Model emitted off-schema tool arguments — re-rolling "
                    "(attempt %d/%d)", attempt + 1, self.MAX_RETRIES,
                )
                retryable = True

            if not retryable or attempt == self.MAX_RETRIES - 1:
                log.error("LLM call failed %s: %s", response.status_code, last_error)
                raise LLMError(
                    f"{self.base_url} returned {response.status_code}: {last_error}"
                )

            delay = self._retry_delay(response, attempt)

            # A per-minute cap clears in seconds and is worth waiting out. A
            # per-DAY cap does not — retrying just burns time and ends in the
            # same failure. Before giving up, drop to a smaller model on the
            # same provider: free tiers meter each model separately, so the
            # cheaper one is usually still open. Losing a little answer quality
            # mid-demo is a far better outcome than telling a customer standing
            # in front of you that the assistant is unavailable.
            if delay > self.MAX_WAIT_SECONDS:
                if self._downgrade_model():
                    body = {**body, "model": self.model}
                    continue
                raise LLMError(self._quota_message(response, delay))

            log.warning(
                "LLM %s (attempt %d/%d) — retrying in %.1fs",
                response.status_code, attempt + 1, self.MAX_RETRIES, delay,
            )
            time.sleep(delay)

        raise LLMError(f"{self.base_url} failed after retries: {last_error}")

    # Longest we'll block a customer waiting for a rate limit to clear.
    MAX_WAIT_SECONDS = 45

    def _downgrade_model(self) -> bool:
        """
        Move to the next fallback model after a daily quota is exhausted.

        Returns False when there is nowhere left to go, which is when the
        caller should fail with the explanatory message. The switch sticks for
        the life of this client so the next turn doesn't pay the same 429 again.
        """
        remaining = [m for m in (self.fallbacks or []) if m != self.model]
        if not remaining:
            return False
        previous, self.model = self.model, remaining[0]
        self.fallbacks = remaining[1:]
        log.warning(
            "Daily quota exhausted on %s — falling back to %s for the rest of "
            "this run. Set LLM_MODEL in .env to pin a model.",
            previous, self.model,
        )
        return True

    def _quota_message(self, response, delay: float) -> str:
        """Explain an exhausted quota and what to do about it."""
        body = response.text or ""
        daily = "per day" in body or "TPD" in body
        when = (
            f"{int(delay // 60)}m {int(delay % 60)}s" if delay >= 60
            else f"{delay:.0f}s"
        )
        scope = "daily" if daily else "rate"
        lines = [
            f"{self.model}: {scope} quota exhausted on {self.base_url}.",
            f"  Resets in about {when}.",
            "  Options:",
            f"    - wait {when} and run it again",
        ]
        if daily:
            lines.append(
                "    - switch provider for now: set LLM_PROVIDER=gemini in .env "
                "and add GEMINI_API_KEY (free: aistudio.google.com/apikey)"
            )
        return "\n".join(lines)

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        """
        Honour the provider's own guidance before guessing.

        Groq reports both a Retry-After header and a "try again in ..." hint.
        That hint comes in two shapes — "1.47s" for a per-minute cap and
        "7m6.816s" for a daily one — so minutes must be parsed. Reading
        "7m6.816s" as 6.8 seconds silently turns a 7-minute wait into four
        pointless retries.

        NOT capped here: the caller compares against MAX_WAIT_SECONDS to decide
        between waiting and failing with a clear message. Capping first would
        hide a daily quota behind a short sleep.
        """
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header) + 0.25
            except ValueError:
                pass

        match = re.search(
            r"try again in (?:(\d+)m)?([\d.]+)s", response.text or ""
        )
        if match:
            try:
                minutes = int(match.group(1) or 0)
                return minutes * 60 + float(match.group(2)) + 0.25
            except ValueError:
                pass

        return min(2.0 ** attempt, 15.0)

    @staticmethod
    def tool_result_message(tool_call: ToolCall, content: str) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "content": content,
        }


# --- Anthropic (opt-in; paid) -------------------------------------------------


class AnthropicClient:
    """
    Adapter for the Anthropic Messages API.

    Kept because Claude is the strongest option for this task if the user has
    credits — but it is NOT the default, since it has no free tier. Translates
    to/from the same OpenAI-shaped history the rest of the project uses so the
    two backends stay interchangeable.
    """

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self._sdk = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=self._to_anthropic(messages),
            tools=[
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                }
                for t in tools
            ],
            # Low effort: slot filling is not reasoning-heavy and both channels
            # are latency-sensitive. Thinking stays ON (the default) — disabling
            # it makes the model likelier to describe a tool call in prose
            # instead of emitting one, which would silently lose the booking.
            output_config={"effort": "low"},
        )

        if response.stop_reason == "refusal":
            return LLMResponse(refused=True)

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {}))
            for b in response.content
            if b.type == "tool_use"
        ]

        # Store the assistant turn in OpenAI shape so history stays portable.
        assistant: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in tool_calls
            ]

        return LLMResponse(text=text, tool_calls=tool_calls, raw_assistant_message=assistant)

    @staticmethod
    def _to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI-shaped history -> Anthropic content blocks."""
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]

            if role == "user":
                out.append({"role": "user", "content": msg["content"]})

            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for call in msg.get("tool_calls") or []:
                    fn = call["function"]
                    args = fn["arguments"]
                    if isinstance(args, str):
                        args = json.loads(args or "{}")
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": fn["name"],
                            "input": args,
                        }
                    )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})

            elif role == "tool":
                # Anthropic carries tool results in a *user* turn.
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
                if out and out[-1]["role"] == "user" and isinstance(
                    out[-1]["content"], list
                ):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})

        return out

    @staticmethod
    def tool_result_message(tool_call: ToolCall, content: str) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "content": content,
        }


# --- Provider registry --------------------------------------------------------
# base_url, env var holding the key, default model, whether a key is required.
PROVIDERS: dict[str, dict[str, Any]] = {
    # Free tier, fast, solid function calling. The project default.
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "default_model": "qwen/qwen3.6-27b",
        # Free-tier quotas are metered PER MODEL, so a smaller sibling is a real
        # escape hatch when the big one runs dry mid-demo. Both still do
        # function calling, which is the one capability slot filling requires.
        "fallbacks": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
        "requires_key": True,
        "signup": "https://console.groq.com/keys",
    },
    # Free tier via Gemini's OpenAI-compatible endpoint.
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "default_model": "gemini-2.0-flash",
        "fallbacks": ["gemini-2.0-flash-lite"],
        "requires_key": True,
        "signup": "https://aistudio.google.com/apikey",
    },
    # Has free-tier models (`:free` suffix).
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
        "requires_key": True,
        "signup": "https://openrouter.ai/keys",
    },
    # Fully local and free — no account, no network. Needs a tool-capable model.
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key_env": "OLLAMA_API_KEY",
        "default_model": "llama3.1",
        "requires_key": False,
        "signup": "https://ollama.com (then: ollama pull llama3.1)",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "requires_key": True,
        "signup": "https://platform.openai.com/api-keys",
    },
    # Paid — best quality if you have credits.
    "anthropic": {
        "base_url": None,
        "key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-opus-5",
        "requires_key": True,
        "signup": "https://platform.claude.com",
    },
}


def get_client(provider: str | None = None, model: str | None = None):
    """Build the configured LLM client, with an actionable error if unset."""
    name = (provider or config.LLM_PROVIDER or "groq").lower()
    spec = PROVIDERS.get(name)
    if spec is None:
        raise config.ConfigError(
            f"Unknown LLM_PROVIDER {name!r}. Choose one of: "
            f"{', '.join(sorted(PROVIDERS))}"
        )

    api_key = config.get_env(spec["key_env"])
    if spec["requires_key"] and not api_key:
        raise config.ConfigError(
            f"LLM_PROVIDER is {name!r} but {spec['key_env']} is not set.\n"
            f"  Get a key: {spec['signup']}\n"
            f"  Then put {spec['key_env']}=... in your .env"
        )

    chosen_model = model or config.LLM_MODEL or spec["default_model"]

    if name == "anthropic":
        return AnthropicClient(api_key=api_key, model=chosen_model)

    base_url = config.LLM_BASE_URL or spec["base_url"]
    # An explicitly pinned LLM_MODEL is honoured with no fallbacks: if you named
    # a model, silently answering as a different one would be a worse surprise
    # than a clear quota error.
    fallbacks = [] if (model or config.LLM_MODEL) else spec.get("fallbacks", [])
    return OpenAICompatibleClient(
        base_url=base_url, api_key=api_key, model=chosen_model, fallbacks=fallbacks
    )


def describe_provider() -> str:
    """One-line summary for startup logs and the simulators."""
    name = (config.LLM_PROVIDER or "groq").lower()
    spec = PROVIDERS.get(name, {})
    model = config.LLM_MODEL or spec.get("default_model", "?")
    return f"{name}/{model}"
