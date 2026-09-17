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

import random
import sqlite3

from . import (backup, config, dashboard, data, digest, ghpages, governance,
               market, playoffs, projections, season, store)
from . import chat as chatmod


def _current_scored_week(conn) -> int:
    row = conn.execute("SELECT current_week FROM league WHERE id=1").fetchone()
    return (row["current_week"] if row else 0) or 0


def _live_proj_map() -> dict:
    return {r.entity_id: r.proj_ppg
            for r in projections.build_projections().itertuples(index=False)}


def _midweek_chat(conn):
    """A little ambient banter about the current standings (one round)."""
    top = conn.execute(
        "SELECT team_name, wins, losses FROM teams "
        "ORDER BY wins DESC, points_for DESC LIMIT 1").fetchone()
    detail = (f"{top['team_name']} sits on top at {top['wins']}-{top['losses']}."
              if top else "")
    return chatmod.react_to_event(conn, "Midweek league chatter", detail, rounds=1)


def run_tick(conn: sqlite3.Connection, *, sync: bool = True, refresh: bool = True,
             latest_completed: int = None, do_market: bool = True,
             do_chat: bool = True, make_dashboard: bool = True,
             backup_after: bool = True, dash_path: str = None,
             db_path: str = None, proj_map: dict = None,
             do_playoffs: bool = True, do_midweek: bool = True,
             make_digest: bool = True, do_publish: bool = True,
             do_governance: bool = True, rng=None) -> dict:
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
    # Regular season only here; playoff weeks (> REGULAR_SEASON_WEEKS) are handled
    # by the bracket below.
    for wk in range(current + 1, min(latest_completed, config.REGULAR_SEASON_WEEKS) + 1):
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

    # Playoffs: build/score the bracket once the regular season is complete.
    playoff_events, newly_crowned = [], False
    if do_playoffs and playoffs.regular_season_complete(conn):
        pr = playoffs.advance(conn, latest_completed, year, proj_map=proj_map)
        playoff_events = pr["events"]
        newly_crowned = pr.get("newly_crowned", False)
        events.extend(playoff_events)

    advanced = bool(weeks_scored) or bool(playoff_events)

    # Mid-week 'life' between scored weeks -- probability-gated so idle hourly
    # ticks stay cheap; the Haiku gate still decides who actually engages.
    midweek = []
    if not advanced and do_midweek:
        r = rng or random
        if do_market and r.random() < config.MIDWEEK_TRADE_PROB:
            res = market.attempt_one_trade(conn, proj_map or _live_proj_map())
            if res and res.get("status") == "accepted":
                midweek.append("mid-week trade completed")
        if do_chat and r.random() < config.MIDWEEK_CHAT_PROB:
            if _midweek_chat(conn):
                midweek.append("mid-week chatter")
        events.extend(midweek)

    # Governance can also advance here (busy or idle). In the deployed setup the
    # 15-minute chat loop drives it; running it here too is safe (idempotent
    # close, one-bylaw-at-a-time lock) and covers a manual/scheduled run_tick.
    gov_events = governance.step(conn, rng=rng or random) if do_governance else []
    events.extend(gov_events)

    dash = dashboard.write(conn, dash_path) if make_dashboard else None

    # Backups. Crowning a champion is a non-reproducible, high-value event, so
    # take a dedicated explicit snapshot the moment it happens (like the
    # post-draft backup) rather than relying only on the routine/cron backup.
    # Any other advancing tick still gets the routine backup.
    did_backup = False
    if newly_crowned and backup_after:
        try:
            backup.backup_db(db_path=db_path or config.DB_PATH)
            events.append("championship backup taken")
            did_backup = True
        except Exception as e:  # noqa: BLE001 -- never fail a tick on backup
            events.append(f"WARNING: championship backup failed: {e}")
    if advanced and backup_after and not did_backup:
        try:
            backup.backup_db(db_path=db_path or config.DB_PATH)
        except Exception as e:  # noqa: BLE001 -- never fail a tick on backup
            events.append(f"WARNING: backup failed: {e}")

    digest_path = None
    trade_happened = any("trade" in m for m in midweek)
    if make_digest and (advanced or trade_happened):
        try:
            digest_path = digest.publish(
                conn, weeks_scored=weeks_scored, extra_events=events)["path"]
        except Exception as e:  # noqa: BLE001
            events.append(f"WARNING: digest failed: {e}")

    # Publish the dashboard to GitHub Pages when something changed. Governance
    # activity (a proposal, votes, a closed bylaw) changes the Bylaws tab and the
    # chat feed, so it triggers a publish too. A no-op unless
    # FFL_GH_DASHBOARD_TOKEN/REPO are configured, and publish() never raises.
    if do_publish and make_dashboard and dash and (
            advanced or trade_happened or gov_events):
        pub = ghpages.publish(dash)
        if pub["status"] == "published":
            events.append("dashboard published to GitHub Pages")
        elif pub["status"] == "error":
            events.append(f"WARNING: dashboard publish failed: {pub['error']}")

    status = "advanced" if advanced else ("midweek" if midweek else "idle")
    return {"status": status, "weeks_scored": weeks_scored,
            "latest_completed": latest_completed, "dashboard": dash,
            "digest": digest_path, "champion": playoffs.champion(conn),
            "events": events}
