"""Weekly score projections (deterministic, no API calls).

Feature 1 of the analytics set. Projects fantasy points for a league week from
*recent actual scoring* only -- a plain statistical estimate, not an LLM guess,
not a prediction of the real NFL result, and not the league's actual points.

How a number is built
---------------------
* **Per player:** the simple mean of the player's most recent
  ``SCORE_PROJ_WINDOW`` (default 4) actual weekly scores *strictly before* the
  target week, read from ``player_weekly_scores``. "Most recent" spans seasons
  (it dips into the prior season when the current one has fewer than the window
  many games yet), so an early-season week isn't projected off a single game.
  A player with no prior game returns ``None`` (no basis to project).

* **Bye weeks:** a starter whose NFL team is idle that week is projected at 0 --
  they can't score. Byes are known in advance from the schedule, so this is not
  hindsight; validated on the 2025 backtest it cuts team-total error by ~35%
  (MAE 28->18) and removes a systematic +13 pt over-projection. The core
  functions take the set of teams-on-bye as an argument (so the math stays pure
  and testable); :func:`teams_on_bye` fills it from the schedule for live use.

* **Per team:** the sum of the team's starters' player projections. For a week
  whose lineup is already set (a scored week) the *actual* starters are valued;
  for an upcoming week the optimal lineup is chosen from the current roster by
  these same projections (via :func:`season.optimal_lineup`), avoiding bye
  players. A starter with no projection basis contributes 0 and is counted in
  ``n_uncovered``; a starter on bye contributes 0 and is counted in ``n_bye``.

The point of the per-team number is the *projected vs actual* comparison the
dashboard shows once a week is scored: what recent form said the fielded lineup
would score, next to what it actually scored.
"""
from __future__ import annotations

import sqlite3

from . import config, season


def teams_on_bye(season_year: int, week: int) -> set[str]:
    """NFL team abbreviations idle in (season_year, week), from the schedule.

    A team is on bye when it plays no game that week. Reads the schedule via the
    data layer (parquet cache), so it may touch disk/network; callers that must
    stay pure pass an explicit set to the projection functions instead. Returns
    an empty set if the schedule can't be read, degrading to no bye filtering.
    """
    try:
        from . import data
        sch = data.schedules([season_year])
        sch = sch[sch["season"] == season_year]
        all_teams = set(sch["home_team"]) | set(sch["away_team"])
        wk = sch[sch["week"] == week]
        playing = set(wk["home_team"]) | set(wk["away_team"])
        return {t for t in (all_teams - playing) if t}
    except Exception:  # noqa: BLE001 -- never let a schedule read break a render
        return set()


def _window(window: int | None) -> int:
    return window if window is not None else config.SCORE_PROJ_WINDOW


def project_player(conn: sqlite3.Connection, player_id: str, season_year: int,
                   week: int, window: int | None = None) -> float | None:
    """Mean of the player's most recent `window` actual scores before `week`.

    Recency spans seasons via a (season, week) chronological key, so the window
    is the last `window` games played before the target week regardless of the
    season boundary. Returns None when the player has no prior game.
    """
    window = _window(window)
    target = season_year * 100 + week
    rows = conn.execute(
        """SELECT fantasy_points FROM player_weekly_scores
            WHERE player_id = ? AND (season * 100 + week) < ?
            ORDER BY season DESC, week DESC LIMIT ?""",
        (player_id, target, window)).fetchall()
    if not rows:
        return None
    return round(sum(r["fantasy_points"] for r in rows) / len(rows), 2)


def _actual_starters(conn, team_id, week):
    """The starters recorded for a (set) week, or None if no lineup exists yet."""
    rows = conn.execute(
        """SELECT l.player_id, p.name, p.position, p.nfl_team
             FROM lineups l JOIN players p ON p.player_id = l.player_id
            WHERE l.team_id = ? AND l.week = ? AND l.slot != 'BENCH'""",
        (team_id, week)).fetchall()
    return [{"player_id": r["player_id"], "name": r["name"],
             "position": r["position"], "nfl_team": r["nfl_team"]}
            for r in rows] or None


