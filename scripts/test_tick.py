"""Offline tests for the tick loop + dashboard -- no API, no network.

Seeds a synthetic league with week-1 scores, then drives run_tick with data
access stubbed off and checks: a new week advances (scored + standings), a
repeat tick is idle, and the dashboard renders with real content.
Run:  python -m scripts.test_tick
"""
import sys, os, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, tick, dashboard, season

ROSTER_POS = (["QB"] * 2 + ["RB"] * 5 + ["WR"] * 4 + ["TE"] * 2 + ["K"] + ["DST"])


def _seed(conn):
    conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                     num_teams, status) VALUES(1,2026,1,0,8,'setup')""")
    for slot in range(1, 9):
        conn.execute(
            """INSERT INTO teams(team_name, gm_name, draft_slot, chattiness)
               VALUES(?,?,?, 'moderate')""", (f"Team {slot}", f"GM {slot}", slot))
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
            conn.execute("""INSERT INTO player_weekly_scores(player_id, season, week,
                             fantasy_points) VALUES(?,2026,1,?)""",
                         (pid, 8.0 + (ti % 5) + i * 0.1))
            proj[pid] = 100 - (ti * 100 + i)
    conn.commit()
    return proj


def test_tick_advances_then_idles():
    conn = db.init_db(":memory:")
    proj = _seed(conn)
    with tempfile.TemporaryDirectory() as tmp:
        dash = os.path.join(tmp, "dash.html")
        # Week 1 is "complete"; skip real data + market/chat (API) for the test.
        r1 = tick.run_tick(conn, sync=False, refresh=False, latest_completed=1,
                           do_market=False, do_chat=False, backup_after=False,
                           dash_path=dash, proj_map=proj)
        assert r1["status"] == "advanced" and r1["weeks_scored"] == [1], r1
        assert os.path.exists(dash)

        # league advanced to week 1; standings now populated.
        assert conn.execute("SELECT current_week FROM league").fetchone()[0] == 1
        s = season.standings(conn)
        assert sum(t["wins"] + t["losses"] + t["ties"] for t in s) == 8

        # A second tick with nothing new is idle and doesn't double-score.
        r2 = tick.run_tick(conn, sync=False, refresh=False, latest_completed=1,
                           do_market=False, do_chat=False, backup_after=False,
                           dash_path=dash, proj_map=proj)
        assert r2["status"] == "idle" and r2["weeks_scored"] == [], r2
        s2 = season.standings(conn)
        assert s == s2, "idle tick changed standings"
    print("ok: run_tick advances a new week, then idles (no double-count)")


def test_no_league_is_safe():
    conn = db.init_db(":memory:")
    r = tick.run_tick(conn, sync=False, refresh=False, latest_completed=1)
    assert r["status"] == "no_league", r
    print("ok: run_tick with no league is a safe no-op")


def test_dashboard_has_content():
    conn = db.init_db(":memory:")
    _seed(conn)
    season.build_schedule(conn)
    html = dashboard.render(conn)
    assert "<title>AI Fantasy League</title>" in html
    assert "Standings" in html and "Team 1" in html
    assert "prefers-color-scheme: dark" in html  # both themes defined
    assert html.strip().endswith("</html>")
    print("ok: dashboard renders standings + both themes")


def test_championship_backup():
    import glob
    from scripts.test_playoffs import _seed as seed_playoffs
    with tempfile.TemporaryDirectory() as tmp:
        dbp = os.path.join(tmp, "league.db")
        bdir = os.path.join(tmp, "backups")
        conn = db.init_db(dbp)
        tids, proj = seed_playoffs(conn)
        season.build_schedule(conn)
        for wk in range(1, 15):                 # play out the regular season
            season.score_week(conn, wk, 2026, proj_map=proj)
        conn.execute("UPDATE league SET current_week=14 WHERE id=1")
        conn.commit()

        common = dict(sync=False, refresh=False, latest_completed=16,
                      do_market=False, do_chat=False, do_midweek=False,
                      make_dashboard=False, make_digest=False, db_path=dbp,
                      proj_map=proj)
        os.environ["FFL_BACKUP_DIR"] = bdir
        try:
            r1 = tick.run_tick(conn, **common)   # crowns the champion
            after_first = len(glob.glob(os.path.join(bdir, "league-*.db")))
            r2 = tick.run_tick(conn, **common)   # already complete
        finally:
            os.environ.pop("FFL_BACKUP_DIR", None)

        assert r1["champion"] is not None
        assert any("championship backup" in e for e in r1["events"]), r1["events"]
        assert after_first >= 1, "no championship backup file written"
        assert not any("championship backup" in e for e in r2["events"]), r2["events"]
    print("ok: championship backup taken once at crowning, not repeated")


def main():
    test_tick_advances_then_idles()
    test_no_league_is_safe()
    test_dashboard_has_content()
    test_championship_backup()
    print("\nALL OFFLINE TICK TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
