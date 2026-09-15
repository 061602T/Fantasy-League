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

# Some hosts (e.g. Claude Code's managed runtime) reserve ANTHROPIC_API_KEY for
# their own provider auth, so a value set under that exact name may not reach
# app code. Setting the key under this alias instead is honoured here.
_KEY_ALIAS = "FFL_ANTHROPIC_API_KEY"

_client = None
_dotenv_loaded = False


def _resolve_key() -> str | None:
    """Return the API key from the standard var, the alias, or a local .env."""
    _load_dotenv_once()
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(_KEY_ALIAS)
    if key and not os.environ.get("ANTHROPIC_API_KEY"):
        # The SDK only looks at ANTHROPIC_API_KEY, so promote the alias.
        os.environ["ANTHROPIC_API_KEY"] = key
    return key


def _load_dotenv_once() -> None:
    """Best-effort load of a gitignored .env, only if no key is set yet."""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(_KEY_ALIAS):
        _dotenv_loaded = True
        return
    try:
        from dotenv import load_dotenv  # optional convenience
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

        if not _resolve_key():
            raise RuntimeError(
                "No Anthropic API key found. The agents need one for real "
                "Claude API calls. Provide it in any of these ways:\n"
                f"  - set {_KEY_ALIAS} (use this if the host reserves "
                "ANTHROPIC_API_KEY, e.g. Claude Code's cloud env),\n"
                "  - export ANTHROPIC_API_KEY, or\n"
                "  - put ANTHROPIC_API_KEY=sk-ant-... in a local .env (gitignored)."
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
