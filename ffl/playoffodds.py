"""Playoff odds via Monte-Carlo simulation (deterministic given a seed, no API).

Feature 3 of the analytics set. Estimates each team's chance of finishing the
regular season in the top `PLAYOFF_TEAMS` (the seeds that make the bracket),
given the current standings and the remaining schedule -- a statistical
estimate, not a prediction of the real games.

Method
------
Take each team's current record (wins, points_for) as fixed, then simulate the
rest of the regular season many times. Each remaining game draws both teams'
scores from their season-so-far scoring distribution -- the same
`Normal(mean, sd)` used by win probability (``winprob.team_dist``), with sd
floored at the pooled league spread -- and awards the win plus the points. After
each simulated season the teams are re-seeded exactly as the league does
(`wins` desc, then `points_for` desc) and the top `bracket_size` are flagged.
The odds are the fraction of simulations in which a team made that cut.

The whole run is vectorized over simulations with numpy, so the default 10,000
runs cost only a few milliseconds -- cheap enough to recompute every tick. When
the regular season is already complete (no remaining games) the result is
deterministic: the current top seeds are 100%, everyone else 0%.
"""
from __future__ import annotations

import sqlite3

import numpy as np

from . import config, playoffs, winprob


def remaining_games(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """(home_id, away_id) for every regular-season matchup not yet final."""
    return [(r["home_team_id"], r["away_team_id"]) for r in conn.execute(
        """SELECT home_team_id, away_team_id FROM matchups
            WHERE status != 'final' AND week <= ?
            ORDER BY week, matchup_id""", (config.REGULAR_SEASON_WEEKS,))]


def current_records(conn: sqlite3.Connection) -> dict[int, tuple[int, float]]:
    """{team_id: (wins, points_for)} from the standings columns."""
    return {r["team_id"]: (r["wins"], r["points_for"])
            for r in conn.execute("SELECT team_id, wins, points_for FROM teams")}


def team_distributions(conn: sqlite3.Connection) -> dict[int, tuple[float, float]]:
    """{team_id: (mean, sd)} for simulating scores, from season-so-far totals.

    Reuses winprob's shrunk distributions so odds and win probabilities agree and
    a thin early-season sample can't lock a team in. A team with no games yet is
    given the league prior itself, so a fresh season starts everyone symmetric.
    """
    hist = winprob.team_weekly_scores(conn)
    prior = winprob.league_prior(hist)
    dists = {}
    for tid, scores in hist.items():
        d = winprob.team_dist(scores, prior)
        dists[tid] = d if d is not None else prior
    return dists


def playoff_odds(conn: sqlite3.Connection, iterations: int | None = None,
                 seed: int | None = None,
                 cutoff: int | None = None) -> dict[int, float]:
    """Probability each team makes the top-`cutoff` by end of regular season.

    `cutoff` defaults to the actual bracket size. Returns {team_id: prob in
    [0,1]}. Deterministic for a given `seed`.
    """
    iterations = iterations or config.PLAYOFF_SIMS
    cutoff = cutoff or playoffs._bracket_size()
    base = current_records(conn)
    team_ids = list(base)
    n_teams = len(team_ids)
    idx = {tid: i for i, tid in enumerate(team_ids)}

    base_wins = np.array([base[t][0] for t in team_ids], dtype=float)
    base_pf = np.array([base[t][1] for t in team_ids], dtype=float)
    games = remaining_games(conn)

    # No games left -> deterministic current standings.
    if not games:
        made = _made_counts(base_wins.reshape(n_teams, 1),
                            base_pf.reshape(n_teams, 1), cutoff)
        return {t: float(made[idx[t]]) for t in team_ids}

    dists = team_distributions(conn)
    rng = np.random.default_rng(seed)
    wins = np.repeat(base_wins.reshape(n_teams, 1), iterations, axis=1)
    pf = np.repeat(base_pf.reshape(n_teams, 1), iterations, axis=1)

    for home, away in games:
        h, a = idx[home], idx[away]
        hs = rng.normal(dists[home][0], dists[home][1], iterations)
        as_ = rng.normal(dists[away][0], dists[away][1], iterations)
        pf[h] += hs
        pf[a] += as_
        wins[h] += (hs > as_)
        wins[a] += (as_ > hs)

    made = _made_counts(wins, pf, cutoff)
    return {t: float(made[idx[t]] / iterations) for t in team_ids}


def _made_counts(wins: np.ndarray, pf: np.ndarray, cutoff: int) -> np.ndarray:
    """Given per-simulation wins and points_for (teams x sims), count how many
    sims each team finished in the top `cutoff` by (wins, points_for)."""
    n_teams, n_sims = wins.shape
    # Single sortable key: wins dominates, points_for breaks ties. A team's
    # season points_for is far below the 1e6 scale, so ordering is exact.
    key = wins * 1e6 + pf
    order = np.argsort(-key, axis=0)          # teams ranked best-first per sim
    topk = order[:cutoff, :]                   # (cutoff, n_sims) team indices
    counts = np.bincount(topk.reshape(-1), minlength=n_teams)
    return counts
