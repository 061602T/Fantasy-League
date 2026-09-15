"""Backtest the weekly scoring cycle over the completed 2025 season.

Takes a consistent snapshot of the live league (via the backup helper -- never
touches the real DB), then scores the drafted rosters across NFL weeks 1..N of
the finished 2025 season and prints the final standings. This exercises the full
multi-week cycle end-to-end on real data now, before 2026 has played out.

No API calls. Usage:
    python -m scripts.backtest_2025 [--db PATH] [--weeks N]
"""
import argparse
import sys, os, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import backup, config, db, season, store, projections
from scripts.run_week import print_standings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--weeks", type=int, default=config.REGULAR_SEASON_WEEKS)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No league DB at {args.db} -- run gen_personas + run_draft first.")
        return 1

    tmpdir = tempfile.mkdtemp(prefix="ffl-backtest-")
    snap = backup.backup_db(db_path=args.db, backup_dir=tmpdir, retain_days=0,
                            quiet=True)
    print(f"Backtesting on a snapshot copy (real DB untouched): {snap}\n")

    conn = db.connect(snap)
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] < config.NUM_TEAMS:
        print("Snapshot has no league.")
        return 1

    # Real 2025 weekly scores (build_game_logs spans the backfill season).
    store.store_weekly_scores(conn, projections.build_game_logs())
    season.build_schedule(conn, weeks=args.weeks, force=True)

    scored = 0
    for wk in range(1, args.weeks + 1):
        have = conn.execute(
            "SELECT COUNT(*) FROM player_weekly_scores WHERE season=2025 AND week=?",
            (wk,)).fetchone()[0]
        if have == 0:
            continue
        season.score_week(conn, wk, season=2025)
        scored += 1

    print(f"Scored {scored} weeks of the 2025 season on the drafted rosters.")
    print_standings(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
