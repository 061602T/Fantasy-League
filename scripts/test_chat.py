"""Offline tests for the event-aware group chat -- no API calls.

Stubs the Haiku gate and Sonnet compose so the summary-building, gating,
threading, and persistence can be checked deterministically.
Run:  python -m scripts.test_chat
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, chat


def _seed(conn):
    conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                      num_teams, status) VALUES(1,2026,1,1,8,'regular')""")
    for slot, chatty in [(1, "trash-talker"), (2, "quiet"), (3, "moderate")]:
        conn.execute(
            """INSERT INTO teams(team_name, gm_name, draft_slot, personality,
                 chattiness) VALUES(?,?,?,?,?)""",
            (f"Team {slot}", f"GM {slot}", slot, "plays hard", chatty))
    tids = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]
    # Week-1 matchup: team1 beats team2 in a blowout; team3 idle for summary.
    conn.execute("""INSERT INTO matchups(week, home_team_id, away_team_id,
                     home_points, away_points, winner_team_id, status)
                     VALUES(1,?,?,150.0,100.0,?, 'final')""", (tids[0], tids[1], tids[0]))
    conn.commit()
    return tids


def test_week_summary():
    conn = db.init_db(":memory:")
    t1, t2, t3 = _seed(conn)
    headline, detail, inv = chat.week_summary(conn, 1)
    assert "Week 1" in headline
    assert "WON" in inv[t1] and "HIGH score" in inv[t1]
    assert "LOST" in inv[t2] and "LOW score" in inv[t2]
    assert "beat" in detail and "150.0-100.0" in detail
    print("ok: week_summary (result lines, involvement, high/low tags)")


def test_react_gating_and_threading():
    conn = db.init_db(":memory:")
    t1, t2, t3 = _seed(conn)

    # Only the trash-talker (GM 1) and moderate (GM 3) speak; quiet (GM 2) never.
    speakers = {"GM 1", "GM 3"}
    chat.llm.gate = lambda system, user, **k: any(g in system for g in speakers)

    prompts, counter = [], {"n": 0}
    def fake_compose(system, user, **k):
        prompts.append(user)
        counter["n"] += 1
        return {"message": f"msg{counter['n']}"}
    chat.llm.chat_json = fake_compose

    posted = chat.react_to_event(conn, "Big week!", "details", rounds=2)
    # 2 speakers x 2 rounds = 4 messages; quiet GM posted nothing.
    assert len(posted) == 4, posted
    assert all(p["gm_name"] in speakers for p in posted)
    banter = conn.execute(
        "SELECT COUNT(*) FROM chat_log WHERE event_type='banter'").fetchone()[0]
    assert banter == 4
    # Threading: the 2nd poster's prompt sees the 1st poster's message.
    assert "msg1" in prompts[1], "later speaker did not see earlier chat"
    print("ok: react_to_event (chattiness gating, threading, banter persisted)")


def test_no_matchup_is_noop():
    conn = db.init_db(":memory:")
    assert chat.week_summary(conn, 9) is None
    assert chat.react_to_week(conn, 9) == []
    print("ok: react_to_week with no scored matchups is a no-op")


def main():
    test_week_summary()
    test_react_gating_and_threading()
    test_no_matchup_is_noop()
    print("\nALL OFFLINE CHAT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
