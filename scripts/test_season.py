"""Offline tests for the weekly scoring cycle -- no API, no network.

Builds a synthetic 8-team league (rosters + fake weekly scores), then checks
the schedule, lineup optimizer, scoring, standings, and idempotency.
Run:  python -m scripts.test_season
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, season, config

# 15-man roster template (legal: QB>=1, RB>=2, WR>=2, TE>=1, K1, DST1, +flex).
ROSTER_POS = (["QB"] * 2 + ["RB"] * 5 + ["WR"] * 4 + ["TE"] * 2 + ["K"] + ["DST"])


def _seed(conn):
    conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                      num_teams, status) VALUES(1,2026,1,0,8,'regular')""")
    for slot in range(1, 9):
        conn.execute(
            """INSERT INTO teams(team_name, gm_name, draft_slot)
               VALUES(?,?,?)""", (f"Team {slot}", f"GM {slot}", slot))
    team_ids = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]

    proj_map, points = {}, {}
    for ti, tid in enumerate(team_ids):
        for i, pos in enumerate(ROSTER_POS):
            pid = f"t{tid}_{pos}_{i}"
            conn.execute(
                "INSERT INTO players(player_id, name, position) VALUES(?,?,?)",
                (pid, pid, pos))
            conn.execute(
                """INSERT INTO rosters(team_id, player_id, acquired_via, acquired_week)
                   VALUES(?,?, 'draft', 0)""", (tid, pid))
            # Distinct projections so the optimizer is deterministic.
            proj_map[pid] = 100.0 - (ti * 100 + i)
            # Fake week-1 actual points: vary by team so matchups have winners.
            pts = 10.0 + (ti % 4) * 3 + (i * 0.1)
            conn.execute(
                """INSERT INTO player_weekly_scores(player_id, season, week,
                     fantasy_points) VALUES(?,?,?,?)""", (pid, 2026, 1, pts))
            points[pid] = pts
    conn.commit()
    return team_ids, proj_map


def test_round_robin():
    ids = list(range(1, 9))
    sched = season.round_robin(ids, config.REGULAR_SEASON_WEEKS)
    assert len(sched) == 14
    from collections import Counter
    meetings = Counter()
    for week in sched:
        seen = []
        assert len(week) == 4, "each week has 4 matchups for 8 teams"
        for h, a in week:
            assert h != a, "no team plays itself"
            seen += [h, a]
            meetings[frozenset((h, a))] += 1
        assert sorted(seen) == ids, "every team plays exactly once each week"
    # Double round-robin: each of the 28 pairs meets exactly twice.
    assert len(meetings) == 28 and set(meetings.values()) == {2}, meetings
    print("ok: round_robin (14 weeks, everyone plays weekly, each pair twice)")


def test_optimal_lineup():
    # 1 QB, 3 RB, 3 WR, 1 TE, 1 K, 1 DST; projections descending by list order.
    roster, proj = [], 50
    for pos in ["QB", "RB", "RB", "RB", "WR", "WR", "WR", "TE", "K", "DST"]:
        roster.append({"player_id": f"{pos}{proj}", "position": pos, "proj": proj})
        proj -= 1
    lineup = season.optimal_lineup(roster)
    assert lineup["QB"] == ["QB50"]
    assert lineup["RB"] == ["RB49", "RB48"]          # two best RB
    assert lineup["WR"] == ["WR46", "WR45"]          # two best WR
    assert lineup["TE"] == ["TE43"]
    assert lineup["FLEX"] == ["RB47"]                # best remaining flex-eligible
    assert lineup["K"] == ["K42"] and lineup["DST"] == ["DST41"]
    print("ok: optimal_lineup (slots filled by projection, FLEX takes the spare RB)")


def test_score_and_standings():
    conn = db.init_db(":memory:")
    team_ids, proj_map = _seed(conn)
    assert season.build_schedule(conn) == 14 * 4

    results = season.score_week(conn, 1, season=2026, proj_map=proj_map)
    assert len(results) == 4, "8 teams -> 4 matchups in week 1"

    # Each team starts exactly 9 (QB,RB,RB,WR,WR,TE,FLEX,K,DST); rest bench.
    for tid in team_ids:
        n_start = conn.execute(
            "SELECT COUNT(*) FROM lineups WHERE team_id=? AND week=1 AND slot!='BENCH'",
            (tid,)).fetchone()[0]
        assert n_start == 9, f"team {tid} started {n_start}"

    # Matchup points equal the sum of that team's non-bench actual points.
    pts = season._weekly_points(conn, 2026, 1)
    for r in results:
        for side in ("home", "away"):
            tid = r[f"{side}_team_id"]
            starters = [row["player_id"] for row in conn.execute(
                "SELECT player_id FROM lineups WHERE team_id=? AND week=1 "
                "AND slot!='BENCH'", (tid,))]
            expect = round(sum(pts[p] for p in starters), 2)
            assert r[f"{side}_points"] == expect, (r, side, expect)

    # Standings are zero-sum and cover exactly the 4 games played.
    s = season.standings(conn)
    assert sum(t["wins"] for t in s) + sum(t["ties"] for t in s) // 1 >= 0
    assert sum(t["wins"] for t in s) == sum(t["losses"] for t in s)
    assert sum(t["wins"] + t["losses"] + t["ties"] for t in s) == 8  # 4 games, 2 sides
    print(f"ok: score_week (4 matchups final, standings zero-sum; "
          f"leader {s[0]['team_name']} {s[0]['wins']}-{s[0]['losses']})")

    # Idempotency: re-scoring the same week doesn't double-count.
    before = season.standings(conn)
    season.score_week(conn, 1, season=2026, proj_map=proj_map)
    after = season.standings(conn)
    assert before == after, "re-scoring changed standings"
    print("ok: re-scoring a week is idempotent (no double-count)")


def main():
    test_round_robin()
    test_optimal_lineup()
    test_score_and_standings()
    print("\nALL OFFLINE SEASON TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
