"""Offline tests for the event-aware group chat -- no API calls.

Stubs the Haiku gate and Sonnet compose so the summary-building, gating,
threading, and persistence can be checked deterministically.
Run:  python -m scripts.test_chat
"""
import sys, os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, chat

_NOW = datetime(2026, 9, 15, 14, 30, 0, tzinfo=timezone.utc)


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


# --- Ambient chat (the decoupled loop) -------------------------------------

def _seed_ambient(conn):
    for slot, chatty in [(1, "trash-talker"), (2, "moderate"), (3, "quiet")]:
        conn.execute("""INSERT INTO teams(team_name, gm_name, draft_slot,
                         personality, bio, chattiness) VALUES(?,?,?,?,?,?)""",
                     (f"Team {slot}", f"GM {slot}", slot, "plays hard", "", chatty))
    conn.commit()


class _FakeRng:
    """Deterministic rng for ambient_exchange: fixed starter, reply-count, mode,
    and stagger, so the exchange is fully controlled."""
    def __init__(self, n_replies=2, random_val=0.0, stagger=30):
        self.n_replies, self.random_val, self.stagger = n_replies, random_val, stagger

    def choices(self, population, weights=None, k=1):
        # [0,1,2] reply-count list vs the team list.
        if population and isinstance(population[0], int):
            return [self.n_replies]
        return [population[0]]

    def random(self):
        return self.random_val

    def shuffle(self, x):
        pass

    def randint(self, a, b):
        return self.stagger


def test_ambient_gate_declines():
    conn = db.init_db(":memory:")
    _seed_ambient(conn)
    chat.llm.gate = lambda system, user, **k: False       # nobody wants to talk
    posted = chat.ambient_exchange(conn, rng=_FakeRng(), now=_NOW)
    assert posted == []
    assert conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == 0
    print("ok: ambient_exchange (gate declines -> no posts)")


def test_ambient_threading_and_timestamps():
    conn = db.init_db(":memory:")
    _seed_ambient(conn)
    # Pre-seed a message so the starter is in 'reply' mode and threads onto it.
    conn.execute("INSERT INTO chat_log(team_id, event_type, message) "
                 "VALUES(1,'banter','opening shot from earlier')")
    conn.commit()

    chat.llm.gate = lambda system, user, **k: True
    prompts, n = [], {"i": 0}
    def fake_compose(system, user, **k):
        prompts.append(user)
        n["i"] += 1
        return {"message": f"line{n['i']}"}
    chat.llm.chat_json = fake_compose

    posted = chat.ambient_exchange(conn, rng=_FakeRng(n_replies=2, stagger=40),
                                   now=_NOW)
    # Starter + 2 replies = 3 messages.
    assert len(posted) == 3, posted
    assert [p["mode"] for p in posted] == ["reply", "reply", "reply"]
    # Threading: a later composer saw an earlier message in its prompt.
    assert any("line1" in p for p in prompts[1:]), "reply didn't see earlier message"
    # Each message stored with its OWN staggered timestamp, strictly increasing.
    ts = [r["created_at"] for r in conn.execute(
        "SELECT created_at FROM chat_log WHERE event_type='banter' "
        "AND message LIKE 'line%' ORDER BY chat_id")]
    assert len(set(ts)) == 3 and ts == sorted(ts), ts
    print("ok: ambient_exchange (threaded replies, staggered unique timestamps)")


def test_ambient_fresh_topic_when_quiet():
    conn = db.init_db(":memory:")
    _seed_ambient(conn)
    chat.llm.gate = lambda system, user, **k: True
    seen = {}
    def fake_compose(system, user, **k):
        seen["user"] = user
        return {"message": "fresh take"}
    chat.llm.chat_json = fake_compose
    # No prior chat -> starter must open a fresh topic (not a reply).
    posted = chat.ambient_exchange(conn, rng=_FakeRng(n_replies=0), now=_NOW)
    assert len(posted) == 1 and posted[0]["mode"] == "fresh"
    assert "NOT a reply" in seen["user"], "fresh-topic prompt expected"
    print("ok: ambient_exchange (opens a fresh topic when the chat is quiet)")


def test_last_banter_age():
    conn = db.init_db(":memory:")
    _seed_ambient(conn)
    assert chat.last_banter_age(conn) is None            # nothing yet
    from datetime import datetime, timezone
    old = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO chat_log(team_id, event_type, message, created_at) "
                 "VALUES(1,'banter','just now',?)", (old,))
    conn.commit()
    age = chat.last_banter_age(conn)
    assert age is not None and age < 5, age                # seconds old
    print("ok: last_banter_age (None when empty, small age for a fresh post)")


def main():
    test_week_summary()
    test_react_gating_and_threading()
    test_no_matchup_is_noop()
    test_ambient_gate_declines()
    test_ambient_threading_and_timestamps()
    test_ambient_fresh_topic_when_quiet()
    test_last_banter_age()
    print("\nALL OFFLINE CHAT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
