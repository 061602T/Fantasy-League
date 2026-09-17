"""Cached real-world context the GMs can *occasionally* reference in chat.

A separate, infrequent job (``scripts.refresh_context``, cron'd ~every 4h) does
a couple of web searches -- current NFL news and general trending pop-culture /
news -- via the Anthropic server-side ``web_search`` tool, distills the results
to a handful of short bullets, and caches them to a small JSON file. The two
chat-generation paths (``ffl.chat._compose`` for the hourly tick's event
reactions and ``ffl.chat._ambient_line`` for the 15-min ambient loop) read the
cache through :func:`prompt_snippet` as *optional* flavor.

Design guarantees:
  * **Never blocks or errors chat.** Every read path swallows its own failures
    and returns "" / [], so a missing, stale, malformed, or unreadable cache
    just means the GMs talk about the league only.
  * **Occasional, not constant.** :func:`prompt_snippet` shows the context to
    the model on only a fraction of messages (``config.CONTEXT_INJECT_PROB``),
    and even then the prompt tells the GM to reference it rarely.
  * **No stale "news".** A cache older than ``config.CONTEXT_MAX_AGE_HOURS`` is
    ignored, so a wedged refresh job degrades to league-only rather than having
    GMs cite week-old events as current.

The web search runs server-side on Anthropic's infrastructure (the same API the
app already calls), so the Pi needs no new outbound domains or credentials. If
web search isn't enabled on the account, ``refresh`` fails gracefully and the
cache is simply never populated.
"""
from __future__ import annotations

import json
import os
import random as _random
from datetime import datetime, timezone

from . import config, llm

# The dynamic-filtering web-search tool; supported on Sonnet 5 (MODEL_DECISION).
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search",
                    "max_uses": 6}

MAX_BULLETS = 10
MAX_BULLET_CHARS = 160


def _path(path: str | None = None) -> str:
    return os.path.expanduser(path or config.CONTEXT_PATH)


# --- Read side (used by chat generation) -----------------------------------

def load(path: str | None = None, *,
         max_age_hours: float | None = None) -> list[str]:
    """Return the cached bullets, or ``[]`` if the cache is missing, stale, or
    unreadable. Never raises -- chat generation must proceed without it."""
    if max_age_hours is None:
        max_age_hours = config.CONTEXT_MAX_AGE_HOURS
    try:
        with open(_path(path), encoding="utf-8") as f:
            data = json.load(f)
        bullets = data.get("bullets") or []
        gen = data.get("generated_at")
        if max_age_hours and gen:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(gen)).total_seconds() / 3600.0
            if age_h > max_age_hours:
                return []
        return [b for b in bullets if isinstance(b, str) and b.strip()]
    except Exception:  # noqa: BLE001 -- any trouble => no context, never crash chat
        return []


def prompt_snippet(path: str | None = None, *, load_fn=load, rng=None,
                   chance: float | None = None) -> str:
    """An *optional* context block to append to a chat prompt, or "".

    Returns "" most of the time on purpose: with probability ``chance`` (default
    ``config.CONTEXT_INJECT_PROB``) the model doesn't even see the context, so
    the bulk of messages stay league-only. When it is shown, the wording makes
    clear it's a rare, natural aside -- not something to force into the message.
    """
    if chance is None:
        chance = config.CONTEXT_INJECT_PROB
    if (rng or _random).random() >= chance:
        return ""
    bullets = load_fn(path)
    if not bullets:
        return ""
    lines = "\n".join(f"- {b}" for b in bullets)
    return ("\n\nStuff going on in the real world right now (NFL + pop "
            "culture/news), for flavor only. You MAY drop a casual, offhand "
            "reference to ONE of these if it genuinely fits what you're already "
            "saying -- the way a real person slips a current event into chat. "
            "Usually you won't. Never force it, never list several, never sound "
            "like you're reading headlines:\n" + lines)


# --- Filter step (distill a model answer into clean bullets) ---------------

def to_bullets(text: str, *, max_bullets: int = MAX_BULLETS,
               max_chars: int = MAX_BULLET_CHARS) -> list[str]:
    """Turn the search model's free-text answer into clean, short bullets.

    Kept separate from the API call so it can be unit-tested on its own: strips
    bullet markers, drops blank lines / section headers / anything with a URL,
    trims over-long lines, caps the count, and de-duplicates.
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*•• \t").strip()
        if not line:
            continue
        if "http://" in line or "https://" in line:
            continue
        # A short trailing-colon line like "NFL:" is a header, not a fact.
        if line.endswith(":") and len(line) < 40:
            continue
        out.append(line[:max_chars].rstrip())
        if len(out) >= max_bullets:
            break
    seen: set[str] = set()
    uniq: list[str] = []
    for b in out:
        k = b.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(b)
    return uniq


# --- Write side (the refresh job) ------------------------------------------

_SEARCH_SYSTEM = (
    "You gather quick, current real-world talking points for a fantasy football "
    "group chat of wisecracking friends. Use the web_search tool to find what's "
    "happening RIGHT NOW, then distill it into short, punchy bullets someone "
    "could casually drop into a chat. Each bullet: one factual, self-contained "
    "line, roughly 6-14 words, no dates unless essential, no URLs, no citations, "
    "no hashtags. Keep it light, mainstream, and conversational. AVOID anything "
    "tragic, graphic, or genuinely sensitive (deaths, disasters, violence, "
    "partisan politics) -- this is for jokes, not a news desk.")

_SEARCH_USER = (
    "Do two searches: (1) current NFL news this week -- notable injuries, "
    "standout or terrible performances, surprising results, trades, and "
    "storylines fans are talking about; (2) general trending pop culture / "
    "entertainment / lighthearted news right now -- movies, TV, music, sports "
    "outside the NFL, viral moments, celebrities, tech, memes. Then give me "
    "8-12 total short bullets, a good mix of both, each on its own line "
    "prefixed with '- '. Just the bullets, no headers or commentary.")


def _default_search() -> str:
    """One web-search-enabled call; returns the model's concatenated text.

    Bounded continuation on ``pause_turn`` (the server-tool pause signal). Web
    search errors don't raise -- they come back as result blocks -- so if the
    search yields nothing usable the text is just empty and the caller drops it.
    """
    client = llm.client()
    messages = [{"role": "user", "content": _SEARCH_USER}]
    parts = []
    for _ in range(4):
        resp = client.messages.create(
            model=llm.MODEL_DECISION, max_tokens=1500,
            system=_SEARCH_SYSTEM, messages=messages, tools=[_WEB_SEARCH_TOOL])
        parts.append(llm._text_of(resp))
        if getattr(resp, "stop_reason", None) == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return "\n".join(p for p in parts if p)


def refresh(path: str | None = None, *, search_fn=None) -> list[str]:
    """Run the searches, distill to bullets, and write the cache. Returns the
    bullets (possibly ``[]``).

    Never raises: on any failure -- no API key, web search not enabled, network
    error, empty result -- it leaves the existing cache untouched and returns
    ``[]``. The caller (the cron script) logs; chat generation is unaffected
    either way because it reads the cache independently.
    """
    search = search_fn or _default_search
    try:
        bullets = to_bullets(search())
    except Exception:  # noqa: BLE001 -- a background job must not hard-fail
        return []
    if not bullets:
        return []
    write(bullets, path)
    return bullets


def write(bullets: list[str], path: str | None = None) -> str:
    """Atomically write the cache (temp file + rename). Returns the path."""
    p = _path(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                   "bullets": bullets}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p
