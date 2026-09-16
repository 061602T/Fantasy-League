"""Offline tests for weekly score projections -- no API calls.

Synthetic players/scores/rosters/lineups exercise the projection math, the
cross-season recency window, actual-vs-optimal starter selection, and the
matchup roll-up. Run:  python -m scripts.test_scoreproj
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, scoreproj


def _add_player(conn, pid, pos, scores, name=None, nfl_team=None):
    """scores: list of (season, week, points)."""
    conn.execute("INSERT INTO players(player_id, name, position, nfl_team) "
                 "VALUES(?,?,?,?)", (pid, name or pid, pos, nfl_team))
    for s, w, pts in scores:
        conn.execute("""INSERT INTO player_weekly_scores(player_id, season, week,
                         fantasy_points) VALUES(?,?,?,?)""", (pid, s, w, pts))
    conn.commit()


def test_project_player_trailing_mean():
    conn = db.init_db(":memory:")
    _add_player(conn, "P", "RB",
                [(2025, 1, 10), (2025, 2, 20), (2025, 3, 30),
                 (2025, 4, 40), (2025, 5, 50)])
    # Most recent 4 before week 5 = w4,w3,w2,w1 -> (40+30+20+10)/4 = 25.
    assert scoreproj.project_player(conn, "P", 2025, 5, window=4) == 25.0
    # Fewer than window early on: weeks<3 = w1,w2 -> 15.
    assert scoreproj.project_player(conn, "P", 2025, 3, window=4) == 15.0
    # Window slides: weeks<6 top-4 = w5,w4,w3,w2 -> (50+40+30+20)/4 = 35.
    assert scoreproj.project_player(conn, "P", 2025, 6, window=4) == 35.0
    # No prior game -> None.
    assert scoreproj.project_player(conn, "P", 2025, 1, window=4) is None
    print("ok: project_player (trailing mean, short window, None on no history)")


def test_project_player_spans_seasons():
    conn = db.init_db(":memory:")
    _add_player(conn, "Q", "WR",
                [(2024, 17, 8), (2024, 18, 12), (2025, 1, 4)])
    # Before (2025, wk2): most recent = 2025w1, 2024w18, 2024w17 -> (4+12+8)/3 = 8.
    assert scoreproj.project_player(conn, "Q", 2025, 2, window=4) == 8.0
    # Window caps at 2 -> 2025w1 + 2024w18 = (4+12)/2 = 8.0 (order is chronological).
    assert scoreproj.project_player(conn, "Q", 2025, 2, window=2) == 8.0
    print("ok: project_player (recency window spans the season boundary)")


def _full_team(conn, team_id, flat):
    """Roster a legal lineup; each player's history is flat at its `flat` value
    so its projection equals that value. `flat`: {player_id: (pos, value)}."""
    conn.execute("""INSERT INTO teams(team_id, team_name, gm_name, draft_slot)
                    VALUES(?,?,?,?)""", (team_id, f"T{team_id}", f"GM{team_id}", team_id))
    for pid, (pos, val) in flat.items():
        # nfl_team = the player id, so a bye set can target one player by id.
        _add_player(conn, pid, pos, [(2025, w, val) for w in (1, 2, 3, 4)],
                    nfl_team=pid)
        conn.execute("""INSERT INTO rosters(team_id, player_id, acquired_via,
                         acquired_week) VALUES(?,?,'draft',0)""", (team_id, pid))
    conn.commit()


def test_project_team_actual_vs_optimal():
    conn = db.init_db(":memory:")
    # 9 starters worth of positions + 2 bench-caliber extras.
    flat = {
        "qb": ("QB", 20), "rb1": ("RB", 15), "rb2": ("RB", 12),
        "wr1": ("WR", 14), "wr2": ("WR", 11), "te": ("TE", 9),
        "k": ("K", 8), "dst": ("DST", 7), "flex": ("WR", 13),
        "benchrb": ("RB", 3), "benchwr": ("WR", 2),
    }
    _full_team(conn, 1, flat)

    # Upcoming week 5 (no lineup): optimal lineup by projection is the 9 best
    # legal starters; the two low bench players are left out.
    up = scoreproj.project_team(conn, 1, 2025, 5)
    expected = 20 + 15 + 12 + 14 + 11 + 9 + 8 + 7 + 13  # = 109
    assert up["proj"] == expected, up
    assert up["n_starters"] == 9 and up["n_uncovered"] == 0

    # Scored week 5: pin an ACTUAL lineup that (perversely) benches wr1 and
    # starts benchrb, so project_team must value the recorded starters, not the
    # optimal ones.
    starters = ["qb", "rb1", "rb2", "benchrb", "wr2", "te", "k", "dst", "flex"]
    for pid in starters:
        conn.execute("INSERT INTO lineups(team_id, week, player_id, slot) "
                     "VALUES(1,5,?,?)", (pid, flat[pid][0]))
    for pid in ("wr1", "benchwr"):
        conn.execute("INSERT INTO lineups(team_id, week, player_id, slot) "
                     "VALUES(1,5,?,'BENCH')", (pid,))
    conn.commit()
    scored = scoreproj.project_team(conn, 1, 2025, 5)
    exp_actual = 20 + 15 + 12 + 3 + 11 + 9 + 8 + 7 + 13  # benchrb(3) in, wr1 out
    assert scored["proj"] == exp_actual, scored
    assert exp_actual != expected, "actual-starter path should differ from optimal"
    print("ok: project_team (optimal lineup upcoming, actual starters when scored)")


def test_project_team_uncovered_starter():
    conn = db.init_db(":memory:")
    _full_team(conn, 1, {"qb": ("QB", 20), "rb1": ("RB", 15), "rb2": ("RB", 12),
                         "wr1": ("WR", 14), "wr2": ("WR", 11), "te": ("TE", 9),
                         "k": ("K", 8), "dst": ("DST", 7), "flex": ("WR", 13)})
    # Add a rookie with no history and start him at week 1 (nothing prior).
    _add_player(conn, "rook", "RB", [])
    conn.execute("""INSERT INTO rosters(team_id, player_id, acquired_via,
                     acquired_week) VALUES(1,'rook','draft',0)""")
    conn.execute("INSERT INTO lineups(team_id, week, player_id, slot) "
                 "VALUES(1,1,'rook','RB')", ())
    conn.commit()
    r = scoreproj.project_team(conn, 1, 2025, 1)
    # Only the rookie starts (only lineup row for week 1); no history -> 0, uncovered.
    assert r["proj"] == 0.0 and r["n_uncovered"] == 1 and r["n_covered"] == 0, r
    print("ok: project_team (starter with no history contributes 0, counted uncovered)")


def test_project_team_byes():
    conn = db.init_db(":memory:")
    flat = {"qb": ("QB", 20), "rb1": ("RB", 15), "rb2": ("RB", 12),
            "wr1": ("WR", 14), "wr2": ("WR", 11), "te": ("TE", 9),
            "k": ("K", 8), "dst": ("DST", 7), "flex": ("WR", 13)}
    _full_team(conn, 1, flat)
    # Pin the actual starters (a scored week) = the 9 listed players.
    for pid in flat:
        conn.execute("INSERT INTO lineups(team_id, week, player_id, slot) "
                     "VALUES(1,5,?,?)", (pid, flat[pid][0]))
    conn.commit()

    full = scoreproj.project_team(conn, 1, 2025, 5)["proj"]        # no byes
    # qb (nfl_team 'qb') and wr1 ('wr1') on bye -> their 20 + 14 drop out.
    byed = scoreproj.project_team(conn, 1, 2025, 5, bye_teams={"qb", "wr1"})
    assert byed["proj"] == round(full - 20 - 14, 2), (full, byed["proj"])
    assert byed["n_bye"] == 2 and byed["n_covered"] == 7
    assert all(s["on_bye"] for s in byed["starters"]
               if s["nfl_team"] in {"qb", "wr1"})
    print("ok: project_team (bye-week starters projected 0, counted in n_bye)")


def test_matchup_projections():
    conn = db.init_db(":memory:")
    _full_team(conn, 1, {"qb": ("QB", 20), "rb1": ("RB", 15), "rb2": ("RB", 12),
                         "wr1": ("WR", 14), "wr2": ("WR", 11), "te": ("TE", 9),
                         "k": ("K", 8), "dst": ("DST", 7), "flex": ("WR", 13)})
    _full_team(conn, 2, {"qb2": ("QB", 18), "rb3": ("RB", 10), "rb4": ("RB", 9),
                         "wr3": ("WR", 12), "wr4": ("WR", 8), "te2": ("TE", 6),
                         "k2": ("K", 7), "dst2": ("DST", 5), "flex2": ("WR", 11)})
    conn.execute("""INSERT INTO matchups(week, home_team_id, away_team_id,
                     home_points, away_points, winner_team_id, status)
                     VALUES(5,1,2,140.0,96.0,1,'final')""")
    conn.execute("""INSERT INTO matchups(week, home_team_id, away_team_id, status)
                     VALUES(6,2,1,'scheduled')""")
    conn.commit()

    wk5 = scoreproj.matchup_projections(conn, 5, 2025)
    assert len(wk5) == 1 and wk5[0]["final"] is True
    assert wk5[0]["home_actual"] == 140.0 and wk5[0]["away_actual"] == 96.0
    assert wk5[0]["home_proj"] == 109.0 and wk5[0]["away_proj"] == 86.0

    wk6 = scoreproj.matchup_projections(conn, 6, 2025)
    assert wk6[0]["final"] is False
    assert wk6[0]["home_actual"] is None and wk6[0]["away_actual"] is None
    assert wk6[0]["home_proj"] == 86.0 and wk6[0]["away_proj"] == 109.0
    print("ok: matchup_projections (proj both sides; actuals only when final)")


def main():
    test_project_player_trailing_mean()
    test_project_player_spans_seasons()
    test_project_team_actual_vs_optimal()
    test_project_team_uncovered_starter()
    test_project_team_byes()
    test_matchup_projections()
    print("\nALL OFFLINE SCORE-PROJECTION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
