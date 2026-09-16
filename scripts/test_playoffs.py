"""Offline tests for the playoff bracket -- no API, no network.

Seeds a league, scores 14 real-ish regular weeks so standings differentiate,
then drives the bracket over weeks 15-16 and checks seeding, advancement, the
champion, and that playoff games don't touch regular-season records.
Run:  python -m scripts.test_playoffs
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, season, playoffs, config

ROSTER_POS = (["QB"] * 2 + ["RB"] * 5 + ["WR"] * 4 + ["TE"] * 2 + ["K"] + ["DST"])


def _seed(conn):
    conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                     num_teams, status) VALUES(1,2026,1,0,8,'regular')""")
    for slot in range(1, 9):
        conn.execute("""INSERT INTO teams(team_name, gm_name, draft_slot,
                         chattiness) VALUES(?,?,?, 'moderate')""",
                     (f"Team {slot}", f"GM {slot}", slot))
    tids = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]
    proj = {}
    for ti, tid in enumerate(tids):
        for i, pos in enumerate(ROSTER_POS):
            pid = f"t{tid}_{pos}_{i}"
            conn.execute("INSERT INTO players(player_id, name, position) VALUES(?,?,?)",
                         (pid, f"{pos}{i} T{tid}", pos))
            conn.execute("""INSERT INTO rosters(team_id, player_id, acquired_via,
                             acquired_week) VALUES(?,?, 'draft', 0)""", (tid, pid))
            proj[pid] = 100 - (ti * 100 + i)
            # Lower team index scores higher, every week (incl. playoffs 15-16),
            # so Team 1 is the top seed and wins out.
            for wk in range(1, 17):
                conn.execute("""INSERT INTO player_weekly_scores(player_id, season,
                                 week, fantasy_points) VALUES(?,2026,?,?)""",
                             (pid, wk, 10.0 + (7 - ti) + i * 0.02))
    conn.commit()
    return tids, proj


def test_helpers():
    assert playoffs._bracket_size() == 4
    assert playoffs.num_rounds() == 2
    assert playoffs.week_of_round(1) == 15 and playoffs.week_of_round(2) == 16
    print("ok: bracket helpers (4 teams, 2 rounds, weeks 15-16)")


def test_full_bracket():
    conn = db.init_db(":memory:")
    tids, proj = _seed(conn)
    season.build_schedule(conn)
    for wk in range(1, config.REGULAR_SEASON_WEEKS + 1):
        season.score_week(conn, wk, 2026, proj_map=proj)

    assert playoffs.regular_season_complete(conn)
    order = playoffs.seeds(conn)
    assert order[0] == tids[0], "Team 1 should be the top seed"

    res = playoffs.advance(conn, latest_completed=16, season_year=2026, proj_map=proj)
    assert res["status"] == "complete", res
    assert res["champion"] == tids[0], "top seed should win it all here"

    # Structure: 2 semifinals in week 15, 1 final in week 16, all final.
    semis = conn.execute("SELECT COUNT(*) FROM matchups WHERE week=15 "
                         "AND status='final'").fetchone()[0]
    final = conn.execute("SELECT COUNT(*) FROM matchups WHERE week=16 "
                        "AND status='final'").fetchone()[0]
    assert semis == 2 and final == 1, (semis, final)
    # Seeding of the semis: 1v4 and 2v3.
    home_aways = {(m["home_team_id"], m["away_team_id"]) for m in conn.execute(
        "SELECT home_team_id, away_team_id FROM matchups WHERE week=15")}
    assert (tids[0], tids[3]) in home_aways and (tids[1], tids[2]) in home_aways

    # Playoff wins do NOT inflate regular-season records: Team 1 went 14-0 in the
    # 14-week regular season, not 16-0 after winning two playoff games.
    champ_wins = conn.execute("SELECT wins FROM teams WHERE team_id=?",
                             (tids[0],)).fetchone()[0]
    assert champ_wins == config.REGULAR_SEASON_WEEKS, champ_wins
    assert conn.execute("SELECT status FROM league").fetchone()[0] == "complete"
    print(f"ok: full bracket (Team 1 champion; regular record stays "
          f"{champ_wins}-0, playoffs excluded)")


def test_advance_waits_for_data():
    conn = db.init_db(":memory:")
    tids, proj = _seed(conn)
    season.build_schedule(conn)
    for wk in range(1, config.REGULAR_SEASON_WEEKS + 1):
        season.score_week(conn, wk, 2026, proj_map=proj)
    # Only week 15 data available -> semis scored, final not yet built/scored.
    conn.execute("DELETE FROM player_weekly_scores WHERE week=16")
    conn.commit()
    res = playoffs.advance(conn, latest_completed=15, season_year=2026, proj_map=proj)
    assert res["status"] == "in_progress" and res["champion"] is None
    # The final matchup may be set (scheduled) once semis end, but it is not
    # scored until week-16 data arrives.
    assert conn.execute("SELECT COUNT(*) FROM matchups WHERE week=16 "
                        "AND status='final'").fetchone()[0] == 0
    print("ok: bracket waits for each round's data before scoring the final")


def main():
    test_helpers()
    test_full_bracket()
    test_advance_waits_for_data()
    print("\nALL OFFLINE PLAYOFF TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
