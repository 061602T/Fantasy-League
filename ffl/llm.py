"""Thin wrapper around the Anthropic SDK for the league's AI agents.

Two model tiers (see README, "Model tiers"):
  MODEL_GATE     - Haiku 4.5, the cheap, frequent "do you want to act?" check.
  MODEL_DECISION - Sonnet 5, the real decisions (personas, draft, trades, chat).

The client reads ANTHROPIC_API_KEY from the environment. As a convenience it
will also pull the key from a local ``.env`` file (which is gitignored), so a
human can drop the key next to the code without exporting it. If no key is
found, the error is raised at *call* time with a clear message -- importing
this module never needs a key, so the offline logic tests still run.
"""
from __future__ import annotations

import json
import os

# Model ids are pinned in the README's "Decisions locked in" section.
MODEL_GATE = "claude-haiku-4-5"
MODEL_DECISION = "claude-sonnet-5"

_client = None
_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """Best-effort load of a gitignored .env, only if the key isn't already set."""
    global _dotenv_loaded
    if _dotenv_loaded or os.environ.get("ANTHROPIC_API_KEY"):
        _dotenv_loaded = True
        return
    try:
        from dotenv import load_dotenv  # transitive dep; optional
        load_dotenv()
    except Exception:
        pass
    _dotenv_loaded = True


def client():
    """Return a lazily-built, shared Anthropic client.

    Raises a friendly RuntimeError if no API key is configured, rather than the
    SDK's less obvious "Could not resolve authentication method".
    """
    global _client
    if _client is None:
        import anthropic

        _load_dotenv_once()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, or put it in a local "
                ".env file (gitignored): ANTHROPIC_API_KEY=sk-ant-...\n"
                "The league's agents need it to make real Claude API calls."
            )
        _client = anthropic.Anthropic()
    return _client


def _text_of(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def chat_text(system: str, user: str, *, model: str = MODEL_DECISION,
              max_tokens: int = 1024, temperature: float = 1.0) -> str:
    """Single-turn completion returning plain text."""
    resp = client().messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        system=system, messages=[{"role": "user", "content": user}],
    )
    return _text_of(resp).strip()


def chat_json(system: str, user: str, *, model: str = MODEL_DECISION,
              max_tokens: int = 1024, temperature: float = 1.0) -> dict:
    """Single-turn completion that returns a parsed JSON object.

    Uses an assistant "{" prefill so the model is forced to emit JSON, then
    extracts the first balanced object (models sometimes trail commentary).
    Retries once with a stricter nudge if the first parse fails.
    """
    def _call(extra_system: str = "") -> str:
        resp = client().messages.create(
            model=model, max_tokens=max_tokens, temperature=temperature,
            system=system + extra_system,
            messages=[
                {"role": "user", "content": user},
                {"role": "assistant", "content": "{"},
            ],
        )
        return "{" + _text_of(resp)

    raw = _call()
    try:
        return json.loads(_extract_first_json_object(raw) or raw)
    except (json.JSONDecodeError, TypeError):
        raw = _call("\n\nReturn ONLY a single valid JSON object, nothing else.")
        return json.loads(_extract_first_json_object(raw) or raw)


def _extract_first_json_object(s: str):
    """Return the substring of the first balanced, top-level {...} object.

    String-aware, so braces inside quoted values don't confuse the balance
    count. Returns None if no complete object is found.
    """
    depth = 0
    in_str = False
    esc = False
    start = None
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return s[start:i + 1]
    return None
