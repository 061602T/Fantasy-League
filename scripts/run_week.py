"""Score one league week against real NFL data, and print results + standings.

League week N == NFL week N. Defaults to the current season (2026); only weeks
that already have real results can be scored (today: week 1). No API calls --
lineups are set deterministically by projection and scored from real per-player
weekly points, so this is cheap and reproducible.

Usage:
    python -m scripts.run_week --week N [--season YEAR] [--db PATH] [--no-sync]
"""
import argparse
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, season, store, projections


def print_results(conn, results):
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    print("  Matchups:")
    for r in results:
        h, a = r["home_team_id"], r["away_team_id"]
        hp, ap = r["home_points"], r["away_points"]
        win = r["winner_team_id"]
        tag = "TIE" if win == 0 else f"{names[win]} win"
        print(f"    {names[h]:<20} {hp:6.2f}  vs  {ap:6.2f} {names[a]:<20}  [{tag}]")


def print_standings(conn):
    print("\n  Standings (W-L-T, PF, PA):")
    for i, t in enumerate(season.standings(conn), 1):
        print(f"    {i}. {t['team_name']:<20} "
              f"{t['wins']}-{t['losses']}-{t['ties']:<2}  "
              f"PF {t['points_for']:7.2f}  PA {t['points_against']:7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--season", type=int, default=config.SEASON)
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--no-sync", action="store_true",
                    help="skip refreshing real weekly scores before scoring")
    args = ap.parse_args()

    conn = db.init_db(args.db)
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] < config.NUM_TEAMS:
        print("No league found -- run gen_personas + run_draft first.")
        return 1

    if not args.no_sync:
        print("Refreshing real weekly scores from nflverse ...")
        n = store.store_weekly_scores(conn, projections.build_game_logs())
        print(f"  {n} player-week scores available.")

    have = conn.execute(
        "SELECT COUNT(*) FROM player_weekly_scores WHERE season=? AND week=?",
        (args.season, args.week)).fetchone()[0]
    if have == 0:
        print(f"No real results for {args.season} week {args.week} yet "
              "-- that week hasn't been played.")
        return 1

    season.build_schedule(conn)  # idempotent
    print(f"\nScoring {args.season} week {args.week} ...")
    results = season.score_week(conn, args.week, season=args.season)
    conn.execute("UPDATE league SET current_week=?, status='regular' WHERE id=1",
                 (args.week,))
    conn.commit()

    print_results(conn, results)
    print_standings(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
