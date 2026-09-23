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

from ffl import config, dashboard, db, ghpages, governance, market, weeklysummary
from ffl import chat as chatmod


def _proj_map():
    from ffl import projections
    return {r.entity_id: r.proj_ppg
            for r in projections.build_projections().itertuples(index=False)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--force", action="store_true",
                    help="bypass the chat cooldown and probability pre-gate "
                         "(still runs the Haiku gate); for testing the wiring")
    ap.add_argument("--no-publish", action="store_true",
                    help="don't refresh/publish the dashboard afterward")
    ap.add_argument("--no-governance", action="store_true",
                    help="skip the bylaw step this firing")
    ap.add_argument("--no-market", action="store_true",
                    help="skip the trade attempt this firing")
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

    # This loop carries the GMs' between-game DECISIONS as well as chat. Trades
    # and governance run independently of the chat cooldown/pre-gate (each has
    # its own gating), so bylaws keep moving and trades still happen even during
    # a chat-quiet stretch. Scoring, waivers, and playoffs stay in run_tick --
    # they're weekly, driven by real NFL weeks completing.
    events = []

    # Governance: close/vote/propose. Cheap when nothing's live; never raises.
    if not args.no_governance:
        for e in governance.step(conn):
            events.append(e)

    # Trade market: a heavier step, so only attempt one on a fraction of firings.
    if not args.no_market and random.random() < config.CHAT_TICK_TRADE_PROB:
        try:
            res = market.attempt_one_trade(conn, _proj_map())
            if res and res.get("status") == "accepted":
                events.append("mid-cycle trade completed")
        except Exception as e:  # noqa: BLE001 -- a cron loop shouldn't hard-fail
            print(f"[{stamp}] trade error: {e}", file=sys.stderr)

    # Ambient chat keeps its own cooldown + probability pre-gate.
    posted = []
    do_chat = True
    if not args.force:
        age = chatmod.last_banter_age(conn)
        if age is not None and age < config.CHAT_TICK_COOLDOWN:
            do_chat = False
        elif random.random() >= config.CHAT_TICK_PROB:
            do_chat = False
    if do_chat:
        try:
            posted = chatmod.ambient_exchange(conn)
        except Exception as e:  # noqa: BLE001 -- a cron loop shouldn't hard-fail
            print(f"[{stamp}] chat error: {e}", file=sys.stderr)
            posted = []

    # Auto-write any missing weekly recap (a newly-scored week, or a backfill of
    # earlier weeks). Cheap when caught up; never raises.
    try:
        wk = weeklysummary.ensure_all(conn)
        if wk:
            events.append("wrote weekly recap(s): " + ", ".join(f"wk{w}" for w in wk))
    except Exception as e:  # noqa: BLE001 -- must not crash the loop
        print(f"[{stamp}] weekly recap error: {e}", file=sys.stderr)

    for e in events:
        print(f"[{stamp}] {e}")
    if posted:
        print(f"[{stamp}] posted {len(posted)} message(s):")
        for p in posted:
            print(f"    {p['ts'].strftime('%H:%M:%S')} {p['gm_name']}: {p['message']}")
    if not events and not posted:
        print(f"[{stamp}] quiet: nothing happened this firing")
        return 0

    # Something changed (a bylaw moved, a trade landed, or chat posted), so
    # refresh + publish the dashboard. never crashes the loop, and publish() is a
    # no-op when Pages isn't configured or the file is unchanged.
    if not args.no_publish:
        try:
            dash = dashboard.write(conn)
            pub = ghpages.publish(dash)
            if pub["status"] == "published":
                print(f"    dashboard published to GitHub Pages")
            elif pub["status"] == "error":
                print(f"    WARNING: dashboard publish failed: {pub['error']}",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001 -- publishing must not crash the loop
            print(f"    WARNING: dashboard refresh/publish failed: {e}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
