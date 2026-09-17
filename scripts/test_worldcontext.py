"""Offline tests for the real-world context layer -- no API calls.

Mocks the web-search step and exercises the filter, the cache round-trip,
staleness handling, the optional prompt snippet, and graceful failure. Run:
    python -m scripts.test_worldcontext
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import worldcontext as wc


# A realistic messy model answer: bullet markers, a header line, a blank line,
# a URL line to drop, a duplicate, and an over-long line to trim.
_FAKE_SEARCH = """Here are the bullets:

NFL:
- Star RB left Sunday's game with an ankle injury, week-to-week
* Backup QB threw five touchdowns in a blowout nobody saw coming
• A last-second field goal decided the primetime game

Pop culture:
- A new sci-fi blockbuster smashed opening-weekend box office records
- Everyone online is arguing about a surprise album drop at midnight
- Star RB left Sunday's game with an ankle injury, week-to-week
- Read more at https://example.com/news
- """ + ("x" * 300)


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "world_context.json")


def test_to_bullets():
    out = wc.to_bullets(_FAKE_SEARCH)
    # Markers stripped; header/blank/URL dropped; duplicate collapsed.
    assert "Star RB left Sunday's game with an ankle injury, week-to-week" in out
    assert all("http" not in b for b in out), out
    assert "NFL:" not in out and "Pop culture:" not in out
    assert len(out) == len(set(b.lower() for b in out)), "not de-duplicated"
    assert all(len(b) <= wc.MAX_BULLET_CHARS for b in out), "over-long not trimmed"
    assert len(out) <= wc.MAX_BULLETS
    print(f"ok: to_bullets cleaned {len(out)} bullets from a messy answer")
    return out


def test_refresh_and_load_roundtrip():
    path = _tmp()
    bullets = wc.refresh(path, search_fn=lambda: _FAKE_SEARCH)
    assert bullets, "refresh returned nothing"
    # Written to disk with a timestamp, and load() reads it back.
    data = json.load(open(path, encoding="utf-8"))
    assert data["bullets"] == bullets and data["generated_at"]
    assert wc.load(path) == bullets
    print(f"ok: refresh -> write -> load round-trip ({len(bullets)} bullets)")
    return path, bullets


def test_load_is_graceful():
    # Missing file -> [].
    assert wc.load(os.path.join(tempfile.mkdtemp(), "nope.json")) == []
    # Corrupt JSON -> [] (never raises).
    bad = _tmp()
    open(bad, "w").write("{not json")
    assert wc.load(bad) == []
    # Stale cache -> [] (older than max_age_hours).
    stale = _tmp()
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    json.dump({"generated_at": old, "bullets": ["ancient news"]},
              open(stale, "w"))
    assert wc.load(stale, max_age_hours=24) == []
    # ...but readable when fresh enough.
    assert wc.load(stale, max_age_hours=72) == ["ancient news"]
    print("ok: load graceful on missing / corrupt / stale caches")


def test_refresh_failures_are_silent():
    path = wc.refresh(_tmp(), search_fn=lambda: "")          # empty answer
    assert path == []
    def boom():
        raise RuntimeError("web search not enabled")
    assert wc.refresh(_tmp(), search_fn=boom) == []          # search raised
    # A failing refresh must not clobber a good existing cache.
    good = _tmp()
    wc.refresh(good, search_fn=lambda: _FAKE_SEARCH)
    before = wc.load(good)
    wc.refresh(good, search_fn=boom)
    assert wc.load(good) == before and before, "failing refresh clobbered cache"
    print("ok: refresh fails silently and preserves the previous cache")


def test_prompt_snippet_optionality():
    path, bullets = test_refresh_and_load_roundtrip()
    # Forced on: block present, wording flags it as optional, bullets included.
    snip = wc.prompt_snippet(path, chance=1.0)
    assert snip and bullets[0] in snip
    assert "MAY" in snip and "Never force it" in snip
    # Forced off: nothing injected.
    assert wc.prompt_snippet(path, chance=0.0) == ""
    # On but no cache -> "" (chat just proceeds league-only).
    assert wc.prompt_snippet(os.path.join(tempfile.mkdtemp(), "none.json"),
                             chance=1.0) == ""
    print("ok: prompt_snippet is optional, self-labels, and empty without cache")


def main():
    test_to_bullets()
    test_refresh_and_load_roundtrip()
    test_load_is_graceful()
    test_refresh_failures_are_silent()
    test_prompt_snippet_optionality()
    print("\nALL OFFLINE WORLD-CONTEXT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
