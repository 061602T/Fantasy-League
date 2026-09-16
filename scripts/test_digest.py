"""Offline tests for the notification digest -- no API, no network.

Run:  python -m scripts.test_digest
"""
import sys, os, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, season, digest


def _seed_scored(conn):
    conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                     num_teams, status) VALUES(1,2026,1,1,8,'regular')""")
    for slot in range(1, 3):
        conn.execute("INSERT INTO teams(team_name, gm_name, draft_slot) VALUES(?,?,?)",
                     (f"Team {slot}", f"GM {slot}", slot))
    t1, t2 = [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]
    conn.execute("""INSERT INTO matchups(week, home_team_id, away_team_id,
                     home_points, away_points, winner_team_id, status)
                     VALUES(1,?,?,150.0,120.0,?, 'final')""", (t1, t2, t1))
    conn.execute("UPDATE teams SET wins=1, points_for=150 WHERE team_id=?", (t1,))
    conn.execute("UPDATE teams SET losses=1, points_for=120 WHERE team_id=?", (t2,))
    conn.commit()


def test_render():
    conn = db.init_db(":memory:")
    _seed_scored(conn)
    text = digest.render_digest(conn, weeks_scored=[1], extra_events=[
        "week 1: 1/2 waiver claims won"])
    assert "AI Fantasy League" in text
    assert "Week 1 results" in text and "def." in text
    assert "Standings (top 4)" in text
    assert "waiver" in text  # notable event surfaced
    print("ok: render_digest (results, standings, notable events)")


def test_publish_and_command_delivery():
    conn = db.init_db(":memory:")
    _seed_scored(conn)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "digest.txt")
        sink = os.path.join(tmp, "delivered.txt")
        # FFL_DIGEST_CMD receives the digest on stdin; here we just capture it.
        os.environ["FFL_DIGEST_CMD"] = f"cat > {sink}"
        os.environ.pop("FFL_DIGEST_WEBHOOK", None)
        try:
            res = digest.publish(conn, path=out, weeks_scored=[1])
        finally:
            os.environ.pop("FFL_DIGEST_CMD", None)
        assert os.path.exists(out) and "AI Fantasy League" in open(out).read()
        assert res["delivered"]["command"] == "exit 0"
        assert os.path.exists(sink) and "Week 1 results" in open(sink).read()
    print("ok: publish writes the file and delivers via FFL_DIGEST_CMD")


def test_no_delivery_config_is_noop():
    conn = db.init_db(":memory:")
    _seed_scored(conn)
    for k in ("FFL_DIGEST_CMD", "FFL_DIGEST_WEBHOOK"):
        os.environ.pop(k, None)
    d = digest.deliver("hello")
    assert d == {"webhook": None, "command": None}
    print("ok: deliver with nothing configured is a clean no-op")


def main():
    test_render()
    test_publish_and_command_delivery()
    test_no_delivery_config_is_noop()
    print("\nALL OFFLINE DIGEST TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
