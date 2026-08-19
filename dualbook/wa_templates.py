"""
WhatsApp message templates — the shape a message must take outside 24 hours.

WHY THIS FILE EXISTS
--------------------
WhatsApp does not let a business send arbitrary text to whoever it likes. There
are exactly two shapes an outbound message can take, and which one is legal
depends on the recipient, not on you:

    SESSION message    free text, any wording. Permitted ONLY within 24 hours
                       of that number's last inbound message to you.
    TEMPLATE message   a pre-registered, Meta-APPROVED body with numbered
                       placeholders. The only thing that can open a conversation.

The distinction is invisible in a demo and unavoidable in production, because
the case it governs is the ordinary one: a customer who books BY PHONE has never
messaged your WhatsApp number, so their confirmation is an opening message and
free text is refused (error 131047).

Templates are declared in `templates/whatsapp.json` rather than in code, because
the name and language code must match a registration made in Meta's dashboard
exactly, and that is a thing you edit and re-check — not a thing you deploy.
This module loads that file, validates it, and renders it into the two forms
needed: Meta's API payload, and readable text for the outbox.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TEMPLATES_PATH = Path(__file__).resolve().parent / "templates" / "whatsapp.json"

# Placeholder tokens Meta uses in an approved body: {{1}}, {{2}}, ...
_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")

# Meta rejects template names containing anything but lowercase, digits and
# underscore. Catching it here turns a confusing API error into a startup-time
# complaint about the line you just edited.
_VALID_NAME = re.compile(r"^[a-z0-9_]+$")


class TemplateError(ValueError):
    """A template declaration that Meta would reject, caught locally."""


@dataclass(frozen=True)
class Template:
    """One declared template, already validated."""

    key: str            # the message KIND this template serves
    name: str           # template name as registered with Meta
    language: str       # language code as registered with Meta
    body_text: str      # approved body, with {{n}} placeholders
    variables: list[str]  # value names filling {{1}}, {{2}}, ... in order

    def values_in_order(self, values: dict[str, Any]) -> list[str]:
        """Resolve this template's variables against a values dict, in order.

        A missing value becomes an empty string rather than raising: Meta
        rejects a parameter count that disagrees with the registered template,
        so dropping one would fail the whole send. An empty slot is a cosmetic
        problem; a wrong count is a delivery failure.
        """
        out = []
        for var in self.variables:
            value = values.get(var)
            if value is None or str(value).strip() == "":
                log.warning(
                    "Template %r has no value for %r — sending an empty slot",
                    self.name, var,
                )
                value = ""
            out.append(str(value))
        return out

    def render(self, values: dict[str, Any]) -> str:
        """The body as the customer will read it. For the outbox, not the API.

        Meta renders the approved body itself from the parameters we send, so
        this is a faithful local preview — which is the only way the dashboard
        can show what was delivered.
        """
        resolved = self.values_in_order(values)
        return _PLACEHOLDER.sub(
            lambda m: resolved[int(m.group(1)) - 1]
            if 0 < int(m.group(1)) <= len(resolved) else m.group(0),
            self.body_text,
        )

    def components(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        """The `components` array of Meta's template payload."""
        resolved = self.values_in_order(values)
        if not resolved:
            return []
        return [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": v} for v in resolved],
            }
        ]


def _parse(key: str, raw: dict[str, Any]) -> Template:
    """Validate one declaration, or explain precisely what is wrong with it."""
    name = str(raw.get("name") or "").strip()
    language = str(raw.get("language") or "").strip()
    body_text = str(raw.get("body_text") or "")
    variables = list(raw.get("variables") or [])

    if not name:
        raise TemplateError(f"{key}: 'name' is required.")
    if not _VALID_NAME.match(name):
        raise TemplateError(
            f"{key}: template name {name!r} is invalid. Meta allows only "
            "lowercase letters, digits and underscores."
        )
    if not language:
        raise TemplateError(
            f"{key}: 'language' is required — the locale code you registered, "
            "e.g. en_US."
        )

    # The placeholder count must match the declared variables, or Meta rejects
    # the send for a parameter-count mismatch. Checking here means the mistake
    # surfaces when you edit the file, not on a live customer's confirmation.
    indexes = {int(n) for n in _PLACEHOLDER.findall(body_text)}
    if indexes:
        highest = max(indexes)
        if indexes != set(range(1, highest + 1)):
            raise TemplateError(
                f"{key}: body_text placeholders must run 1..N with no gaps; "
                f"found {sorted(indexes)}."
            )
        if highest != len(variables):
            raise TemplateError(
                f"{key}: body_text uses {highest} placeholder(s) but "
                f"{len(variables)} variable(s) are declared "
                f"({', '.join(variables) or 'none'}). They must agree."
            )
    elif variables:
        raise TemplateError(
            f"{key}: {len(variables)} variable(s) declared but body_text has no "
            "{{n}} placeholders."
        )

    return Template(
        key=key,
        name=name,
        language=language,
        body_text=body_text,
        variables=[str(v) for v in variables],
    )


def load(path: Path | str | None = None) -> dict[str, Template]:
    """
    Read and validate the template declarations.

    A missing file is NOT an error — it means no templates are declared, which
    is the correct state until you have registered one with Meta. Everything
    keeps working inside the 24-hour window; only out-of-window sends are
    blocked, and they say so.
    """
    target = Path(path or TEMPLATES_PATH)
    if not target.exists():
        log.debug("No WhatsApp template config at %s", target)
        return {}

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Loud, but not fatal: a broken template file must not stop the agent
        # taking bookings. Out-of-window confirmations degrade to 'blocked',
        # which is visible in the outbox.
        log.error("Could not read %s: %s", target, exc)
        return {}

    templates: dict[str, Template] = {}
    for key, spec in raw.items():
        if key.startswith("_") or not isinstance(spec, dict):
            continue  # `_README` and friends
        try:
            templates[key] = _parse(key, spec)
        except TemplateError as exc:
            log.error("Ignoring template %r: %s", key, exc)
    return templates


def get(kind: str, path: Path | str | None = None) -> Template | None:
    """The template registered for this message kind, if there is one."""
    return load(path).get(kind)


def describe() -> list[dict[str, Any]]:
    """Declared templates, for the dashboard and the doctor command."""
    return [
        {
            "kind": t.key,
            "name": t.name,
            "language": t.language,
            "variables": t.variables,
        }
        for t in load().values()
    ]
