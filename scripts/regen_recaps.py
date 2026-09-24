"""Rewrite every stored weekly recap in the current style, overwriting the rows.

The auto-writer (ffl/weeklysummary.py) only fills in MISSING weeks on a normal
tick, so recaps written under an older style stay as they were. This one-off
forces a rewrite of every scored week -- GM-name-driven, centered on that week's
group chat and the decisions the GMs made -- then republishes the dashboard.

Costs one model call per scored week (a handful), so run it by hand, not on a
timer.

Usage:
    python -m scripts.regen_recaps [--db PATH] [--no-publish]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, dashboard, db, ghpages, weeklysummary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--no-publish", action="store_true",
                    help="don't refresh/publish the dashboard afterward")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no league db at {args.db}; nothing to do")
        return 1
    conn = db.connect(args.db)

    weeks = weeklysummary.scored_weeks(conn)
    if not weeks:
        print("no scored weeks yet; nothing to regenerate")
        return 0
    print(f"regenerating {len(weeks)} weekly recap(s) for weeks "
          f"{', '.join(map(str, weeks))} (one model call each)...")
    written = weeklysummary.ensure_all(conn, force=True)
    print(f"rewrote weeks: {', '.join(map(str, written)) or '(none)'}")

    if not args.no_publish and written:
        try:
            dash = dashboard.write(conn)
            pub = ghpages.publish(dash)
            print(f"dashboard: {pub['status']}"
                  + (f" -- {pub.get('error')}" if pub.get("error") else ""))
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: dashboard refresh/publish failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
