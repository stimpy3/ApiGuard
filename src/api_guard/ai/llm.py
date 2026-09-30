"""Every model call in api-guard goes through here.

Before this module, explain.py, graph.py and agent.py each built their own
Groq client, loaded the key their own way, chose their own retries and carried
their own copy of the project's rules in their prompts — and the copies had
already drifted (one prompt said a field demotion needed no waiver). Now there
is one of each.

**Roles, not models.** Callers ask for a role — `triage`, `explain`, `agent` —
and this module maps it to a model. Each role keeps its own setting so the
small free model does the cheap work and the larger one only writes prose.

**Free, and degrading cleanly.** The provider is Groq's free tier. When a model
is rate-limited even after the client's own retries, the call is tried once on
the *other* Groq model: same key, but Groq's limits are per model, so it has its
own budget. If that also fails, callers get None and fall back to the
rule-based facts. An AI failure never reaches the verdict.

**Provider-neutral shape.** Everything returned is a LangChain chat model, and
the tools reach the agent through MCP, so supporting another free provider is a
new branch in `_chat()` — the prompts, the agent loop and the tools stay as
they are.
"""

from __future__ import annotations

import os
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

SMALL = "openai/gpt-oss-20b"
LARGE = "openai/gpt-oss-120b"

# role -> (setting, default model, retries inside the client)
_ROLES: dict[str, tuple[str, str, int]] = {
    "triage": ("GROQ_CLASSIFY_MODEL", SMALL, 1),
    "explain": ("GROQ_MODEL", LARGE, 1),
    # An agent makes several calls in a row, so it meets per-minute limits a
    # single call does not. The client honours Groq's retry-after.
    "agent": ("GROQ_AGENT_MODEL", SMALL, 4),
}
_TIMEOUT = 45

# The one copy of the project's rules, for every prompt that gives migration
# advice. Stated rather than left to the model: small open models invent keys
# like `x-deprecated`, and a wrong rule here becomes wrong advice everywhere.
POLICY_RULES = """This project's rules for shipping a change without breaking consumers:
- To retire an ENDPOINT: set `deprecated: true` (that exact key; there is no
  `x-deprecated`) plus an `x-sunset` date, ship that, and delete it after the
  date. No waiver needed.
- A RESPONSE FIELD has no sunset. Add the replacement field first. Demoting the
  old field from required to optional IS the breaking step and needs a waiver;
  once it is optional, deleting it is free.
- A REQUEST FIELD that is required breaks every client that doesn't send it,
  whenever it happens: adding a new required field, or later making an
  optional one required, both need a waiver. Add new request fields as
  optional.
- A waiver is an entry in waivers.yaml with exactly these keys: fingerprint,
  id, path, reason, approved_by, expires. The fingerprint comes from the
  report; never make one up. Expiry at most 90 days ahead by default."""

T = TypeVar("T", bound=BaseModel)


# --- key and provider ------------------------------------------------------


def api_key() -> str | None:
    """The Groq key, loading .env if python-dotenv is installed.

    In CI the key arrives as a real environment variable from the secret store;
    .env is a local-development convenience, not the mechanism.
    """
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        return None
    return os.environ.get("GROQ_API_KEY") or None


def provider() -> str:
    return os.environ.get("AI_PROVIDER", "groq").strip().lower()


def available() -> bool:
    """True when a model can be called at all: a supported provider and a key."""
    return provider() == "groq" and api_key() is not None


def model_name(role: str) -> str:
    setting, default, _ = _ROLES[role]
    return os.environ.get(setting, default)


def _other(name: str) -> str | None:
    """The fallback model: the other size, on the same key."""
    if name == SMALL:
        return LARGE
    if name == LARGE:
        return SMALL
    return None  # a custom model name: no guess at a fallback


