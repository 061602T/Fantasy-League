"""Weekly scoring cycle: schedule, lineups, head-to-head results, standings.

The league plays a head-to-head regular season. This module:

* builds a balanced double round-robin **schedule** (REGULAR_SEASON_WEEKS),
* sets each team's weekly **lineup** deterministically -- the highest-*projected*
  eligible player for each starting slot (no hindsight; actual points decide the
  score, projections only decide who starts),
* **scores** a week from real per-player weekly fantasy points and finalizes each
  matchup, then
* recomputes **standings** by aggregating all final matchups (so re-scoring a
  week is idempotent -- never double-counts).

League week N maps to NFL week N. Scoring reads actual points for a chosen
`season`, so the same engine drives live 2026 play (season=2026, week=1 today)
and a completed-season backtest (season=2025, weeks 1..N).
"""
from __future__ import annotations

import sqlite3

from . import config, effects, projections

# Starting-lineup slots in fill order. FLEX is filled last from the best
# remaining flex-eligible player.
_SLOT_FILL = [
    ("QB", ("QB",), 1),
    ("RB", ("RB",), 2),
    ("WR", ("WR",), 2),
    ("TE", ("TE",), 1),
    ("K", ("K",), 1),
    ("DST", ("DST",), 1),
    ("FLEX", config.FLEX_POSITIONS, 1),
]


# --- Schedule --------------------------------------------------------------

