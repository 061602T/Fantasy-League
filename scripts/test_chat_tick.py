"""Offline tests for the ambient chat-tick script's publish wiring -- no API.

Stubs ambient_exchange, dashboard.write, and ghpages.publish so the "refresh +
publish the dashboard only when something was posted" behavior can be checked
deterministically. Run:  python -m scripts.test_chat_tick
"""
import sys, os, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db
import scripts.run_chat_tick as rct

_POST = [{"gm_name": "G1", "message": "yo",
          "ts": datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)}]


def _league_db():
    p = tempfile.mktemp(suffix=".db")
    conn = db.init_db(p)
    conn.execute("""INSERT INTO league(id,season,as_of_week,current_week,num_teams,
                     status) VALUES(1,2026,1,1,8,'regular')""")
    for s in range(1, 9):
        conn.execute("INSERT INTO teams(team_name,gm_name,draft_slot,chattiness) "
                     "VALUES(?,?,?,?)", (f"T{s}", f"G{s}", s, "moderate"))
    conn.commit()
    conn.close()
    return p


def _run(argv, exchange_result):
    calls = {"wrote": 0, "published": 0}
    rct.chatmod.ambient_exchange = lambda conn: exchange_result
    rct.dashboard.write = lambda conn: (calls.__setitem__("wrote", calls["wrote"] + 1)
                                        or "/tmp/x.html")
    rct.ghpages.publish = lambda path: (calls.__setitem__("published", calls["published"] + 1)
                                        or {"status": "published"})
    old = sys.argv
    sys.argv = ["run_chat_tick"] + argv
    try:
        rct.main()
    finally:
        sys.argv = old
    return calls


def test_publishes_when_posted():
    calls = _run(["--db", _league_db(), "--force"], _POST)
    assert calls["wrote"] == 1 and calls["published"] == 1, calls
    print("ok: chat tick refreshes + publishes the dashboard when it posts")


def test_no_publish_when_quiet():
    calls = _run(["--db", _league_db(), "--force"], [])
    assert calls["wrote"] == 0 and calls["published"] == 0, calls
    print("ok: chat tick does NOT publish when nothing was posted")


def test_no_publish_flag():
    calls = _run(["--db", _league_db(), "--force", "--no-publish"], _POST)
    assert calls["wrote"] == 0 and calls["published"] == 0, calls
    print("ok: --no-publish skips the dashboard refresh even after posting")


def main():
    test_publishes_when_posted()
    test_no_publish_when_quiet()
    test_no_publish_flag()
    print("\nALL OFFLINE CHAT-TICK TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