def _chat(name: str, retries: int) -> Any:
    if provider() != "groq":
        raise ValueError(f"AI_PROVIDER={provider()!r} is not supported; only 'groq' is")
    from langchain_groq import ChatGroq

    return ChatGroq(
        api_key=api_key(),
        model=name,
        temperature=0,
        timeout=_TIMEOUT,
        max_retries=retries,
    )


def model_for(role: str, name: str | None = None) -> Any | None:
    """A chat model for this role, or None if no model can be called."""
    if not available():
        return None
    _, _, retries = _ROLES[role]
    return _chat(name or model_name(role), retries)


def is_rate_limit(exc: BaseException) -> bool:
    return type(exc).__name__ == "RateLimitError" or "429" in str(getattr(exc, "status_code", ""))


def with_fallback(role: str, call: Callable[[Any], T], *, name: str | None = None) -> T | None:
    """Run `call(model)`; on a rate limit, try once on the other Groq model.

    Returns None when no model is available or both are rate-limited. Any other
    error is re-raised for the caller, which decides how to degrade.
    """
    if not available():
        return None
    first = name or model_name(role)
    for candidate in [first, _other(first)]:
        if candidate is None:
            continue
        try:
            return call(model_for(role, candidate))
        except Exception as exc:  # noqa: BLE001 - classified below
            if not is_rate_limit(exc):
                raise
    return None


def structured(model: Any, schema: type[T]) -> Any:
    """Structured output in the form this model handles reliably.

    gpt-oss-20b lowercases tool names in tool-calling mode ("severity" for a
    schema named Severity) and Groq rejects the call, so the gpt-oss family
    uses JSON-schema mode instead.
    """
    name = getattr(model, "model_name", "") or getattr(model, "model", "")
    method = "json_schema" if "gpt-oss" in str(name) else "function_calling"
    return model.with_structured_output(schema, method=method)


# --- keeping prompts small ---------------------------------------------------

_RANK = {"ERR": 3, "WARN": 2, "INFO": 1}


def _field(change: Any, key: str) -> Any:
    return change.get(key) if isinstance(change, dict) else getattr(change, key, None)


def compact_changes(changes: list, limit: int = 8) -> tuple[list[dict], int]:
    """The changes a prompt needs, worst first, repeats merged.

    Keeps only what the model reads: severity, operation, path, text and rule.
    Fingerprints are left out on purpose — the model is never asked to repeat
    them, since a mistyped one would silently match nothing. Changes with the
    same rule on the same operation and path are merged with a count.

    Returns (compact list, number left out because of `limit`).
    """
    merged: dict[tuple, dict] = {}
    for change in changes:
        severity = str(_field(change, "severity") or "ERR")
        key = (_field(change, "id"), _field(change, "operation"), _field(change, "path"))
        if key in merged:
            merged[key]["count"] += 1
            continue
        merged[key] = {
            "severity": severity,
            "operation": _field(change, "operation") or "",
            "path": _field(change, "path") or "",
            "text": _field(change, "text") or "",
            "id": _field(change, "id") or "",
            "count": 1,
        }
    ordered = sorted(merged.values(), key=lambda c: _RANK.get(c["severity"], 0), reverse=True)
    return ordered[:limit], max(0, len(ordered) - limit)


def format_changes(compact: list[dict], omitted: int = 0) -> str:
    lines = []
    for c in compact:
        where = f"{c['operation']} {c['path']}".strip()
        repeat = f" (x{c['count']})" if c["count"] > 1 else ""
        lines.append(f"- [{c['severity']}] {where}: {c['text']} (rule: {c['id']}){repeat}")
    if omitted:
        lines.append(f"- ... and {omitted} less severe change(s) not shown")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Rough and deliberately cheap: about four characters per token."""
    return len(text) // 4 + 1


def fit(text: str, budget_tokens: int) -> str:
    """Trim text from the end to fit a token budget, saying that it did."""
    if estimate_tokens(text) <= budget_tokens:
        return text
    keep = max(0, budget_tokens * 4 - 60)
    return text[:keep] + "\n... [trimmed to stay within the free-tier token budget]"
