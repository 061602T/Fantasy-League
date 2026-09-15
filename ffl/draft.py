"""Snake draft engine.

Eight AI GMs draft a 15-round snake draft (120 picks). Each pick is a real
Sonnet decision: the GM sees its persona, its current roster, what it still
needs to field a legal lineup, and a menu of the best available players, then
picks one in character.

Roster shape (from config): starters QB/RB/RB/WR/WR/TE/FLEX/K/DST + 6 bench = 15.
The engine keeps every roster legal with two guards, independent of what the
model says:

* **Position caps** stop a team hoarding one slot (no 15-QB rosters).
* **Must-fill**: once a team has only as many picks left as it has mandatory
  starter holes, its menu is restricted to positions that fill one -- so every
  team finishes able to start a full, legal lineup.

Picks persist to draft_picks and rosters; each GM's one-line pick quip is
written to chat_log (event_type 'draft').
"""
from __future__ import annotations

import json
import sqlite3

import pandas as pd

from . import config, llm, projections, store

# Max players per position a team may roster (keeps rosters sane & legal).
POSITION_CAPS = {"QB": 3, "RB": 8, "WR": 8, "TE": 3, "K": 2, "DST": 2}

# Minimum players per position needed to field the starting lineup.
STARTER_MINS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1}
FLEX_MIN = 1  # one extra RB/WR/TE beyond the mins above


# --- Draft order -----------------------------------------------------------

def build_draft_order(conn: sqlite3.Connection) -> list[dict]:
    """Return the 120-pick snake order as dicts with overall/round/pick/team_id."""
    slot_team = {r["draft_slot"]: r["team_id"]
                 for r in conn.execute("SELECT team_id, draft_slot FROM teams")}
    n = len(slot_team)
    order = []
    for rnd in range(1, config.DRAFT_ROUNDS + 1):
        slots = range(1, n + 1) if rnd % 2 == 1 else range(n, 0, -1)
        for pick_in_round, slot in enumerate(slots, start=1):
            overall = (rnd - 1) * n + pick_in_round
            order.append({"overall": overall, "round": rnd,
                          "pick_in_round": pick_in_round,
                          "team_id": slot_team[slot]})
    return order


# --- Roster state ----------------------------------------------------------

def roster_counts(conn: sqlite3.Connection, team_id: int) -> dict[str, int]:
    """Current count of rostered players per position for a team."""
    rows = conn.execute(
        """SELECT p.position, COUNT(*) AS n
             FROM rosters r JOIN players p ON p.player_id = r.player_id
            WHERE r.team_id = ? AND r.dropped_week IS NULL
            GROUP BY p.position""",
        (team_id,),
    )
    return {r["position"]: r["n"] for r in rows}


def mandatory_holes(counts: dict[str, int]) -> dict[str, int]:
    """How many more of each position the team must draft to be startable.

    Includes a FLEX hole once the RB/WR/TE minimums are met but no spare
    flex-eligible player exists yet.
    """
    holes = {pos: max(0, need - counts.get(pos, 0))
             for pos, need in STARTER_MINS.items()}
    spare_flex = (max(0, counts.get("RB", 0) - STARTER_MINS["RB"])
                  + max(0, counts.get("WR", 0) - STARTER_MINS["WR"])
                  + max(0, counts.get("TE", 0) - STARTER_MINS["TE"]))
    holes["FLEX"] = max(0, FLEX_MIN - spare_flex)
    return holes


def allowed_positions(counts: dict[str, int], picks_left: int) -> set[str]:
    """Positions this team may still draft, honouring caps and must-fill."""
    under_cap = {pos for pos, cap in POSITION_CAPS.items()
                 if counts.get(pos, 0) < cap}

    holes = mandatory_holes(counts)
    total_holes = sum(holes.values())
    if picks_left > total_holes:
        return under_cap  # free phase: anything under its cap

    # Must-fill phase: only positions that satisfy an outstanding hole.
    needed: set[str] = set()
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        if holes.get(pos, 0) > 0:
            needed.add(pos)
    if holes.get("FLEX", 0) > 0:
        needed.update({"RB", "WR", "TE"})
    return (needed & under_cap) or under_cap


