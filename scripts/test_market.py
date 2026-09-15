"""Offline tests for trades + waivers -- no API calls.

Stubs the LLM-facing functions (propose/evaluate/decide) so the execution,
legality, negotiation-flow, and waiver-resolution logic can be checked
deterministically. Run:  python -m scripts.test_market
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import db, market, rosters, config

ROSTER_POS = (["QB"] * 2 + ["RB"] * 5 + ["WR"] * 4 + ["TE"] * 2 + ["K"] + ["DST"])


def _seed(conn, n_teams=2, faab=None):
    conn.execute("""INSERT INTO league(id, season, as_of_week, current_week,
                      num_teams, status) VALUES(1,2026,1,1,8,'regular')""")
    faab = faab or [100] * n_teams
    for slot in range(1, n_teams + 1):
        conn.execute(
            """INSERT INTO teams(team_name, gm_name, draft_slot, personality,
                 risk_tolerance, valuation_bias, faab_remaining)
               VALUES(?,?,?,?,?,?,?)""",
            (f"Team {slot}", f"GM {slot}", slot, "plays hard", "balanced",
             "none", faab[slot - 1]))
    tids = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]
    for tid in tids:
        for i, pos in enumerate(ROSTER_POS):
            pid = f"t{tid}_{pos}_{i}"
            conn.execute("INSERT INTO players(player_id, name, position) VALUES(?,?,?)",
                         (pid, f"{pos}{i} (T{tid})", pos))
            conn.execute("""INSERT INTO rosters(team_id, player_id, acquired_via,
                             acquired_week) VALUES(?,?, 'draft', 0)""", (tid, pid))
    conn.commit()
    return tids


def _first(conn, tid, pos):
    return conn.execute(
        """SELECT p.player_id FROM rosters r JOIN players p ON p.player_id=r.player_id
            WHERE r.team_id=? AND p.position=? AND r.dropped_week IS NULL LIMIT 1""",
        (tid, pos)).fetchone()["player_id"]


def test_validate_and_execute():
    conn = db.init_db(":memory:")
    a, b = _seed(conn, 2)
    ra, rb = _first(conn, a, "RB"), _first(conn, b, "RB")

    good = {"a": a, "b": b, "a_gives": [ra], "b_gives": [rb], "a_faab": 10, "b_faab": 0}
    assert market.validate_offer(conn, good)

    # Uneven counts, bad ownership, unaffordable, roster-breaking -> invalid.
    assert not market.validate_offer(conn, {**good, "b_gives": [rb, _first(conn, b, "WR")]})
    assert not market.validate_offer(conn, {**good, "a_gives": [rb]})  # a doesn't own rb
    assert not market.validate_offer(conn, {**good, "a_faab": 999})
    qbs_a = [r["player_id"] for r in conn.execute(
        """SELECT p.player_id FROM rosters r JOIN players p ON p.player_id=r.player_id
            WHERE r.team_id=? AND p.position='QB' AND r.dropped_week IS NULL""", (a,))]
    wrs_b = [r["player_id"] for r in conn.execute(
        """SELECT p.player_id FROM rosters r JOIN players p ON p.player_id=r.player_id
            WHERE r.team_id=? AND p.position='WR' AND r.dropped_week IS NULL""", (b,))][:2]
    assert not market.validate_offer(conn, {"a": a, "b": b, "a_gives": qbs_a,
                                            "b_gives": wrs_b, "a_faab": 0, "b_faab": 0})

    assert market.execute_trade(conn, good) is True
    # ra now on b, rb now on a; sizes stay 15; FAAB moved.
    assert rosters.owner_of(conn, ra) == b and rosters.owner_of(conn, rb) == a
    for tid in (a, b):
        assert len(rosters.active_roster(conn, tid)) == 15
    fa = conn.execute("SELECT faab_remaining FROM teams WHERE team_id=?", (a,)).fetchone()[0]
    fb = conn.execute("SELECT faab_remaining FROM teams WHERE team_id=?", (b,)).fetchone()[0]
    assert (fa, fb) == (90, 110), (fa, fb)
    print("ok: validate_offer + execute_trade (legality, ownership, FAAB transfer)")


def test_negotiate_flow():
    for script, expect in [("accept", "accepted"), ("reject", "rejected"),
                           ("counter_accept", "accepted"), ("stall", "rejected")]:
        conn = db.init_db(":memory:")
        a, b = _seed(conn, 2)
        ra, rb = _first(conn, a, "RB"), _first(conn, b, "RB")
        wa = _first(conn, a, "WR")
        base = {"a": a, "b": b, "a_gives": [ra], "b_gives": [rb], "a_faab": 0, "b_faab": 0}
        counter = {"a": a, "b": b, "a_gives": [wa], "b_gives": [rb], "a_faab": 0, "b_faab": 0}

        market.propose_offer = lambda c, x, y, p, base=base: dict(base, _message="deal?")

        calls = {"n": 0}
        def ev(c, decider, offer, rnd, p, script=script, counter=counter, calls=calls):
            calls["n"] += 1
            if script == "accept":
                return {"decision": "accept", "message": "ok"}
            if script == "reject":
                return {"decision": "reject", "message": "no"}
            if script == "counter_accept":
                if calls["n"] == 1:
                    return {"decision": "counter", "message": "how about", "counter": counter}
                return {"decision": "accept", "message": "fine"}
            return {"decision": "counter", "message": "again", "counter": counter}
        market.evaluate_offer = ev

        res = market.negotiate(conn, a, b, proj_map={})
        assert res["status"] == expect, (script, res)
        txn = conn.execute("SELECT status FROM transactions WHERE type='trade'").fetchone()[0]
        if expect == "accepted":
            assert txn == "processed" and rosters.owner_of(conn, rb) == a
        else:
            assert rosters.owner_of(conn, rb) == b  # no move on reject/stall
        print(f"ok: negotiate {script} -> {res['status']} (txn={txn}, rounds={res['rounds']})")


def test_waiver_resolution():
    conn = db.init_db(":memory:")
    a, b = _seed(conn, 2, faab=[100, 100])
    # Two free agents in the pool (unrostered).
    for pid, pos in [("FA_RB", "RB"), ("FA_WR", "WR")]:
        conn.execute("INSERT INTO players(player_id, name, position) VALUES(?,?,?)",
                     (pid, pid, pos))
    conn.commit()

    drop_a, drop_b = _first(conn, a, "WR"), _first(conn, b, "WR")
    # Both teams bid on FA_RB; a bids more -> a wins, b fails and keeps FAAB.
    plan = {a: {"team_id": a, "add": "FA_RB", "drop": drop_a, "faab": 30, "message": "mine"},
            b: {"team_id": b, "add": "FA_RB", "drop": drop_b, "faab": 20, "message": "mine"}}
    market.decide_waiver = lambda c, tid, fl, p: plan[tid]

    res = market.run_waivers(conn, week=2, use_gate=False, proj_map={})
    by_team = {r["team_id"]: r for r in res}
    assert by_team[a]["status"] == "processed" and by_team[b]["status"] == "failed"
    assert rosters.owner_of(conn, "FA_RB") == a
    assert conn.execute("SELECT faab_remaining FROM teams WHERE team_id=?", (a,)).fetchone()[0] == 70
    assert conn.execute("SELECT faab_remaining FROM teams WHERE team_id=?", (b,)).fetchone()[0] == 100
    # Winner still has a legal, full roster; loser unchanged.
    assert len(rosters.active_roster(conn, a)) == 15 and rosters.legal_after(conn, a)
    assert rosters.owner_of(conn, drop_a) is None  # dropped player released
    assert rosters.owner_of(conn, drop_b) == b     # loser kept its player
    print("ok: waivers (highest FAAB wins a contested claim; loser keeps budget)")


def main():
    test_validate_and_execute()
    test_negotiate_flow()
    test_waiver_resolution()
    print("\nALL OFFLINE MARKET TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
