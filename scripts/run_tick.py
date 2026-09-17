"""Run the league tick loop.

One tick refreshes real NFL results, scores any newly-completed week (with
waivers + group-chat reactions), and regenerates the dashboard. Idle ticks just
refresh the dashboard, so this is safe to run on a schedule.

Usage:
    python -m scripts.run_tick --db league.db                 # one tick, exit
    python -m scripts.run_tick --db league.db --loop          # run forever
    python -m scripts.run_tick --db league.db --loop --interval 3600

Deploy on the Pi either way:
  - cron (hourly):   0 * * * * cd /home/pi/Fantasy-League && \
      FFL_DB_PATH=/home/pi/ffl-data/league.db /usr/bin/python3 -m scripts.run_tick
  - or a systemd service running with --loop.
"""
import argparse
import sys, os, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, tick


def one_tick(conn, no_refresh, db_path, decisions=True):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    result = tick.run_tick(conn, refresh=not no_refresh, db_path=db_path,
                           do_midweek=decisions, do_governance=decisions)
    line = f"[{stamp}] {result['status']}"
    if result.get("weeks_scored"):
        line += f" -- scored {result['weeks_scored']}"
    print(line)
    for e in result.get("events", []):
        print(f"    - {e}")
    if result.get("dashboard"):
        print(f"    dashboard: {result['dashboard']}")
    if result.get("digest"):
        print(f"    digest:    {result['digest']}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--interval", type=int, default=config.TICK_INTERVAL_SECONDS)
    ap.add_argument("--no-refresh", action="store_true",
                    help="skip re-downloading current-season data this tick")
    ap.add_argument("--no-decisions", action="store_true",
                    help="mechanics only: score + waivers + week reactions, but "
                         "skip the ongoing GM decisions (mid-week trades and "
                         "governance) -- use when those run on a separate loop, "
                         "e.g. the 15-min run_chat_tick")
    args = ap.parse_args()

    # Integrity gate: verify the DB (restoring the newest good backup if it is
    # corrupt) before opening it. Runs once at startup, before the loop.
    try:
        status = db.preflight(args.db)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    if status.startswith("restored:"):
        print(f"NOTE: database was corrupt on startup; restored from "
              f"{status.split(':', 1)[1]}")

    conn = db.init_db(args.db)
    decisions = not args.no_decisions
    if not args.loop:
        result = one_tick(conn, args.no_refresh, args.db, decisions)
        return 0 if result["status"] != "no_league" else 1

    print(f"Tick loop started (every {args.interval}s). Ctrl-C to stop.")
    try:
        while True:
            one_tick(conn, args.no_refresh, args.db, decisions)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Tick loop stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
