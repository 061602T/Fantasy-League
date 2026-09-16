"""Validate matchup win probability against the real 2025 backtest.

Snapshots the live league, scores the 2025 regular season, then for every
matchup computes the pre-game win probability from *earlier* weeks only and
scores those forecasts: Brier score and log loss vs a coin-flip baseline,
favourite accuracy, and a reliability (calibration) table. No API calls.

Usage:  python -m scripts.validate_winprob [--db PATH] [--weeks N]
"""
import argparse
import sys, os, tempfile, math
from statistics import fmean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import backup, config, db, season, store, projections, winprob


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--weeks", type=int, default=config.REGULAR_SEASON_WEEKS)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No league DB at {args.db} -- run gen_personas + run_draft first.")
        return 1

    tmp = tempfile.mkdtemp(prefix="ffl-valwp-")
    snap = backup.backup_db(db_path=args.db, backup_dir=tmp, retain_days=0, quiet=True)
    conn = db.connect(snap)
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] < config.NUM_TEAMS:
        print("Snapshot has no league.")
        return 1

    store.store_weekly_scores(conn, projections.build_game_logs())
    season.build_schedule(conn, weeks=args.weeks, force=True)
    for wk in range(1, args.weeks + 1):
        if conn.execute("SELECT COUNT(*) FROM player_weekly_scores "
                        "WHERE season=2025 AND week=?", (wk,)).fetchone()[0]:
            season.score_week(conn, wk, season=2025)

    # Forecast every matchup from weeks strictly before it, then read the result.
    samples = []                      # (p_home_win, outcome in {1,0,0.5})
    for wk in range(2, args.weeks + 1):   # wk1 has no prior history
        wps = {(r["home_team_id"], r["away_team_id"]): r
               for r in winprob.matchup_winprobs(conn, wk)}
        for m in conn.execute(
                """SELECT home_team_id, away_team_id, home_points, away_points,
                          winner_team_id FROM matchups
                    WHERE week=? AND status='final'""", (wk,)):
            r = wps.get((m["home_team_id"], m["away_team_id"]))
            if not r:
                continue
            if m["winner_team_id"] is None:
                outcome = 0.5
            else:
                outcome = 1.0 if m["winner_team_id"] == m["home_team_id"] else 0.0
            samples.append((r["home_wp"], outcome))

    n = len(samples)
    brier = fmean((p - o) ** 2 for p, o in samples)
    base_brier = fmean((0.5 - o) ** 2 for p, o in samples)
    eps = 1e-9
    logloss = fmean(-(o * math.log(max(p, eps)) + (1 - o) * math.log(max(1 - p, eps)))
                    for p, o in samples)
    base_ll = -math.log(0.5)
    # Favourite accuracy (ties count as half-right).
    hit = fmean((1.0 if (p > 0.5 and o == 1) or (p < 0.5 and o == 0) else
                 0.5 if p == 0.5 or o == 0.5 else 0.0) for p, o in samples)

    print(f"Win-probability validation on 2025 ({n} regular-season matchups, "
          f"weeks 2-{args.weeks}):\n")
    print(f"  Brier      = {brier:.4f}   (coin-flip baseline {base_brier:.4f}; "
          f"lower is better)")
    print(f"  log loss   = {logloss:.4f}   (coin-flip baseline {base_ll:.4f})")
    print(f"  favourite hit-rate = {hit:.3f}")

    # Reliability: fold to the favourite's probability and bucket it.
    print("\n  Calibration (by favourite's win prob):")
    print(f"  {'bucket':>10}  {'n':>3}  {'pred':>5}  {'actual':>6}")
    folded = [(max(p, 1 - p), (1.0 if (p >= 0.5 and o == 1) or (p < 0.5 and o == 0)
                               else 0.5 if o == 0.5 else 0.0))
              for p, o in samples]
    for lo, hi in [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]:
        bucket = [(p, w) for p, w in folded if lo <= p < hi]
        if not bucket:
            continue
        pred = fmean(p for p, _ in bucket)
        act = fmean(w for _, w in bucket)
        print(f"  {lo:.1f}-{hi if hi <= 1 else 1.0:.1f}  {len(bucket):>7}  "
              f"{pred:>5.2f}  {act:>6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
