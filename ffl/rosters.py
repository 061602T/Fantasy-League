"""Shared roster helpers used by the market (trades + waivers).

Small, SQL-light utilities for reading a team's active roster, looking up
positions, checking that a proposed add/drop keeps a roster legal, and finding
the league's current week. Legality reuses the draft engine's roster/needs
accounting so "legal" means the same thing everywhere.
"""
from __future__ import annotations

import sqlite3

from . import config, draft


def current_week(conn: sqlite3.Connection) -> int:
    """The league's active week (falls back to the data as-of week)."""
    row = conn.execute("SELECT current_week FROM league WHERE id = 1").fetchone()
    wk = row["current_week"] if row else 0
    return wk or config.AS_OF_WEEK


def active_roster(conn: sqlite3.Connection, team_id: int) -> list[sqlite3.Row]:
    """Rows (player_id, name, position) currently rostered by a team."""
    return conn.execute(
        """SELECT p.player_id, p.name, p.position
             FROM rosters r JOIN players p ON p.player_id = r.player_id
            WHERE r.team_id = ? AND r.dropped_week IS NULL
            ORDER BY p.position, p.name""",
        (team_id,)).fetchall()


def positions_of(conn: sqlite3.Connection, player_ids) -> dict[str, str]:
    """player_id -> position for the given ids."""
    if not player_ids:
        return {}
    marks = ",".join("?" * len(player_ids))
    return {r["player_id"]: r["position"] for r in conn.execute(
        f"SELECT player_id, position FROM players WHERE player_id IN ({marks})",
        list(player_ids))}


def owner_of(conn: sqlite3.Connection, player_id: str):
    """team_id currently rostering a player, or None if it's a free agent."""
    row = conn.execute(
        "SELECT team_id FROM rosters WHERE player_id = ? AND dropped_week IS NULL",
        (player_id,)).fetchone()
    return row["team_id"] if row else None


def legal_after(conn: sqlite3.Connection, team_id: int,
                add_ids=(), drop_ids=()) -> bool:
    """True if the roster is still full (15) and startable after add/drop."""
    counts = dict(draft.roster_counts(conn, team_id))
    pos = positions_of(conn, list(add_ids) + list(drop_ids))
    for pid in drop_ids:
        counts[pos[pid]] = counts.get(pos[pid], 0) - 1
    for pid in add_ids:
        counts[pos[pid]] = counts.get(pos[pid], 0) + 1
    total = sum(counts.values())
    return total == config.ROSTER_SIZE and sum(draft.mandatory_holes(counts).values()) == 0
