"""Persistence helpers that bridge the data/projection layer and the DB.

Keeps SQL out of the analytics modules and gives the rest of the system a
small, tested vocabulary for reading/writing league state.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from . import projections


def upsert_players(conn: sqlite3.Connection, pool: pd.DataFrame) -> int:
    """Insert/update the draftable player identities from a pool DataFrame.

    Expects columns: entity_id, name, position, team. Returns row count.
    """
    rows = [
        (r.entity_id, r.name, r.position, r.team, 1 if r.position == "DST" else 0)
        for r in pool.itertuples(index=False)
    ]
    conn.executemany(
        """INSERT INTO players(player_id, name, position, nfl_team, is_dst)
           VALUES(?,?,?,?,?)
           ON CONFLICT(player_id) DO UPDATE SET
             name=excluded.name, position=excluded.position,
             nfl_team=excluded.nfl_team, is_dst=excluded.is_dst""",
        rows,
    )
    conn.commit()
    return len(rows)


def store_weekly_scores(conn: sqlite3.Connection, game_logs: pd.DataFrame) -> int:
    """Persist actual per-player/DST weekly fantasy points from game logs.

    Only stores scores for players already present in `players` (FK), so call
    upsert_players first. Expects columns: entity_id, season, week, fpts.
    """
    known = {r[0] for r in conn.execute("SELECT player_id FROM players")}
    rows = [
        (r.entity_id, int(r.season), int(r.week), float(r.fpts))
        for r in game_logs.itertuples(index=False)
        if r.entity_id in known
    ]
    conn.executemany(
        """INSERT INTO player_weekly_scores(player_id, season, week, fantasy_points)
           VALUES(?,?,?,?)
           ON CONFLICT(player_id, season, week) DO UPDATE SET
             fantasy_points=excluded.fantasy_points,
             computed_at=datetime('now')""",
        rows,
    )
    conn.commit()
    return len(rows)


def sync_pool_and_scores(conn: sqlite3.Connection):
    """Convenience: build the current pool + game logs and persist both."""
    pool = projections.build_player_pool()
    game_logs = projections.build_game_logs()
    n_players = upsert_players(conn, pool)
    n_scores = store_weekly_scores(conn, game_logs)
    return n_players, n_scores
