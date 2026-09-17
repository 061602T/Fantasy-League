"""Offline tests for the governance layer -- no API calls.

Stubs the LLM-touching functions and drives the propose -> vote -> tally ->
enact flow deterministically, plus each bounded effect's validation. Run:
    python -m scripts.test_governance
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, db, effects, governance, market

_CHATTINESS = ["trash-talker", "moderate", "quiet", "moderate",
               "quiet", "moderate", "trash-talker", "quiet"]


def _seed():
    conn = db.init_db(":memory:")
    conn.execute("INSERT INTO league(id,season,as_of_week,current_week,num_teams,"
                 "status) VALUES(1,2026,1,1,8,'regular')")
    for s in range(1, 9):
        conn.execute("INSERT INTO teams(team_id,team_name,gm_name,personality,bio,"
                     "chattiness,draft_slot,faab_remaining) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (s, f"Team {s}", f"GM {s}", "runs it hard", "some bio",
                      _CHATTINESS[s - 1], s, 100))
    conn.commit()
    return conn


# --- effect bounds ----------------------------------------------------------

def test_faab_bounds():
    conn = _seed()
    ok, _ = effects.validate_effect(conn, "faab_adjust", 1, {"delta": 0})
    assert not ok, "zero delta should be rejected"
    ok, _ = effects.validate_effect(conn, "faab_adjust", 1, {"delta": 999})
    assert not ok, "over-cap delta should be rejected"
    ok, _ = effects.validate_effect(conn, "faab_adjust", 999, {"delta": -5})
    assert not ok, "unknown team should be rejected"

    # Can't go negative.
    conn.execute("UPDATE teams SET faab_remaining=10 WHERE team_id=1")
    ok, msg = effects.apply_effect(conn, "faab_adjust", 1, {"delta": -25})
    assert ok and conn.execute("SELECT faab_remaining FROM teams WHERE team_id=1"
                               ).fetchone()[0] == 0, msg
    # Trade-won surplus above the cap is preserved as the ceiling.
    conn.execute("UPDATE teams SET faab_remaining=120 WHERE team_id=2")
    effects.apply_effect(conn, "faab_adjust", 2, {"delta": 50})
    assert conn.execute("SELECT faab_remaining FROM teams WHERE team_id=2"
                        ).fetchone()[0] == 120, "should clamp to max(cap,current)=120"
    # A normal team can't be inflated past the cap.
    effects.apply_effect(conn, "faab_adjust", 3, {"delta": 50})   # was 100
    assert conn.execute("SELECT faab_remaining FROM teams WHERE team_id=3"
                        ).fetchone()[0] == 100, "should clamp to cap 100"
    print("ok: faab_adjust bounds (non-zero, <=cap delta, no negative, surplus kept)")


def test_duration_and_loser_bounds():
    conn = _seed()
    for etype in ("trade_freeze", "waiver_backseat"):
        assert not effects.validate_effect(conn, etype, 1, {"weeks": 0})[0]
        assert not effects.validate_effect(conn, etype, 1, {"weeks": 4})[0]
        ok, _ = effects.apply_effect(conn, etype, 1, {"weeks": 2}, bylaw_id=None)
        assert ok
    rows = conn.execute("SELECT effect_type, active_through_week FROM team_effects "
                        "WHERE team_id=1 ORDER BY effect_id").fetchall()
    # current_week is 1, so a 2-week duration is recorded through week 3.
    assert [r["active_through_week"] for r in rows] == [3, 3], rows

    assert not effects.validate_effect(conn, "loser_flag", 1, {"label": "   "})[0]
    ok, _ = effects.apply_effect(conn, "loser_flag", 2,
                                 {"label": "x" * 200})   # over-long, gets capped
    row = conn.execute("SELECT params_json, active_through_week FROM team_effects "
                       "WHERE team_id=2 AND effect_type='loser_flag'").fetchone()
    label = json.loads(row["params_json"])["label"]
    assert len(label) <= config.GOV_LOSER_LABEL_MAX and row["active_through_week"] is None
    assert not effects.validate_effect(conn, "bogus", 1, {})[0], "unknown effect"
    print("ok: duration/loser bounds (weeks 1..max, label capped, unknown rejected)")


# --- propose / vote / tally -------------------------------------------------

def _stub_propose(title="Tax the hoarders", pitch="They sit on FAAB like dragons."):
    governance.llm.chat_json = lambda *a, **k: {"title": title, "pitch": pitch}


def _stub_votes(mapping):
    """mapping: team_id -> 'yes'|'no'|'abstain'. Patches the per-GM vote call."""
    governance._cast_vote = lambda conn, b, team: {
        "vote": mapping.get(team["team_id"], "abstain"), "message": "because"}


def _close_now(conn):
    return governance.close_if_due(
        conn, now=datetime.now(timezone.utc) + timedelta(hours=99))


def test_propose_sanitizes_and_locks():
    conn = _seed()
    _stub_propose(title="A" * 500, pitch="  ctrl\x07chars\n\nand   spaces  ")
    b = governance.propose(conn, 1)
    assert b and len(b["title"]) <= config.GOV_TITLE_MAX
    assert "\x07" not in b["rationale"] and "  " not in b["rationale"]
    # Proposer is auto-recorded as YES.
    assert governance.tally(conn, b["bylaw_id"])["yes"] == 1
    # Only one bylaw on the floor at a time.
    assert governance.propose(conn, 2) is None, "second concurrent proposal allowed"
    print("ok: propose sanitizes text, auto-yes for proposer, one-at-a-time lock")


def test_vote_pass():
    conn = _seed()
    _stub_propose()
    b = governance.propose(conn, 1)                 # proposer team 1 -> yes
    _stub_votes({2: "yes", 3: "yes", 4: "yes", 5: "no"})  # +3 yes, 1 no; rest abstain
    governance.cast_missing_votes(conn, b["bylaw_id"])
    t = governance.tally(conn, b["bylaw_id"])
    assert (t["yes"], t["no"]) == (4, 1) and t["passed"], t
    out = _close_now(conn)
    assert out and out[0]["status"] == "passed_pending"
    assert governance.pending(conn)[0]["bylaw_id"] == b["bylaw_id"]
    print("ok: vote pass -> passed_pending (4-1)")


def test_vote_tie_fails():
    conn = _seed()
    _stub_propose()
    b = governance.propose(conn, 1)                 # yes
    _stub_votes({2: "no", 3: "no", 4: "yes"})       # yes 2 (1,4), no 2 (2,3)
    governance.cast_missing_votes(conn, b["bylaw_id"])
    t = governance.tally(conn, b["bylaw_id"])
    assert (t["yes"], t["no"]) == (2, 2) and not t["passed"], t
    out = _close_now(conn)
    assert out[0]["status"] == "rejected_vote" and "tie" in out[0]["reason"]
    print("ok: 2-2 tie fails (status quo wins)")


def test_vote_no_quorum_fails():
    conn = _seed()
    _stub_propose()
    b = governance.propose(conn, 1)                 # yes, everyone else abstains
    _stub_votes({})
    governance.cast_missing_votes(conn, b["bylaw_id"])
    t = governance.tally(conn, b["bylaw_id"])
    assert t["cast"] == 1 and not t["passed"] and not t["quorum_ok"], t
    out = _close_now(conn)
    assert out[0]["status"] == "rejected_vote" and "quorum" in out[0]["reason"]
    print("ok: below-quorum vote fails")


# --- enactment --------------------------------------------------------------

def _passed_bylaw(conn):
    _stub_propose()
    b = governance.propose(conn, 1)
    _stub_votes({2: "yes", 3: "yes", 4: "yes", 5: "no"})
    governance.cast_missing_votes(conn, b["bylaw_id"])
    _close_now(conn)
    return b["bylaw_id"]


def test_enact_lore():
    conn = _seed()
    bid = _passed_bylaw(conn)
    ok, _ = governance.enact_lore(conn, bid)
    assert ok
    assert [x["bylaw_id"] for x in governance.active_lore(conn)] == [bid]
    # Re-enacting something no longer pending fails cleanly.
    assert not governance.enact_lore(conn, bid)[0]
    print("ok: enact as lore -> standing rule; non-pending re-enact refused")


def test_enact_effect():
    conn = _seed()
    bid = _passed_bylaw(conn)
    conn.execute("UPDATE teams SET faab_remaining=80 WHERE team_id=6")
    ok, summary = governance.enact_effect(conn, bid, "faab_adjust", 6, {"delta": -25})
    assert ok and conn.execute("SELECT faab_remaining FROM teams WHERE team_id=6"
                               ).fetchone()[0] == 55, summary
    row = conn.execute("SELECT status, enacted_json FROM bylaws WHERE bylaw_id=?",
                       (bid,)).fetchone()
    assert row["status"] == "enacted_effect"
    assert json.loads(row["enacted_json"])["effect_type"] == "faab_adjust"
    print("ok: enact mechanical effect applies + records on the bylaw")


def test_enact_effect_out_of_bounds_refused():
    conn = _seed()
    bid = _passed_bylaw(conn)
    ok, msg = governance.enact_effect(conn, bid, "faab_adjust", 6, {"delta": 999})
    assert not ok and "rejected" in msg
    # Bylaw stays pending so it can still be enacted correctly.
    assert governance.pending(conn)[0]["bylaw_id"] == bid
    print("ok: out-of-bounds effect refused, bylaw left pending")


def test_reject():
    conn = _seed()
    bid = _passed_bylaw(conn)
    ok, _ = governance.reject(conn, bid, "too far")
    assert ok and conn.execute("SELECT status FROM bylaws WHERE bylaw_id=?",
                               (bid,)).fetchone()[0] == "rejected_admin"
    print("ok: commissioner reject -> rejected_admin")


# --- phase 1: tick wiring ---------------------------------------------------

def test_tick_step_proposes_then_votes():
    import random
    conn = _seed()
    _stub_propose(title="No Kickers Before Noon", pitch="A modest proposal.")
    governance._wants_to_propose = lambda team: True
    old = config.GOV_PROPOSE_PROB
    config.GOV_PROPOSE_PROB = 1.0                 # force the pre-gate open
    try:
        ev1 = governance.step(conn, rng=random.Random(1))
    finally:
        config.GOV_PROPOSE_PROB = old
    assert any("proposed" in e for e in ev1), ev1
    assert len(governance.active_voting(conn)) == 1

    # Next tick: a bylaw is open, so the step casts (a few) votes, never proposes.
    _stub_votes({t: "yes" for t in range(2, 9)})
    ev2 = governance.step(conn, rng=random.Random(2))
    assert any("vote" in e for e in ev2), ev2
    voted = len(governance._voted(conn, governance.active_voting(conn)[0]["bylaw_id"]))
    assert voted <= 4, "per-tick vote trickle should be limited (proposer + <=3)"
    print("ok: tick step proposes when idle, trickles votes when one is open")


def test_tick_step_never_raises():
    import random
    conn = _seed()
    governance.maybe_propose = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    ev = governance.step(conn, rng=random.Random(0))   # must swallow the error
    assert any("WARNING" in e for e in ev), ev
    print("ok: governance step swallows errors (never crashes the driving loop)")


# --- phase 2: enforcement hooks ---------------------------------------------

def test_active_window_and_freeze_blocks_trades():
    conn = _seed()
    effects.apply_effect(conn, "trade_freeze", 1, {"weeks": 2})   # wk1 -> through 3
    assert effects.active_team_ids(conn, "trade_freeze", 1) == {1}
    assert effects.active_team_ids(conn, "trade_freeze", 3) == {1}
    assert effects.active_team_ids(conn, "trade_freeze", 4) == set()   # expired
    effects.apply_effect(conn, "loser_flag", 2, {"label": "clown"})
    assert 2 in effects.active_team_ids(conn, "loser_flag", 99)        # NULL = always

    # negotiate guards before any LLM call, so a frozen team on either side is a
    # no-op with no API contact.
    assert market.negotiate(conn, 1, 2, proj_map={})["status"] == "no_offer"
    assert market.negotiate(conn, 2, 1, proj_map={})["status"] == "no_offer"
    print("ok: active-effect window + trade_freeze blocks trades (both sides)")


def test_waiver_backseat_ordering():
    conn = _seed()
    effects.apply_effect(conn, "waiver_backseat", 3, {"weeks": 1})    # active this week
    key = market._waiver_priority_key(conn, week=1)
    tie = sorted([{"team_id": 3, "faab": 10}, {"team_id": 2, "faab": 10}], key=key)
    assert [c["team_id"] for c in tie] == [2, 3], "backseated team must lose the tie"
    higher = sorted([{"team_id": 2, "faab": 10}, {"team_id": 3, "faab": 20}], key=key)
    assert higher[0]["team_id"] == 3, "a higher bid still beats the penalty"
    print("ok: waiver_backseat loses ties but not higher bids")


def main():
    test_faab_bounds()
    test_duration_and_loser_bounds()
    test_propose_sanitizes_and_locks()
    test_vote_pass()
    test_vote_tie_fails()
    test_vote_no_quorum_fails()
    test_enact_lore()
    test_enact_effect()
    test_enact_effect_out_of_bounds_refused()
    test_reject()
    test_tick_step_proposes_then_votes()
    test_tick_step_never_raises()
    test_active_window_and_freeze_blocks_trades()
    test_waiver_backseat_ordering()
    print("\nALL OFFLINE GOVERNANCE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
