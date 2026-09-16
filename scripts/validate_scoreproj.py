"""Validate weekly score projections against the real 2025 backtest.

Snapshots the live league (never touches the real DB), scores the drafted
rosters across the 2025 regular season, then for every team-week compares the
projected team total (recent-form estimate) to the actual points scored, and
prints error metrics. No API calls.

Usage:  python -m scripts.validate_scoreproj [--db PATH] [--weeks N]
"""
import argparse
import sys, os, tempfile
from statistics import mean, pstdev

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import backup, config, db, season, store, projections, scoreproj


def _corr(xs, ys):
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx, sy = pstdev(xs), pstdev(ys)
    return cov / (sx * sy) if sx and sy else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--weeks", type=int, default=config.REGULAR_SEASON_WEEKS)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No league DB at {args.db} -- run gen_personas + run_draft first.")
        return 1

    tmp = tempfile.mkdtemp(prefix="ffl-valproj-")
    snap = backup.backup_db(db_path=args.db, backup_dir=tmp, retain_days=0, quiet=True)
    conn = db.connect(snap)
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] < config.NUM_TEAMS:
        print("Snapshot has no league.")
        return 1

    store.store_weekly_scores(conn, projections.build_game_logs())
    season.build_schedule(conn, weeks=args.weeks, force=True)
    scored = []
    for wk in range(1, args.weeks + 1):
        if conn.execute("SELECT COUNT(*) FROM player_weekly_scores "
                        "WHERE season=2025 AND week=?", (wk,)).fetchone()[0]:
            season.score_week(conn, wk, season=2025)
            scored.append(wk)

    team_ids = [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]
    per_week = {}                      # week -> list of (proj, actual)
    for wk in scored:
        pts = {}
        for m in conn.execute(
                """SELECT home_team_id, away_team_id, home_points, away_points
                     FROM matchups WHERE week=? AND status='final'""", (wk,)):
            pts[m["home_team_id"]] = m["home_points"]
            pts[m["away_team_id"]] = m["away_points"]
        byes = scoreproj.teams_on_bye(2025, wk)
        for tid in team_ids:
            if tid not in pts:
                continue
            proj = scoreproj.project_team(conn, tid, 2025, wk, bye_teams=byes)["proj"]
            per_week.setdefault(wk, []).append((proj, pts[tid]))

    # Week 1 has no prior games (projection ~0), so report it separately and
    # base the headline metrics on weeks that actually had recent form to use.
    print(f"Projection window: {config.SCORE_PROJ_WINDOW} games "
          f"(config.SCORE_PROJ_WINDOW)\n")
    print(f"{'wk':>3}  {'n':>2}  {'MAE':>6}  {'bias':>6}  "
          f"{'mean_proj':>9}  {'mean_act':>8}")
    all_proj, all_act = [], []
    for wk in sorted(per_week):
        pairs = per_week[wk]
        proj = [p for p, _ in pairs]
        act = [a for _, a in pairs]
        mae = mean(abs(p - a) for p, a in pairs)
        bias = mean(p - a for p, a in pairs)
        print(f"{wk:>3}  {len(pairs):>2}  {mae:>6.1f}  {bias:>+6.1f}  "
              f"{mean(proj):>9.1f}  {mean(act):>8.1f}")
        if wk >= 2:                    # skip the cold-start week in the aggregate
            all_proj += proj
            all_act += act

    pairs = list(zip(all_proj, all_act))
    mae = mean(abs(p - a) for p, a in pairs)
    rmse = (mean((p - a) ** 2 for p, a in pairs)) ** 0.5
    bias = mean(p - a for p, a in pairs)
    print(f"\nAggregate over weeks 2-{max(per_week)} "
          f"({len(pairs)} team-weeks):")
    print(f"  MAE   = {mae:5.2f} pts")
    print(f"  RMSE  = {rmse:5.2f} pts")
    print(f"  bias  = {bias:+5.2f} pts (projected - actual)")
    print(f"  corr  = {_corr(all_proj, all_act):5.3f} (proj vs actual)")
    print(f"  actual spread: mean {mean(all_act):.1f}, sd {pstdev(all_act):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