def round_robin(team_ids: list[int], weeks: int) -> list[list[tuple[int, int]]]:
    """Balanced (home, away) pairings per week via the circle method.

    Produces a double round-robin over `weeks`: the second time two teams meet,
    home/away is swapped. Assumes an even number of teams.
    """
    n = len(team_ids)
    arr = list(team_ids)
    single = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[n - 1 - i]
            # Alternate home/away by board position so it's not always the same.
            pairs.append((a, b) if i % 2 == 0 else (b, a))
        single.append(pairs)
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]  # rotate, fixing arr[0]

    schedule = []
    for w in range(weeks):
        base = single[w % (n - 1)]
        # On the repeat cycle, swap home/away for balance.
        swap = (w // (n - 1)) % 2 == 1
        schedule.append([(b, a) if swap else (a, b) for (a, b) in base])
    return schedule


def build_schedule(conn: sqlite3.Connection, weeks: int = None,
                   force: bool = False) -> int:
    """Insert the round-robin matchups (status 'scheduled'). Idempotent."""
    weeks = weeks or config.REGULAR_SEASON_WEEKS
    existing = conn.execute("SELECT COUNT(*) FROM matchups").fetchone()[0]
    if existing and not force:
        return 0
    if existing and force:
        conn.execute("DELETE FROM matchups")

    team_ids = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY draft_slot")]
    schedule = round_robin(team_ids, weeks)
    n = 0
    for week, pairs in enumerate(schedule, start=1):
        for home, away in pairs:
            conn.execute(
                """INSERT INTO matchups(week, home_team_id, away_team_id, status)
                   VALUES(?,?,?, 'scheduled')""",
                (week, home, away))
            n += 1
    conn.commit()
    return n


# --- Lineups ---------------------------------------------------------------

def _projection_map() -> dict[str, float]:
    """entity_id -> projected PPG, for ranking who starts."""
    proj = projections.build_projections()
    return {r.entity_id: r.proj_ppg for r in proj.itertuples(index=False)}


def optimal_lineup(players: list[dict],
                   force_kicker_flex: bool = False) -> dict[str, list[str]]:
    """Pick starters from a roster by projection. Pure function (testable).

    `players`: list of {player_id, position, proj}. Returns slot -> [player_id].
    Any player not slotted is implicitly BENCH.

    `force_kicker_flex` (governance ``kicker_flex_lock``): the roster's best
    kicker is placed in FLEX instead of K, and K is left empty for the week --
    it does not fall back to filling K from anyone else.
    """
    pool = sorted(players, key=lambda p: p["proj"], reverse=True)
    used: set[str] = set()
    lineup: dict[str, list[str]] = {}
    kicker_id = None
    if force_kicker_flex:
        kicker_id = next((p["player_id"] for p in pool if p["position"] == "K"), None)
    for slot, eligible, count in _SLOT_FILL:
        if force_kicker_flex and slot == "K":
            lineup[slot] = []
            continue
        if force_kicker_flex and slot == "FLEX":
            lineup[slot] = []
            if kicker_id is not None:
                lineup[slot] = [kicker_id]
                used.add(kicker_id)
            continue
        picked = []
        for p in pool:
            if len(picked) == count:
                break
            if p["player_id"] in used or p["position"] not in eligible:
                continue
            picked.append(p["player_id"])
            used.add(p["player_id"])
        lineup[slot] = picked
    return lineup


def set_lineup(conn: sqlite3.Connection, team_id: int, week: int,
               proj_map: dict[str, float]) -> dict[str, list[str]]:
    """Compute and persist a team's lineup for a week. Returns the lineup."""
    roster = conn.execute(
        """SELECT p.player_id, p.position
             FROM rosters r JOIN players p ON p.player_id = r.player_id
            WHERE r.team_id = ? AND r.dropped_week IS NULL""",
        (team_id,)).fetchall()
    players = [{"player_id": r["player_id"], "position": r["position"],
                "proj": proj_map.get(r["player_id"], 0.0)} for r in roster]
    # Governance kicker_flex_lock: a litigating team's kicker starts at FLEX.
    force_kicker_flex = team_id in effects.active_team_ids(
        conn, "kicker_flex_lock", week)
    lineup = optimal_lineup(players, force_kicker_flex=force_kicker_flex)

    starters = {pid for pids in lineup.values() for pid in pids}
    conn.execute("DELETE FROM lineups WHERE team_id = ? AND week = ?",
                 (team_id, week))
    for slot, pids in lineup.items():
        for pid in pids:
            conn.execute(
                "INSERT INTO lineups(team_id, week, player_id, slot) VALUES(?,?,?,?)",
                (team_id, week, pid, slot))
    for p in players:                       # everyone else is BENCH
        if p["player_id"] not in starters:
            conn.execute(
                "INSERT INTO lineups(team_id, week, player_id, slot) VALUES(?,?,?,'BENCH')",
                (team_id, week, p["player_id"]))
    conn.commit()
    return lineup


# --- Scoring ---------------------------------------------------------------

def _weekly_points(conn, season: int, week: int) -> dict[str, float]:
    """player_id -> actual fantasy points for (season, week); missing = absent."""
    rows = conn.execute(
        "SELECT player_id, fantasy_points FROM player_weekly_scores "
        "WHERE season = ? AND week = ?", (season, week))
    return {r["player_id"]: r["fantasy_points"] for r in rows}


def team_week_score(conn, team_id: int, week: int,
                    points: dict[str, float]) -> float:
    """Sum a team's STARTERS' actual points for a week (bench excluded)."""
    starters = conn.execute(
        "SELECT player_id FROM lineups WHERE team_id = ? AND week = ? "
        "AND slot != 'BENCH'", (team_id, week))
    return round(sum(points.get(r["player_id"], 0.0) for r in starters), 2)


def score_week(conn: sqlite3.Connection, week: int, season: int = None,
               proj_map: dict[str, float] = None) -> list[dict]:
    """Set lineups, score every matchup in `week`, and recompute standings.

    Actual points are read for (season, week); league week N == NFL week N.
    `proj_map` (entity_id -> projected PPG) drives lineup choice; built from the
    real projections when omitted. Idempotent: safe to re-run a week.
    """
    season = season or config.SEASON
    if proj_map is None:
        proj_map = _projection_map()
    points = _weekly_points(conn, season, week)

    team_ids = [r["team_id"] for r in conn.execute("SELECT team_id FROM teams")]
    for tid in team_ids:
        set_lineup(conn, tid, week, proj_map)

    results = []
    matchups = conn.execute(
        "SELECT matchup_id, home_team_id, away_team_id FROM matchups WHERE week = ?",
        (week,)).fetchall()
    for m in matchups:
        hp = team_week_score(conn, m["home_team_id"], week, points)
        ap = team_week_score(conn, m["away_team_id"], week, points)
        # NULL winner on a tie (there is no team 0 to reference); a final status
        # distinguishes a tie from an unplayed game.
        winner = (m["home_team_id"] if hp > ap
                  else m["away_team_id"] if ap > hp else None)
        conn.execute(
            """UPDATE matchups SET home_points=?, away_points=?, winner_team_id=?,
                 status='final' WHERE matchup_id=?""",
            (hp, ap, winner, m["matchup_id"]))
        results.append({"matchup_id": m["matchup_id"],
                        "home_team_id": m["home_team_id"], "home_points": hp,
                        "away_team_id": m["away_team_id"], "away_points": ap,
                        "winner_team_id": winner})
    conn.commit()
    recompute_standings(conn)
    return results


def recompute_standings(conn: sqlite3.Connection) -> None:
    """Rebuild every team's W/L/T and points for/against from final matchups.

    Aggregating from scratch (rather than incrementing) makes re-scoring safe.
    """
    agg = {r["team_id"]: {"w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0}
           for r in conn.execute("SELECT team_id FROM teams")}
    # Regular-season records only -- playoff weeks (> REGULAR_SEASON_WEEKS) are
    # single-elimination and must not count toward W-L/PF standings.
    finals = conn.execute(
        """SELECT home_team_id, away_team_id, home_points, away_points,
                  winner_team_id FROM matchups
            WHERE status='final' AND week <= ?""", (config.REGULAR_SEASON_WEEKS,))
    for m in finals:
        h, a = m["home_team_id"], m["away_team_id"]
        hp, ap = m["home_points"] or 0.0, m["away_points"] or 0.0
        agg[h]["pf"] += hp; agg[h]["pa"] += ap
        agg[a]["pf"] += ap; agg[a]["pa"] += hp
        if m["winner_team_id"] is None:   # final + no winner = tie
            agg[h]["t"] += 1; agg[a]["t"] += 1
        elif m["winner_team_id"] == h:
            agg[h]["w"] += 1; agg[a]["l"] += 1
        else:
            agg[a]["w"] += 1; agg[h]["l"] += 1
    for tid, s in agg.items():
        conn.execute(
            """UPDATE teams SET wins=?, losses=?, ties=?, points_for=?,
                 points_against=? WHERE team_id=?""",
            (s["w"], s["l"], s["t"], round(s["pf"], 2), round(s["pa"], 2), tid))
    conn.commit()


def standings(conn: sqlite3.Connection) -> list[dict]:
    """Teams ordered by wins, then points_for (a common fantasy tiebreaker)."""
    rows = conn.execute(
        """SELECT team_name, gm_name, wins, losses, ties, points_for,
                  points_against FROM teams
            ORDER BY wins DESC, points_for DESC""")
    return [dict(r) for r in rows]
