"""Offline tests for matchup win probability -- no API calls.

Checks the normal-approximation math, the thin-history fallbacks, the
history-gathering (regular season only, strictly-before-week), and the matchup
roll-up. Run:  python -m scripts.test_winprob
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, winprob


def test_normal_cdf():
    assert abs(winprob._normal_cdf(0.0) - 0.5) < 1e-9
    assert winprob._normal_cdf(6) > 0.999999
    assert winprob._normal_cdf(-6) < 1e-6
    # Symmetry.
    assert abs(winprob._normal_cdf(1.0) + winprob._normal_cdf(-1.0) - 1.0) < 1e-9
    print("ok: _normal_cdf (0->0.5, tails, symmetry)")


def test_win_probability_math():
    # Equal distributions -> coin flip.
    assert winprob.win_probability((100, 20), (100, 20)) == 0.5
    # Higher mean -> favoured, and a known closed-form value:
    # z = (110-100)/sqrt(20^2+20^2) = 0.35355 -> Phi = 0.63816.
    p = winprob.win_probability((110, 20), (100, 20))
    assert abs(p - 0.63816) < 1e-3, p
    assert p > 0.5 and winprob.win_probability((100, 20), (110, 20)) < 0.5
    # Missing history on either side -> 50/50.
    assert winprob.win_probability(None, (100, 20)) == 0.5
    # Zero combined spread decides by mean.
    assert winprob.win_probability((110, 0), (100, 0)) == 1.0
    assert winprob.win_probability((100, 0), (100, 0)) == 0.5
    print("ok: win_probability (coin flip, closed-form value, fallbacks)")


def test_team_dist_fallbacks():
    from ffl import config
    # >=2 games with sample sd ABOVE the floor -> sample sd used.
    d = winprob.team_dist([60, 140], pooled=10.0)          # sample sd = sqrt(3200)
    assert d[0] == 100.0 and abs(d[1] - (3200 ** 0.5)) < 1e-9, d
    # >=2 games but a tight sample -> sd floored at the pooled league sd.
    dt = winprob.team_dist([95, 105], pooled=20.0)          # sample sd = sqrt(50) < 20
    assert dt == (100.0, 20.0), dt
    # 1 game -> mean is that score, sd is the pooled floor.
    assert winprob.team_dist([105], pooled=12.0) == (105, 12.0)
    # 1 game, no pooled -> configured default sd.
    assert winprob.team_dist([105], pooled=None) == (105, config.WINPROB_DEFAULT_SD)
    # No games -> None.
    assert winprob.team_dist([], pooled=10) is None
    # Degenerate all-equal history (sample sd 0) -> pooled floor.
    assert winprob.team_dist([80, 80, 80], pooled=15.0) == (80.0, 15.0)
    print("ok: team_dist (sample sd, pooled floor, thin-history/degenerate fallbacks)")


def test_pooled_sd():
    # team1 var([100,120])=200; team2 var([90,110,100])=100 -> sqrt(mean)=sqrt150.
    pooled = winprob.pooled_sd({1: [100, 120], 2: [90, 110, 100], 3: [50]})
    assert abs(pooled - (150 ** 0.5)) < 1e-9, pooled
    # Nobody with >=2 games -> None.
    assert winprob.pooled_sd({1: [100], 2: []}) is None
    print("ok: pooled_sd (root-mean of per-team variances, None when thin)")


def _seed_two_teams(conn):
    for slot in (1, 2):
        conn.execute("""INSERT INTO teams(team_name, gm_name, draft_slot)
                        VALUES(?,?,?)""", (f"T{slot}", f"GM{slot}", slot))
    return [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]


def test_team_weekly_scores_scope():
    conn = db.init_db(":memory:")
    a, b = _seed_two_teams(conn)
    # Weeks 1-2 final, a playoff week 15 final, and a scheduled week 3.
    conn.execute("""INSERT INTO matchups(week,home_team_id,away_team_id,home_points,
                    away_points,winner_team_id,status) VALUES(1,?,?,120,90,?, 'final')""",
                 (a, b, a))
    conn.execute("""INSERT INTO matchups(week,home_team_id,away_team_id,home_points,
                    away_points,winner_team_id,status) VALUES(2,?,?,80,110,?, 'final')""",
                 (b, a, a))
    conn.execute("""INSERT INTO matchups(week,home_team_id,away_team_id,home_points,
                    away_points,winner_team_id,status) VALUES(15,?,?,130,120,?, 'final')""",
                 (a, b, a))  # playoff week -> excluded (> REGULAR_SEASON_WEEKS)
    conn.execute("""INSERT INTO matchups(week,home_team_id,away_team_id,status)
                    VALUES(3,?,?, 'scheduled')""", (a, b))
    conn.commit()

    allw = winprob.team_weekly_scores(conn)
    # a: wk1 home 120, wk2 away 110; wk15 (130) is a playoff week and excluded.
    assert sorted(allw[a]) == [110.0, 120.0], allw[a]
    assert sorted(allw[b]) == [80.0, 90.0], allw[b]           # b: wk1 away 90, wk2 home 80
    before3 = winprob.team_weekly_scores(conn, before_week=3)
    assert sorted(before3[a]) == [110.0, 120.0] and sorted(before3[b]) == [80.0, 90.0]
    print("ok: team_weekly_scores (regular season only, strictly-before-week)")


def test_matchup_winprobs():
    conn = db.init_db(":memory:")
    a, b = _seed_two_teams(conn)
    # a scores high, b scores low over weeks 1-2; they meet in week 3.
    conn.execute("""INSERT INTO matchups(week,home_team_id,away_team_id,home_points,
                    away_points,winner_team_id,status) VALUES(1,?,?,130,95,?, 'final')""",
                 (a, b, a))
    conn.execute("""INSERT INTO matchups(week,home_team_id,away_team_id,home_points,
                    away_points,winner_team_id,status) VALUES(2,?,?,120,100,?, 'final')""",
                 (a, b, a))
    conn.execute("""INSERT INTO matchups(week,home_team_id,away_team_id,status)
                    VALUES(3,?,?, 'scheduled')""", (a, b))
    conn.commit()

    rows = winprob.matchup_winprobs(conn, 3)
    assert len(rows) == 1
    r = rows[0]
    assert r["n_home"] == 2 and r["n_away"] == 2
    assert r["home_wp"] > 0.5                       # a is the stronger team
    assert abs(r["home_wp"] + r["away_wp"] - 1.0) < 1e-9
    print("ok: matchup_winprobs (favours stronger team, probs sum to 1)")


def main():
    test_normal_cdf()
    test_win_probability_math()
    test_team_dist_fallbacks()
    test_pooled_sd()
    test_team_weekly_scores_scope()
    test_matchup_winprobs()
    print("\nALL OFFLINE WIN-PROBABILITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