def _projected_starters(conn, team_id, season_year, week, window, bye_teams):
    """Optimal starting lineup from the current roster, ranked by our weekly
    projection (used for an upcoming week that has no lineup set). Bye-week
    players project 0, so the optimal lineup naturally avoids them."""
    roster = conn.execute(
        """SELECT p.player_id, p.name, p.position, p.nfl_team
             FROM rosters r JOIN players p ON p.player_id = r.player_id
            WHERE r.team_id = ? AND r.dropped_week IS NULL""",
        (team_id,)).fetchall()
    meta = {r["player_id"]: {"name": r["name"], "position": r["position"],
                             "nfl_team": r["nfl_team"]} for r in roster}
    players = []
    for r in roster:
        on_bye = r["nfl_team"] in bye_teams
        proj = 0.0 if on_bye else (project_player(conn, r["player_id"],
                                                  season_year, week, window) or 0.0)
        players.append({"player_id": r["player_id"], "position": r["position"],
                        "proj": proj})
    lineup = season.optimal_lineup(players)
    starter_ids = [pid for pids in lineup.values() for pid in pids]
    return [{"player_id": pid, "name": meta[pid]["name"],
             "position": meta[pid]["position"], "nfl_team": meta[pid]["nfl_team"]}
            for pid in starter_ids]


def project_team(conn: sqlite3.Connection, team_id: int, season_year: int,
                 week: int, window: int | None = None,
                 bye_teams: set[str] | None = None) -> dict:
    """Project a team's total for `week` as the sum of its starters' projections.

    Uses the actual starters when a lineup is set (scored week), otherwise the
    optimal lineup by these projections (upcoming week). Starters whose NFL team
    is in `bye_teams` are projected at 0 (they can't score). Returns the total,
    the starter breakdown, and coverage counts (`n_covered`, `n_uncovered`,
    `n_bye`).
    """
    window = _window(window)
    bye_teams = bye_teams or set()
    starters = _actual_starters(conn, team_id, week) \
        or _projected_starters(conn, team_id, season_year, week, window, bye_teams)

    total, covered, bye, out = 0.0, 0, 0, []
    for s in starters:
        if s.get("nfl_team") in bye_teams:
            bye += 1
            out.append({**s, "proj": 0.0, "on_bye": True})
            continue
        pr = project_player(conn, s["player_id"], season_year, week, window)
        if pr is not None:
            total += pr
            covered += 1
        out.append({**s, "proj": pr, "on_bye": False})
    return {"team_id": team_id, "proj": round(total, 2),
            "n_starters": len(starters), "n_covered": covered,
            "n_uncovered": len(starters) - covered - bye, "n_bye": bye,
            "starters": out}


def matchup_projections(conn: sqlite3.Connection, week: int,
                        season_year: int | None = None,
                        window: int | None = None,
                        bye_teams: set[str] | None = None) -> list[dict]:
    """Per-matchup projected totals for `week`, with actuals when the game is
    final. Each row: home/away team ids + names, *_proj, *_actual (or None),
    and `final`. `bye_teams` (from :func:`teams_on_bye`) zeroes bye starters."""
    season_year = season_year if season_year is not None else config.SEASON
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    out = []
    for m in conn.execute(
            """SELECT home_team_id, away_team_id, home_points, away_points, status
                 FROM matchups WHERE week = ? ORDER BY matchup_id""",
            (week,)).fetchall():
        h, a = m["home_team_id"], m["away_team_id"]
        final = m["status"] == "final"
        out.append({
            "home_team_id": h, "away_team_id": a,
            "home": names.get(h, "?"), "away": names.get(a, "?"),
            "home_proj": project_team(conn, h, season_year, week, window, bye_teams)["proj"],
            "away_proj": project_team(conn, a, season_year, week, window, bye_teams)["proj"],
            "home_actual": m["home_points"] if final else None,
            "away_actual": m["away_points"] if final else None,
            "final": final,
        })
    return out
