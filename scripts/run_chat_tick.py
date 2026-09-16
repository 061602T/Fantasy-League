"""Lightweight ambient-chat loop, decoupled from the scoring tick.

Produces ONLY group-chat banter -- no scoring, waivers, standings, backups,
digest, or dashboard/publish. Cheap and fast, meant to run every ~15 min via
cron so the league chat feels like an ongoing conversation rather than something
tied to the hourly scoring tick.

Each firing (most do nothing, for free):
  1. skip if a banter was posted within FFL_CHAT_TICK_COOLDOWN seconds -- keeps
     natural quiet stretches and stops it piling onto the hourly tick's own
     chat or a previous firing;
  2. clear a cheap probability pre-gate (FFL_CHAT_TICK_PROB) -- most firings
     stop here with no API call at all;
  3. only then run the Haiku chat gate; if a GM actually wants to talk, Sonnet
     writes the line(s) -- threaded (replies reference recent messages) and each
     stamped with its own staggered timestamp.

This is additive: it does not touch run_tick's scoring-event reactions or its
mid-week ambient chat. The cooldown keeps the two from generating chat in the
same few minutes.

Usage:      python -m scripts.run_chat_tick [--db PATH]
Cron (15m): */15 * * * * cd /home/pi/Fantasy-League && \
    venv/bin/python -m scripts.run_chat_tick >> ~/ffl-data/chat_tick.log 2>&1
"""
import argparse
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db
from ffl import chat as chatmod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--force", action="store_true",
                    help="bypass the cooldown and probability pre-gate (still "
                         "runs the Haiku gate); for testing the wiring")
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    if not os.path.exists(args.db):
        print(f"[{stamp}] no league db at {args.db}; nothing to do")
        return 0
    conn = db.connect(args.db)
    n = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    if n < config.NUM_TEAMS:
        print(f"[{stamp}] no league yet ({n} teams); skip")
        return 0

    if not args.force:
        age = chatmod.last_banter_age(conn)
        if age is not None and age < config.CHAT_TICK_COOLDOWN:
            print(f"[{stamp}] quiet: last banter {age:.0f}s ago "
                  f"(< {config.CHAT_TICK_COOLDOWN}s cooldown)")
            return 0
        if random.random() >= config.CHAT_TICK_PROB:
            print(f"[{stamp}] quiet: pre-gate (p={config.CHAT_TICK_PROB})")
            return 0

    try:
        posted = chatmod.ambient_exchange(conn)
    except Exception as e:  # noqa: BLE001 -- a cron loop shouldn't hard-fail
        print(f"[{stamp}] chat error: {e}", file=sys.stderr)
        return 0

    if not posted:
        print(f"[{stamp}] quiet: nobody felt like talking")
        return 0
    print(f"[{stamp}] posted {len(posted)} message(s):")
    for p in posted:
        print(f"    {p['ts'].strftime('%H:%M:%S')} {p['gm_name']}: {p['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
