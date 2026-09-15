"""Generate live in-character group-chat reactions to a scored week.

Requires that week to be scored (see scripts.run_week). Makes real API calls:
a Haiku gate per GM per round, plus a Sonnet line for each GM who speaks.

Usage:
    python -m scripts.run_chat --week N [--db PATH] [--rounds 2]
"""
import argparse
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, chat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()

    conn = db.init_db(args.db)
    summary = chat.week_summary(conn, args.week)
    if summary is None:
        print(f"Week {args.week} isn't scored yet -- run `python -m scripts.run_week "
              f"--week {args.week}` first.")
        return 1

    headline, detail, _ = summary
    print(f"{headline}\n{detail}\n")
    print(f"Generating group-chat reactions ({args.rounds} rounds) ...\n")
    posted = chat.react_to_week(conn, args.week, rounds=args.rounds)
    if not posted:
        print("(nobody had anything to say)")
    for p in posted:
        print(f"  {p['gm_name']}: {p['message']}")
    print(f"\n{len(posted)} messages posted to the league chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