# --- Candidate menu --------------------------------------------------------

def candidate_menu(available: pd.DataFrame, allowed: set[str],
                   top_overall: int = 30, per_pos: int = 2) -> pd.DataFrame:
    """Best available players in allowed positions: top-N overall + a few per pos.

    Sorted by projection, so menu row 1 is always the best available (used as
    the deterministic fallback if the model returns a bad choice).
    """
    pool = available[available["position"].isin(allowed)]
    pool = pool.sort_values("proj_ppg", ascending=False)
    top = pool.head(top_overall)
    extras = pool.groupby("position", group_keys=False).head(per_pos)
    menu = (pd.concat([top, extras])
            .drop_duplicates("entity_id")
            .sort_values("proj_ppg", ascending=False)
            .reset_index(drop=True))
    return menu


# --- The pick decision -----------------------------------------------------

def _roster_summary(conn, team_id) -> str:
    rows = conn.execute(
        """SELECT p.position, p.name
             FROM rosters r JOIN players p ON p.player_id = r.player_id
            WHERE r.team_id = ? AND r.dropped_week IS NULL""",
        (team_id,),
    ).fetchall()
    if not rows:
        return "(empty)"
    by_pos: dict[str, list[str]] = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r["name"])
    return "; ".join(f"{pos}: {', '.join(names)}"
                     for pos, names in sorted(by_pos.items()))


def choose_pick(team: sqlite3.Row, rnd: int, overall: int, picks_left: int,
                roster_str: str, holes: dict[str, int],
                menu: pd.DataFrame) -> tuple[str, str]:
    """Ask the GM to pick one player from the menu. Returns (entity_id, comment).

    Falls back to the best available (menu row 1) if the model's choice is
    invalid, so the draft never stalls.
    """
    lines = []
    for i, r in enumerate(menu.itertuples(index=False), start=1):
        lines.append(f"{i:2}. {r.name} ({r.position}, {r.team or 'FA'}) "
                     f"- proj {r.proj_ppg:.1f} ppg")
    menu_text = "\n".join(lines)
    need_str = ", ".join(f"{pos} x{n}" for pos, n in holes.items() if n) or "none"

    system = (
        f"You are {team['gm_name']}, GM of \"{team['team_name']}\", drafting a "
        f"full-PPR fantasy football team. Persona: {team['personality']} "
        f"Risk tolerance: {team['risk_tolerance']}. Valuation quirk: "
        f"{team['valuation_bias']}. Draft in character, but build a team that "
        f"can win: fill a legal starting lineup and don't waste picks."
    )
    user = (
        f"Round {rnd}, overall pick #{overall}. You have {picks_left} picks "
        f"left (including this one).\n"
        f"Your roster so far: {roster_str}\n"
        f"Starting lineup to fill eventually: 1 QB, 2 RB, 2 WR, 1 TE, "
        f"1 FLEX (RB/WR/TE), 1 K, 1 DST, plus 6 bench.\n"
        f"Still-mandatory needs: {need_str}\n\n"
        f"Best available (pick by number):\n{menu_text}\n\n"
        'Choose ONE. Return JSON: {"pick": <number>, '
        '"comment": "<one short in-character line about the pick>"}.'
    )
    fallback_id = menu.iloc[0]["entity_id"]
    try:
        data = llm.chat_json(system, user, model=llm.MODEL_DECISION, max_tokens=900)
        idx = int(data.get("pick"))
        comment = str(data.get("comment", "")).strip()
        if 1 <= idx <= len(menu):
            return menu.iloc[idx - 1]["entity_id"], comment
        return fallback_id, comment
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return fallback_id, ""


# --- Orchestration ---------------------------------------------------------

