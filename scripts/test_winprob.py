"""Offline tests for matchup win probability -- no API calls.

Checks the normal-approximation math, the thin-history fallbacks, the
history-gathering (regular season only, strictly-before-week), and the matchup
roll-up. Run:  python -m scripts.test_winprob
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, season, winprob


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


def test_league_prior():
    # prior_mean = mean of team means; prior_sd = pooled sd (>=2-game teams).
    pm, ps = winprob.league_prior({1: [100, 120], 2: [90, 110, 100], 3: [50]})
    assert abs(pm - (110 + 100 + 50) / 3) < 1e-9, pm
    assert abs(ps - (150 ** 0.5)) < 1e-9, ps       # sqrt(mean(var=200, var=100))
    # No usable history -> mean 0, sd the configured default.
    assert winprob.league_prior({1: [], 2: []}) == (0.0, config.WINPROB_DEFAULT_SD)
    print("ok: league_prior (mean of means; pooled sd, default when thin)")


def test_team_dist_shrinkage():
    prior = (100.0, 20.0)                           # (prior_mean, prior_sd), K=4
    # No games -> None (caller supplies the default).
    assert winprob.team_dist([], prior) is None
    # 1 game: mean shrunk toward the prior (n/(n+K) = 1/5 weight on the team),
    # sd = prior_sd inflated by the mean-estimate uncertainty sqrt(1 + 1/(n+K)).
    mu, sd = winprob.team_dist([120], prior)
    assert abs(mu - (0.2 * 120 + 0.8 * 100)) < 1e-9, mu           # 104.0
    assert abs(sd - (400 * (1 + 1 / 5)) ** 0.5) < 1e-6, sd        # sqrt(480)
    # A single big week does NOT read as a lock: 140 pulls only to 108.
    assert winprob.team_dist([140], prior)[0] == 0.2 * 140 + 0.8 * 100
    # Shrinkage fades as games accumulate: same average, more games -> closer to it.
    mu1 = winprob.team_dist([140], prior)[0]
    mu8 = winprob.team_dist([140] * 8, prior)[0]
    assert mu8 > mu1 and mu8 < 140, (mu1, mu8)
    # A tight 2-game sample stays floored at the prior spread (not overconfident);
    # a wide one raises it above the prior.
    tight = winprob.team_dist([95, 105], prior)[1]
    wide = winprob.team_dist([60, 140], prior)[1]
    assert tight >= 20.0 and wide > tight, (tight, wide)
    print("ok: team_dist shrinkage (mean->prior, sd floor + inflation, fades with n)")


def _one_game_league(conn):
    """8 teams, a full schedule, only Week 1 played (one score per team)."""
    for slot in range(1, 9):
        conn.execute("INSERT INTO teams(team_name, gm_name, draft_slot) "
                     "VALUES(?,?,?)", (f"T{slot}", f"GM{slot}", slot))
    conn.commit()
    season.build_schedule(conn)
    ids = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]
    scores = dict(zip(ids, [139.5, 88.2, 121.0, 95.7, 110.3, 102.8, 130.1, 76.4]))
    for m in conn.execute("SELECT matchup_id, home_team_id, away_team_id "
                          "FROM matchups WHERE week=1").fetchall():
        h, a = m["home_team_id"], m["away_team_id"]
        hp, ap = scores[h], scores[a]
        win = h if hp > ap else (a if ap > hp else None)
        conn.execute("UPDATE matchups SET home_points=?, away_points=?, "
                     "winner_team_id=?, status='final' WHERE matchup_id=?",
                     (hp, ap, win, m["matchup_id"]))
    conn.commit()
    season.recompute_standings(conn)
    return ids, scores


def test_one_game_history_not_overconfident():
    """A single game per team must not produce a near-certain Week-2 matchup."""
    conn = db.init_db(":memory:")
    _one_game_league(conn)
    rows = winprob.matchup_winprobs(conn, week=2)
    assert rows, "no week-2 matchups"
    for r in rows:
        assert r["n_home"] == 1 and r["n_away"] == 1, r
        assert config.PRED_PROB_CAP_LO <= r["home_wp"] <= config.PRED_PROB_CAP_HI
        # One game is weak evidence -> nothing should be lopsided.
        assert 0.25 < r["home_wp"] < 0.75, r
        assert abs(r["home_wp"] + r["away_wp"] - 1.0) < 1e-9
    spread = ", ".join(f"{r['home_wp']:.2f}" for r in rows)
    print(f"ok: 1-game history -> near coin-flip week-2 win probs ({spread})")


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
    test_league_prior()
    test_team_dist_shrinkage()
    test_pooled_sd()
    test_team_weekly_scores_scope()
    test_matchup_winprobs()
    test_one_game_history_not_overconfident()
    print("\nALL OFFLINE WIN-PROBABILITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
