"""Offline tests for Monte-Carlo playoff odds -- no API calls.

Exercises the ranking/counting core directly, then the full simulation on a
synthetic league: the top-heavy field, the sum-to-cutoff invariant, seed
determinism, and the no-games-left deterministic case.
Run:  python -m scripts.test_playoffodds
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from ffl import db, playoffodds, season


def test_made_counts_core():
    # 3 teams, 2 sims, top-1. Sim0: team0 (2 wins). Sim1: team2 (2 wins).
    wins = np.array([[2, 0], [1, 1], [0, 2]], dtype=float)
    pf = np.array([[100, 100], [100, 100], [100, 100]], dtype=float)
    c = playoffodds._made_counts(wins, pf, cutoff=1)
    assert list(c) == [1, 0, 1], c
    # Tie on wins -> points_for breaks it. Both sims: all 1 win; team1 has most pf.
    wins2 = np.array([[1, 1], [1, 1], [1, 1]], dtype=float)
    pf2 = np.array([[90, 90], [120, 120], [80, 80]], dtype=float)
    c2 = playoffodds._made_counts(wins2, pf2, cutoff=1)
    assert list(c2) == [0, 2, 0], c2
    print("ok: _made_counts (top-k by wins then points_for)")


def _final(conn, week, home, away, hp, ap):
    win = home if hp > ap else away if ap > hp else None
    conn.execute("""INSERT INTO matchups(week, home_team_id, away_team_id,
                     home_points, away_points, winner_team_id, status)
                     VALUES(?,?,?,?,?,?, 'final')""", (week, home, away, hp, ap, win))


def _scheduled(conn, week, home, away):
    conn.execute("""INSERT INTO matchups(week, home_team_id, away_team_id, status)
                    VALUES(?,?,?, 'scheduled')""", (week, home, away))


def _league8(conn):
    """8 teams: 4 'good' (high scorers) beat 4 'bad' over weeks 1-2, then a
    scheduled week 3 of good-vs-good and bad-vs-bad."""
    for slot in range(1, 9):
        conn.execute("""INSERT INTO teams(team_name, gm_name, draft_slot)
                        VALUES(?,?,?)""", (f"T{slot}", f"GM{slot}", slot))
    t = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]
    good = t[:4]      # T1..T4
    bad = t[4:]       # T5..T8
    # Weeks 1-2: each good team beats a bad team 130-80.
    _final(conn, 1, good[0], bad[0], 130, 80); _final(conn, 1, good[1], bad[1], 130, 80)
    _final(conn, 1, good[2], bad[2], 130, 80); _final(conn, 1, good[3], bad[3], 130, 80)
    _final(conn, 2, good[0], bad[3], 130, 80); _final(conn, 2, good[1], bad[2], 130, 80)
    _final(conn, 2, good[2], bad[1], 130, 80); _final(conn, 2, good[3], bad[0], 130, 80)
    # Week 3 scheduled: good plays good, bad plays bad (can't reshuffle the tiers).
    _scheduled(conn, 3, good[0], good[1]); _scheduled(conn, 3, good[2], good[3])
    _scheduled(conn, 3, bad[0], bad[1]); _scheduled(conn, 3, bad[2], bad[3])
    conn.commit()
    season.recompute_standings(conn)
    return good, bad


def test_simulation_topheavy_and_invariants():
    conn = db.init_db(":memory:")
    good, bad = _league8(conn)
    odds = playoffodds.playoff_odds(conn, iterations=4000, seed=1)   # cutoff = 4

    # Probabilities sum to the cutoff (exactly 4 teams make it every sim).
    assert abs(sum(odds.values()) - 4.0) < 1e-9, sum(odds.values())
    # The four good teams are 2-0 with a big points lead over 0-2 teams; even
    # losing week 3 they out-seed every bad team -> essentially locked in.
    for g in good:
        assert odds[g] > 0.99, (g, odds[g])
    for b in bad:
        assert odds[b] < 0.01, (b, odds[b])
    print("ok: simulation (top-heavy field near-locked, odds sum to cutoff)")


def _balanced8(conn):
    """8 near-identical teams, everyone 1-1 with similar points, a scheduled
    week 3 -- so the top-4 cut is genuinely uncertain (fractional odds)."""
    for slot in range(1, 9):
        conn.execute("""INSERT INTO teams(team_name, gm_name, draft_slot)
                        VALUES(?,?,?)""", (f"T{slot}", f"GM{slot}", slot))
    t = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]
    pairs = [(0, 1), (2, 3), (4, 5), (6, 7)]
    for h, a in pairs:                       # week 1: home wins 110-100
        _final(conn, 1, t[h], t[a], 110, 100)
    for h, a in pairs:                       # week 2: away wins 110-100 -> all 1-1
        _final(conn, 2, t[h], t[a], 100, 110)
    for h, a in [(0, 2), (4, 6), (1, 3), (5, 7)]:
        _scheduled(conn, 3, t[h], t[a])
    conn.commit()
    season.recompute_standings(conn)
    return t


def test_seed_determinism():
    conn = db.init_db(":memory:")
    _balanced8(conn)
    a = playoffodds.playoff_odds(conn, iterations=2000, seed=7)
    b = playoffodds.playoff_odds(conn, iterations=2000, seed=7)
    c = playoffodds.playoff_odds(conn, iterations=2000, seed=8)
    # Fractional odds in a balanced field -> some team is strictly between 0 and 1.
    assert any(0.02 < p < 0.98 for p in a.values()), a
    assert a == b, "same seed must reproduce exactly"
    assert a != c, "different seed should give different sampled odds"
    print("ok: seed determinism (reproducible; varies with seed)")


def _one_game_season(conn):
    """8 teams, a full schedule, only Week 1 played -- a single score per team."""
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


def test_one_game_season_not_degenerate():
    """With a single game per team, small-sample shrinkage must produce a real
    spread of playoff odds -- not the 100%/0% the raw sample used to give."""
    conn = db.init_db(":memory:")
    ids, scores = _one_game_season(conn)
    odds = playoffodds.playoff_odds(conn, iterations=6000, seed=1)
    ps = list(odds.values())
    # Still a valid distribution: exactly `cutoff` teams make it each sim.
    assert abs(sum(ps) - 4.0) < 1e-9, sum(ps)
    # The red flag we're fixing: nothing should read as near-certain after 1 game.
    assert max(ps) < 0.9 and min(ps) > 0.1, sorted(ps, reverse=True)
    # But the signal isn't erased -- the best Week-1 team still leads the worst.
    best = max(ids, key=lambda t: scores[t])
    worst = min(ids, key=lambda t: scores[t])
    assert odds[best] > odds[worst], (odds[best], odds[worst])
    hi, lo = odds[best] * 100, odds[worst] * 100
    print(f"ok: 1-game season -> plausible spread (best {hi:.0f}%, worst {lo:.0f}%, "
          f"not 100/0)")


def test_no_remaining_games_is_deterministic():
    conn = db.init_db(":memory:")
    good, bad = _league8(conn)
    # Finalize week 3 so the regular season has no scheduled games left.
    conn.execute("UPDATE matchups SET status='final', home_points=100, "
                 "away_points=90, winner_team_id=home_team_id WHERE week=3")
    conn.commit()
    season.recompute_standings(conn)
    odds = playoffodds.playoff_odds(conn, iterations=1000, seed=1)
    # Deterministic: exactly the current top-4 seeds are 1.0, the rest 0.0.
    made = {t for t, p in odds.items() if p == 1.0}
    assert len(made) == 4 and all(odds[t] == 0.0 for t in odds if t not in made)
    assert set(made) == set(good), made
    print("ok: no remaining games -> deterministic 100/0 on current seeds")


def main():
    test_made_counts_core()
    test_simulation_topheavy_and_invariants()
    test_seed_determinism()
    test_one_game_season_not_degenerate()
    test_no_remaining_games_is_deterministic()
    print("\nALL OFFLINE PLAYOFF-ODDS TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
