"""Validate Monte-Carlo playoff odds against the real 2025 backtest.

Scores the 2025 regular season one week at a time; after each week it computes
every team's playoff odds from that week's standings + the still-unplayed
schedule. Once the season is over we know who actually made the top-4, so we
score those in-season forecasts (Brier vs a naive baseline) and show how the
odds converge. Also times one full-season simulation. No API calls.

Usage:  python -m scripts.validate_playoffodds [--db PATH] [--weeks N]
"""
import argparse
import sys, os, tempfile, time
from statistics import fmean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import backup, config, db, season, store, projections, playoffodds, playoffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--weeks", type=int, default=config.REGULAR_SEASON_WEEKS)
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"No league DB at {args.db} -- run gen_personas + run_draft first.")
        return 1

    tmp = tempfile.mkdtemp(prefix="ffl-valpo-")
    snap = backup.backup_db(db_path=args.db, backup_dir=tmp, retain_days=0, quiet=True)
    conn = db.connect(snap)
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] < config.NUM_TEAMS:
        print("Snapshot has no league.")
        return 1

    store.store_weekly_scores(conn, projections.build_game_logs())
    season.build_schedule(conn, weeks=args.weeks, force=True)
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}

    history = {}                 # week -> {team_id: odds}
    t_week1 = None
    for wk in range(1, args.weeks + 1):
        if not conn.execute("SELECT COUNT(*) FROM player_weekly_scores "
                            "WHERE season=2025 AND week=?", (wk,)).fetchone()[0]:
            continue
        season.score_week(conn, wk, season=2025)
        if wk < args.weeks:      # odds only meaningful while games remain
            t0 = time.perf_counter()
            history[wk] = playoffodds.playoff_odds(conn, seed=wk)
            if wk == 1:
                t_week1 = time.perf_counter() - t0

    cutoff = playoffs._bracket_size()
    final_made = set(playoffs.seeds(conn))    # who actually made the top-4

    # Brier of the in-season forecasts vs the eventual outcome.
    samples = [(p, 1.0 if t in final_made else 0.0)
               for wk in history for t, p in history[wk].items()]
    brier = fmean((p - o) ** 2 for p, o in samples)
    base = cutoff / config.NUM_TEAMS          # naive constant forecast
    base_brier = fmean((base - o) ** 2 for p, o in samples)

    print(f"Playoff-odds validation on 2025 ({config.PLAYOFF_SIMS} sims/tick, "
          f"cutoff = top {cutoff} of {config.NUM_TEAMS}):\n")
    print(f"  one full-season simulation (week 1, most games): "
          f"{t_week1*1000:.0f} ms")
    print(f"  in-season forecasts scored: {len(samples)} team-weeks")
    print(f"  Brier      = {brier:.4f}   (naive {base:.2f}-flat baseline "
          f"{base_brier:.4f}; lower is better)\n")

    # Convergence: eventual playoff teams should trend to 1, the rest to 0.
    checkpoints = [w for w in (3, 6, 9, 12) if w in history]
    order = playoffs.seeds(conn) + [t for t in names if t not in final_made]
    hdr = "  ".join(f"wk{w:>2}" for w in checkpoints)
    print(f"  {'team':<20} {'made?':>5}  {hdr}")
    for t in order:
        made = "yes" if t in final_made else "no"
        cells = "  ".join(f"{history[w][t]*100:4.0f}%" for w in checkpoints)
        print(f"  {names[t][:20]:<20} {made:>5}  {cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
