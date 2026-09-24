"""Offline tests for the auto-written weekly recaps -- no API calls.

Stubs the LLM and drives gather -> generate -> store -> render. Run:
    python -m scripts.test_weekly
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ffl import config, dashboard, db, weeklysummary


def _seed(scored=True):
    conn = db.init_db(":memory:")
    conn.execute("INSERT INTO league(id,season,as_of_week,current_week,num_teams,"
                 "status) VALUES(1,?,1,1,8,'regular')", (config.SEASON,))
    for s in range(1, 9):
        conn.execute("INSERT INTO teams(team_id,team_name,gm_name,chattiness,"
                     "draft_slot,wins,losses,points_for) VALUES(?,?,?,?,?,?,?,?)",
                     (s, f"Team {s}", f"GM {s}", "moderate", s,
                      1 if s % 2 else 0, 0 if s % 2 else 1, 120 - s))
    # A little group chat for flavor.
    conn.execute("INSERT INTO chat_log(team_id, event_type, message) "
                 "VALUES(1,'trash_talk','easy work this week')")
    # Decisions the GMs made this week: a trade, a waiver claim, a bylaw.
    conn.execute("INSERT INTO players(player_id,name,position) VALUES"
                 "('p1','Alpha Back','RB'),('p2','Bravo Wideout','WR')")
    conn.execute(
        "INSERT INTO transactions(type,status,from_team_id,to_team_id,"
        "details_json,resolved_at) VALUES('trade','accepted',1,2,?,datetime('now'))",
        ('{"a_gives":["p1"],"b_gives":["p2"]}',))
    conn.execute(
        "INSERT INTO transactions(type,status,to_team_id,faab_bid,details_json,"
        "resolved_at) VALUES('waiver_claim','processed',3,7,?,datetime('now'))",
        ('{"add":"p2","week":1}',))
    conn.execute(
        "INSERT INTO bylaws(proposer_team_id,title,status,votes_open_at,"
        "votes_close_at,tally_json,resolved_at) VALUES(1,'No Punting Fridays',"
        "'enacted_lore','x','y',?,datetime('now'))", ('{"yes":6,"no":1}',))
    if scored:
        # Week 1: four final matchups, odd teams beat even teams.
        for i, (h, a) in enumerate([(1, 2), (3, 4), (5, 6), (7, 8)]):
            conn.execute(
                "INSERT INTO matchups(week,home_team_id,away_team_id,home_points,"
                "away_points,winner_team_id,status) VALUES(1,?,?,?,?,?, 'final')",
                (h, a, 130.0 - i, 95.0 - i, h))
    conn.commit()
    return conn


def _fake_llm(system, user, **k):
    # Assert the real data was fed in (GM names, not team names), then return
    # the three voices.
    assert "FINAL SCORES" in user and "GM 1 def. GM 2" in user, user[:200]
    assert "STANDINGS NOW" in user
    assert "Team 1" not in user, "recap should use GM names, not team names"
    assert "TRADES THE GMs MADE" in user and "WAIVER MOVES" in user
    return {"lively": "Lively recap of the week.",
            "neutral": "GM 1 beat GM 2.",
            "roast": "GM 8 got absolutely smoked."}


def test_gather_pulls_scores_and_standings():
    conn = _seed()
    g = weeklysummary.gather(conn, 1, config.SEASON)
    assert any("GM 1 def. GM 2" in r for r in g["results"]), g["results"]
    assert len(g["results"]) == 4 and g["standings"]
    assert g["chat"], "recent chat should be gathered"
    # Standings and chat are keyed by GM name, not team name.
    assert any("GM 1" in s for s in g["standings"])
    assert not any("Team " in s for s in g["standings"]), g["standings"]
    print("ok: gather pulls the week's scores, standings, and chat by GM name")


def test_gather_pulls_decisions():
    conn = _seed()
    g = weeklysummary.gather(conn, 1, config.SEASON)
    # Trade: GM 1 sent Alpha Back to GM 2 for Bravo Wideout.
    assert g["trades"] and "GM 1 sent Alpha Back to GM 2 for Bravo Wideout" \
        in g["trades"][0], g["trades"]
    # Waiver: GM 3 won Bravo Wideout for $7 this week.
    assert g["waivers"] and "GM 3 won Bravo Wideout" in g["waivers"][0] \
        and "$7" in g["waivers"][0], g["waivers"]
    # Bylaw: proposer named, vote tally, outcome.
    assert g["bylaws"] and 'GM 1 proposed "No Punting Fridays"' in g["bylaws"][0] \
        and "6-1" in g["bylaws"][0], g["bylaws"]
    print("ok: gather pulls trades, waivers, and bylaws keyed to GM names")


def test_generate_stores_three_voices_idempotent():
    conn = _seed()
    assert weeklysummary.generate(conn, 1, chat_json=_fake_llm) is True
    rows = weeklysummary.week_summaries(conn)
    assert len(rows) == 1 and rows[0]["week"] == 1
    assert rows[0]["lively"] and rows[0]["neutral"] and rows[0]["roast"]
    # Second call is a no-op (already present), so no wasted model call.
    assert weeklysummary.generate(conn, 1, chat_json=lambda *a, **k: 1 / 0) is False
    print("ok: generate writes all three voices and is idempotent")


def test_generate_skips_unscored_week():
    conn = _seed(scored=False)
    assert weeklysummary.generate(conn, 1, chat_json=lambda *a, **k: 1 / 0) is False
    assert weeklysummary.week_summaries(conn) == []
    print("ok: generate skips a week with no final games (no model call)")


def test_ensure_all_backfills_then_noops():
    conn = _seed()
    assert weeklysummary.ensure_all(conn, chat_json=_fake_llm) == [1]
    # Already caught up -> nothing written, and a raising llm proves no call.
    assert weeklysummary.ensure_all(conn, chat_json=lambda *a, **k: 1 / 0) == []
    print("ok: ensure_all backfills scored weeks then no-ops")


def test_dashboard_modal_renders_switcher():
    conn = _seed()
    weeklysummary.generate(conn, 1, chat_json=_fake_llm)
    modal = dashboard._weekly_modal(conn)
    assert "id='weekly'" in modal and "Week 1" in modal
    assert "Lively recap of the week." in modal
    assert "GM 8 got absolutely smoked." in modal
    # The pure-CSS voice switcher is present (radios + labelled tabs + panels).
    assert "wr-tabs" in modal and "wr-p-l" in modal and "wr-p-r" in modal
    assert modal.count("type='radio'") == 3
    # Empty state when nothing's scored yet.
    assert "No weekly recaps yet" in dashboard._weekly_modal(_seed(scored=False))
    print("ok: dashboard weekly modal renders the three-voice switcher")


def main():
    test_gather_pulls_scores_and_standings()
    test_gather_pulls_decisions()
    test_generate_stores_three_voices_idempotent()
    test_generate_skips_unscored_week()
    test_ensure_all_backfills_then_noops()
    test_dashboard_modal_renders_switcher()
    print("\nALL OFFLINE WEEKLY-RECAP TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
