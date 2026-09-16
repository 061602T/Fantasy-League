"""Single-elimination playoff bracket.

After the regular season, the top `PLAYOFF_TEAMS` seeds (by standings) play a
single-elimination bracket in the NFL weeks after REGULAR_SEASON_WEEKS. Round 1
pairs seeds high-vs-low (1 v N, 2 v N-1, ...); winners re-seed each round; the
higher seed is the home team and advances on a tie. Games are scored with the
regular scoring engine, but on playoff weeks (> REGULAR_SEASON_WEEKS) they don't
count toward regular-season records.

`advance` is idempotent and season-parameterized: the tick drives it for the
live season, the 2025 backtest drives it for a finished one.
"""
from __future__ import annotations

import sqlite3

from . import config, season


def _bracket_size() -> int:
    """Largest power of two <= PLAYOFF_TEAMS and <= NUM_TEAMS (>=2)."""
    n = min(config.PLAYOFF_TEAMS, config.NUM_TEAMS)
    size = 1
    while size * 2 <= n:
        size *= 2
    return max(size, 2)


def num_rounds() -> int:
    n, r = _bracket_size(), 0
    while n > 1:
        n //= 2
        r += 1
    return r


def week_of_round(rnd: int) -> int:
    return config.REGULAR_SEASON_WEEKS + rnd


def regular_season_complete(conn) -> bool:
    played = conn.execute(
        "SELECT COUNT(*) FROM matchups WHERE status='final' AND week <= ?",
        (config.REGULAR_SEASON_WEEKS,)).fetchone()[0]
    return played >= config.REGULAR_SEASON_WEEKS * (config.NUM_TEAMS // 2)


def seeds(conn) -> list[int]:
    """Top-`bracket_size` team_ids in seed order (1st = top seed)."""
    order = [r["team_id"] for r in conn.execute(
        "SELECT team_id FROM teams ORDER BY wins DESC, points_for DESC")]
    return order[:_bracket_size()]


def _pair_high_low(participants: list[int], seed_rank: dict[int, int]):
    """Pair the field high-vs-low by seed; home = better seed. Returns list of
    (home, away)."""
    ordered = sorted(participants, key=lambda t: seed_rank[t])
    pairs = []
    i, j = 0, len(ordered) - 1
    while i < j:
        pairs.append((ordered[i], ordered[j]))  # better seed is home
        i += 1
        j -= 1
    return pairs


def _matchups_at(conn, week):
    return conn.execute(
        """SELECT matchup_id, home_team_id, away_team_id, winner_team_id, status
             FROM matchups WHERE week=?""", (week,)).fetchall()


def _winners_at(conn, week, seed_rank):
    rows = _matchups_at(conn, week)
    return [m["winner_team_id"] for m in rows if m["winner_team_id"] is not None]


def _resolve_ties(conn, week):
    """Playoffs can't tie: the home team (better seed) advances on equal points."""
    for m in _matchups_at(conn, week):
        if m["status"] == "final" and m["winner_team_id"] is None:
            conn.execute("UPDATE matchups SET winner_team_id=? WHERE matchup_id=?",
                         (m["home_team_id"], m["matchup_id"]))
    conn.commit()


def champion(conn):
    """Champion team_id once the final is decided, else None."""
    fw = week_of_round(num_rounds())
    row = conn.execute(
        "SELECT winner_team_id FROM matchups WHERE week=? AND status='final'",
        (fw,)).fetchone()
    return row["winner_team_id"] if row else None


def advance(conn: sqlite3.Connection, latest_completed: int,
            season_year: int = None, proj_map: dict = None) -> dict:
    """Build/score the bracket as far as available data allows. Idempotent."""
    season_year = season_year or config.SEASON
    if not regular_season_complete(conn):
        return {"status": "regular_incomplete", "events": []}

    order = seeds(conn)
    seed_rank = {tid: i for i, tid in enumerate(order)}
    events = []

    for rnd in range(1, num_rounds() + 1):
        wk = week_of_round(rnd)
        existing = _matchups_at(conn, wk)

        if not existing:
            if rnd == 1:
                participants = order
            else:
                prev = _matchups_at(conn, wk - 1)
                if not prev or any(m["status"] != "final" for m in prev):
                    break  # previous round not finished yet
                participants = _winners_at(conn, wk - 1, seed_rank)
            for home, away in _pair_high_low(participants, seed_rank):
                conn.execute(
                    """INSERT INTO matchups(week, home_team_id, away_team_id, status)
                       VALUES(?,?,?, 'scheduled')""", (wk, home, away))
            conn.commit()
            conn.execute("UPDATE league SET status='playoffs' WHERE id=1")
            conn.commit()
            events.append(f"round {rnd} bracket set (week {wk})")
            existing = _matchups_at(conn, wk)

        if any(m["status"] != "final" for m in existing):
            have = conn.execute(
                "SELECT COUNT(*) FROM player_weekly_scores WHERE season=? AND week=?",
                (season_year, wk)).fetchone()[0]
            if latest_completed >= wk and have:
                season.score_week(conn, wk, season_year, proj_map=proj_map)
                _resolve_ties(conn, wk)
                events.append(f"round {rnd} scored (week {wk})")
            else:
                break  # data for this round isn't in yet

    champ = champion(conn)
    if champ is not None:
        conn.execute("UPDATE league SET status='complete' WHERE id=1")
        conn.commit()
        name = conn.execute("SELECT team_name FROM teams WHERE team_id=?",
                            (champ,)).fetchone()["team_name"]
        events.append(f"CHAMPION: {name}")

    return {"status": "complete" if champ is not None else "in_progress",
            "events": events, "champion": champ}
