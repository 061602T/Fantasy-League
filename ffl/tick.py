"""The tick loop: one autonomous step of the league.

A tick is cheap by default. It refreshes the current season's real results,
and only does real work -- and spends LLM budget -- when a new NFL week has
finished: it scores that league week, runs waivers, and lets the GMs react in
the group chat. Every tick (busy or idle) regenerates the dashboard; a tick
that advanced the league also takes a backup.

Designed to be safe to run every hour (or any cadence): if nothing new has
completed, it just refreshes the dashboard and returns "idle". Catch-up is
automatic -- if several weeks completed since the last run, it scores each.
"""
from __future__ import annotations

import sqlite3

from . import (backup, config, dashboard, data, market, projections, season,
               store)
from . import chat as chatmod


def _current_scored_week(conn) -> int:
    row = conn.execute("SELECT current_week FROM league WHERE id=1").fetchone()
    return (row["current_week"] if row else 0) or 0


def run_tick(conn: sqlite3.Connection, *, sync: bool = True, refresh: bool = True,
             latest_completed: int = None, do_market: bool = True,
             do_chat: bool = True, make_dashboard: bool = True,
             backup_after: bool = True, dash_path: str = None,
             db_path: str = None, proj_map: dict = None) -> dict:
    """Run one tick. Returns a summary dict.

    Params exist mostly for testing: `sync`/`refresh` control real data access,
    `latest_completed` overrides week detection, and the do_* flags gate the
    API-spending steps.
    """
    n_teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    if n_teams < config.NUM_TEAMS:
        return {"status": "no_league",
                "detail": "run gen_personas + run_draft first"}

    year = config.SEASON
    if latest_completed is None:
        if refresh:
            data.refresh_season(year)
        latest_completed = min(data.latest_completed_week(year),
                               config.REGULAR_SEASON_WEEKS)

    season.build_schedule(conn)  # idempotent
    current = _current_scored_week(conn)

    if sync and latest_completed > current:
        store.store_weekly_scores(
            conn, projections.build_game_logs(year, latest_completed))

    events, weeks_scored = [], []
    for wk in range(current + 1, latest_completed + 1):
        have = conn.execute(
            "SELECT COUNT(*) FROM player_weekly_scores WHERE season=? AND week=?",
            (year, wk)).fetchone()[0]
        if not have:
            continue
        season.score_week(conn, wk, year, proj_map=proj_map)
        conn.execute("UPDATE league SET current_week=?, status='regular' WHERE id=1",
                     (wk,))
        conn.commit()
        weeks_scored.append(wk)
        events.append(f"scored week {wk}")
        if do_market:
            claims = market.run_waivers(conn, week=wk)
            won = sum(1 for c in claims if c["status"] == "processed")
            if claims:
                events.append(f"week {wk}: {won}/{len(claims)} waiver claims won")
        if do_chat:
            posts = chatmod.react_to_week(conn, wk)
            if posts:
                events.append(f"week {wk}: {len(posts)} chat posts")

    dash = dashboard.write(conn, dash_path) if make_dashboard else None
    if weeks_scored and backup_after:
        try:
            backup.backup_db(db_path=db_path or config.DB_PATH)
        except Exception as e:  # noqa: BLE001 -- never fail a tick on backup
            events.append(f"WARNING: backup failed: {e}")

    return {"status": "advanced" if weeks_scored else "idle",
            "weeks_scored": weeks_scored, "latest_completed": latest_completed,
            "dashboard": dash, "events": events}
