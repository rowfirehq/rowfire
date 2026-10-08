"""Value substitution for connector templates.

Deliberately *not* an expression language.

A template here is pointed at customer data and produces an outbound HTTP
request. Handing that a full templating engine -- Jinja and friends -- means
arbitrary evaluation on the path between someone's database and the public
internet, and those sandboxes are routinely escaped. The same reasoning that
put an AST check on the `when` clause applies with more force here, because
this end actually sends.

So the whole language is `{{ name }}` and `{{ a.b }}`: look up a key, insert
its value. No calls, no arithmetic, no attribute traversal into Python
objects, no filters. If something cannot be expressed, that is the intended
outcome rather than a gap to be widened later.
"""

from __future__ import annotations

import re
from typing import Any

# A name, optionally dotted. Nothing else is a valid placeholder.
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s*\}\}")

MAX_RENDERED_BYTES = 256 * 1024


class TemplateError(Exception):
    """Raised when a template refers to something the context does not have."""


def resolve(context: dict[str, Any], path: str) -> Any:
    """Walk a dotted path through plain dicts only.

    Restricted to mappings on purpose: allowing attribute access would let a
    template reach into Python objects and, from there, at anything they
    reference.
    """
    current: Any = context
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise TemplateError(f"template refers to `{path}`, which is not available")
        current = current[part]
    return current


def render_string(template: str, context: dict[str, Any]) -> Any:
    """Substitute placeholders in one string.

    A string that is *entirely* one placeholder keeps the value's type, so
    `{"total": "{{ total_amount }}"}` sends a string and a numeric field
    stays numeric rather than being stringified into JSON.
    """
    whole = PLACEHOLDER.fullmatch(template.strip())
    if whole:
        return resolve(context, whole.group(1))

    def _replace(match: re.Match[str]) -> str:
        value = resolve(context, match.group(1))
        return "" if value is None else str(value)

    return PLACEHOLDER.sub(_replace, template)


def render(value: Any, context: dict[str, Any]) -> Any:
    """Render templates anywhere inside a nested structure, keys included."""
    if isinstance(value, str):
        return render_string(value, context)
    if isinstance(value, dict):
        # Keys are rendered as well as values. An API that takes a dynamic
        # field name -- Braze custom attributes, and most "set this field"
        # endpoints -- needs {"{{ field }}": value} to mean something. Keys
        # used to pass through untouched, so such a body sent the literal text
        # `{{ field }}` as the field name and the call quietly did nothing
        # useful.
        return {_render_key(key, context): render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    return value


def _render_key(key: Any, context: dict[str, Any]) -> Any:
    """Render a mapping key, forced back to a string.

    render_string keeps the value's type when a template is the whole string,
    which is right for values and impossible for keys: a JSON object key is
    always a string.
    """
    if not isinstance(key, str):
        return key
    rendered = render_string(key, context)
    return rendered if isinstance(rendered, str) else str(rendered)


def placeholders(value: Any) -> set[str]:
    """Every name a template needs, for validating a binding up front."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(match.group(1) for match in PLACEHOLDER.finditer(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            # Keys count: they are rendered too, so a binding that does not
            # supply them would fail at send time rather than at save time.
            if isinstance(key, str):
                found.update(match.group(1) for match in PLACEHOLDER.finditer(key))
            found |= placeholders(item)
    elif isinstance(value, list):
        for item in value:
            found |= placeholders(item)
    return found