def _available(conn, pool: pd.DataFrame) -> pd.DataFrame:
    drafted = {r[0] for r in conn.execute("SELECT player_id FROM draft_picks "
                                          "WHERE player_id IS NOT NULL")}
    return pool[~pool["entity_id"].isin(drafted)]


def run_draft(conn: sqlite3.Connection, force: bool = False,
              progress=None, pool: pd.DataFrame = None) -> dict:
    """Run the full snake draft, persisting picks, rosters, and pick quips.

    Returns {picks: [...]} summary. Idempotent unless force=True. `pool` may be
    supplied (columns entity_id/name/position/team/proj_ppg/n_games) to avoid
    rebuilding it from nflverse -- used by the offline tests.
    """
    existing = conn.execute("SELECT COUNT(*) FROM draft_picks").fetchone()[0]
    if existing and not force:
        raise RuntimeError(
            f"{existing} draft picks already exist. Pass force=True to redo.")
    if existing and force:
        conn.execute("DELETE FROM rosters WHERE acquired_via = 'draft'")
        conn.execute("DELETE FROM draft_picks")
        conn.execute("DELETE FROM chat_log WHERE event_type = 'draft'")
        conn.commit()

    # Players must exist before draft_picks/rosters can reference them.
    if pool is None:
        pool = projections.build_player_pool()
    store.upsert_players(conn, pool)
    pool = pool.reset_index(drop=True)

    conn.execute("UPDATE league SET status = 'draft' WHERE id = 1")
    conn.commit()

    teams = {r["team_id"]: r for r in conn.execute("SELECT * FROM teams")}
    order = build_draft_order(conn)
    n_teams = len(teams)
    summary = []

    for pick in order:
        team = teams[pick["team_id"]]
        counts = roster_counts(conn, team["team_id"])
        # Picks this team has left, including the current one.
        picks_made = sum(1 for p in summary if p["team_id"] == team["team_id"])
        picks_left = config.DRAFT_ROUNDS - picks_made
        allowed = allowed_positions(counts, picks_left)

        available = _available(conn, pool)
        menu = candidate_menu(available, allowed)
        if menu.empty:
            # A needed position was exhausted (shouldn't happen with the real
            # 32-K/32-DST pool). Fall back to any under-cap position so we at
            # least never exceed a cap; legality then depends on the pool.
            under_cap = {pos for pos, cap in POSITION_CAPS.items()
                         if counts.get(pos, 0) < cap}
            menu = candidate_menu(available, under_cap)
            if menu.empty:  # truly nothing legal left
                menu = available.sort_values("proj_ppg", ascending=False) \
                                .head(30).reset_index(drop=True)

        entity_id, comment = choose_pick(
            team, pick["round"], pick["overall"], picks_left,
            _roster_summary(conn, team["team_id"]),
            mandatory_holes(counts), menu)

        row = pool[pool["entity_id"] == entity_id].iloc[0]
        conn.execute(
            """INSERT INTO draft_picks(overall_pick, round, pick_in_round,
                 team_id, player_id, picked_at)
               VALUES(?,?,?,?,?, datetime('now'))""",
            (pick["overall"], pick["round"], pick["pick_in_round"],
             team["team_id"], entity_id))
        conn.execute(
            """INSERT INTO rosters(team_id, player_id, acquired_via, acquired_week)
               VALUES(?,?, 'draft', 0)""",
            (team["team_id"], entity_id))
        msg = (f'R{pick["round"]}.{pick["pick_in_round"]} '
               f'{team["gm_name"]} selects {row["name"]} ({row["position"]})'
               + (f' - "{comment}"' if comment else ""))
        conn.execute(
            "INSERT INTO chat_log(team_id, event_type, message) VALUES(?, 'draft', ?)",
            (team["team_id"], msg))
        conn.commit()

        entry = {**pick, "player_id": entity_id, "name": row["name"],
                 "position": row["position"], "comment": comment}
        summary.append(entry)
        if progress:
            progress(entry)

    return {"picks": summary}
