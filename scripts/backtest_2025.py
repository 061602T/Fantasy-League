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

from ffl import backup, config, db, season, store, projections, playoffs, digest, dashboard
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

    print(f"Scored {scored} weeks of the 2025 regular season on the drafted rosters.")
    print_standings(conn)

    # Playoffs: the drafted rosters play out a bracket on real 2025 weeks 15-16.
    if playoffs.regular_season_complete(conn):
        print("\n=== Playoffs ===")
        res = playoffs.advance(conn, latest_completed=18, season_year=2025)
        for e in res["events"]:
            print(f"  - {e}")
        for wk in (playoffs.week_of_round(1), playoffs.week_of_round(2)):
            names = {r["team_id"]: r["team_name"]
                     for r in conn.execute("SELECT team_id, team_name FROM teams")}
            for m in conn.execute(
                    """SELECT home_team_id, away_team_id, home_points, away_points,
                              winner_team_id FROM matchups WHERE week=? AND status='final'""",
                    (wk,)):
                w = m["winner_team_id"]
                tag = names.get(w, "?")
                print(f"    wk{wk}: {names[m['home_team_id']]} {m['home_points']:.1f} "
                      f"vs {m['away_points']:.1f} {names[m['away_team_id']]} "
                      f"→ {tag}")

    # Show the digest + dashboard this state would produce (no delivery).
    d = digest.publish(conn, path="/tmp/ffl-backtest-digest.txt", do_deliver=False)
    dash = dashboard.write(conn, path="/tmp/ffl-backtest-dashboard.html")
    print("\n=== Digest ===\n" + d["text"])
    print(f"\nDashboard written: {dash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
