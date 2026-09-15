"""Offline tests for the snake draft engine -- no API calls.

Stubs the pick decision with a deterministic greedy chooser and runs a full
120-pick draft on a synthetic pool, then checks the invariants that matter:
snake order, no duplicate picks, and every team finishes with a full, legal,
startable roster. Run:  python -m scripts.test_draft
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from ffl import db, draft, config


def _synthetic_pool():
    """A pool mirroring the real POOL_TARGETS depth (32 K / 32 DST, etc.)."""
    counts = dict(config.POOL_TARGETS)
    rows = []
    for pos, n in counts.items():
        for i in range(n):
            rows.append({
                "entity_id": f"{pos}{i:02d}",
                "position": pos,
                "name": f"{pos} Player {i:02d}",
                "team": "FA",
                "proj_ppg": round(30 - i * 0.5, 2),  # descending within position
                "n_games": 10,
            })
    return pd.DataFrame(rows)


def _seed_league(conn):
    conn.execute(
        """INSERT INTO league(id, season, as_of_week, current_week, num_teams, status)
           VALUES(1, 2026, 1, 0, 8, 'setup')""")
    for slot in range(1, 9):
        conn.execute(
            """INSERT INTO teams(team_name, gm_name, personality, risk_tolerance,
                 valuation_bias, chattiness, draft_slot)
               VALUES(?,?,?,?,?,?,?)""",
            (f"Team {slot}", f"GM {slot}", "plays hard", "balanced",
             "none", "moderate", slot))
    conn.commit()


def test_snake_order():
    conn = db.init_db(":memory:")
    _seed_league(conn)
    order = draft.build_draft_order(conn)
    assert len(order) == 8 * config.DRAFT_ROUNDS == 120
    # Round 1 ascending by slot, round 2 descending -> snake.
    slots = {r["draft_slot"]: r["team_id"] for r in conn.execute(
        "SELECT team_id, draft_slot FROM teams")}
    r1 = [p["team_id"] for p in order if p["round"] == 1]
    r2 = [p["team_id"] for p in order if p["round"] == 2]
    assert r1 == [slots[s] for s in range(1, 9)]
    assert r2 == [slots[s] for s in range(8, 0, -1)]
    # Overall picks are 1..120 with no gaps.
    assert [p["overall"] for p in order] == list(range(1, 121))
    print("ok: snake order (120 picks, 1<->8 serpentine, contiguous overalls)")


def test_full_legal_draft():
    conn = db.init_db(":memory:")
    _seed_league(conn)

    # Deterministic GM: always take the best available on the menu (row 1).
    def greedy(team, rnd, overall, picks_left, roster_str, holes, menu):
        return menu.iloc[0]["entity_id"], "chalk"
    draft.choose_pick = greedy

    result = draft.run_draft(conn, pool=_synthetic_pool())
    picks = result["picks"]
    assert len(picks) == 120

    # No player drafted twice.
    ids = [p["player_id"] for p in picks]
    assert len(ids) == len(set(ids)), "a player was drafted more than once"

    # Every team: exactly 15 players and a legal, startable lineup.
    for tid in [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]:
        counts = draft.roster_counts(conn, tid)
        total = sum(counts.values())
        assert total == config.DRAFT_ROUNDS == 15, f"team {tid} has {total} players"
        holes = draft.mandatory_holes(counts)
        assert sum(holes.values()) == 0, f"team {tid} cannot start a lineup: {counts}"

    # draft_picks and rosters both fully populated.
    assert conn.execute("SELECT COUNT(*) FROM draft_picks").fetchone()[0] == 120
    assert conn.execute(
        "SELECT COUNT(*) FROM rosters WHERE acquired_via='draft'").fetchone()[0] == 120
    assert conn.execute(
        "SELECT COUNT(*) FROM chat_log WHERE event_type='draft'").fetchone()[0] == 120
    print("ok: full draft (120 unique picks, 8 legal 15-man rosters, all persisted)")


def test_must_fill_guard():
    # A team that already has everything but K/DST, with only 2 picks left,
    # must be restricted to K and DST.
    counts = {"QB": 1, "RB": 3, "WR": 3, "TE": 2, "K": 0, "DST": 0}
    allowed = draft.allowed_positions(counts, picks_left=2)
    assert allowed == {"K", "DST"}, allowed
    # With plenty of picks left, it's a free-for-all (all under-cap positions).
    free = draft.allowed_positions(counts, picks_left=8)
    assert "RB" in free and "WR" in free and "QB" in free
    print("ok: must-fill guard restricts the endgame to mandatory positions")


def main():
    test_snake_order()
    test_must_fill_guard()
    test_full_legal_draft()
    print("\nALL OFFLINE DRAFT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
