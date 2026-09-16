"""Matchup win probability (deterministic, no API calls).

Feature 2 of the analytics set. Estimates each side's chance of winning an
upcoming head-to-head from the teams' *season-so-far scoring*, via a standard
normal-approximation on the margin -- a statistical estimate, not a prediction
of the real NFL games.

Model
-----
Treat a team's weekly total as roughly Normal(mu, sigma^2), with mu and sigma
the mean and sample standard deviation of its actual weekly totals so far. Two
independent teams' margin is then Normal(mu_a - mu_b, sigma_a^2 + sigma_b^2), so

    P(A beats B) = Phi( (mu_a - mu_b) / sqrt(sigma_a^2 + sigma_b^2) )

where Phi is the standard normal CDF. A tie has probability ~0 under a
continuous model, so the two sides' probabilities sum to 1.

Spread when history is thin
---------------------------
A team with fewer than two games has no usable variance yet, so its sigma falls
back to a **pooled** league spread -- the root-mean of the per-team variances of
teams that do have >=2 games -- and, failing even that, to
``config.WINPROB_DEFAULT_SD``. With no games at all a matchup is 50/50.

Even *with* two-plus games, a small early-season sample can land freakishly
tight and understate a team's true week-to-week variance, which makes the model
overconfident. So a team's sigma is **floored at the pooled league spread**: no
team is treated as steadier than the league norm. On the 2025 backtest this
floor is what makes the forecasts beat a coin flip on log loss (0.68 vs 0.69)
rather than lose to it, without changing which side is favoured.
"""
from __future__ import annotations

import math
import sqlite3
from statistics import fmean, variance

from . import config


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via erf (stdlib, no numpy/scipy)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def team_weekly_scores(conn: sqlite3.Connection,
                       before_week: int | None = None) -> dict[int, list[float]]:
    """{team_id: [weekly totals]} from final regular-season matchups.

    Only regular-season weeks count (playoffs are single-elimination and not
    representative of scoring form). When `before_week` is given, only weeks
    strictly before it are included -- i.e. the information available going into
    that week.
    """
    cap = config.REGULAR_SEASON_WEEKS
    scores: dict[int, list[float]] = {
        r["team_id"]: [] for r in conn.execute("SELECT team_id FROM teams")}
    q = ("""SELECT week, home_team_id, away_team_id, home_points, away_points
              FROM matchups WHERE status='final' AND week <= ?""")
    params = [cap]
    if before_week is not None:
        q += " AND week < ?"
        params.append(before_week)
    for m in conn.execute(q + " ORDER BY week", params):
        if m["home_points"] is not None:
            scores.setdefault(m["home_team_id"], []).append(m["home_points"])
        if m["away_points"] is not None:
            scores.setdefault(m["away_team_id"], []).append(m["away_points"])
    return scores


def pooled_sd(all_scores: dict[int, list[float]]) -> float | None:
    """Typical week-to-week SD across the league: root-mean of the per-team
    sample variances of teams with >=2 games. None if no team qualifies."""
    vars = [variance(s) for s in all_scores.values() if len(s) >= 2]
    if not vars:
        return None
    return math.sqrt(fmean(vars))


def team_dist(scores: list[float], pooled: float | None) -> tuple[float, float] | None:
    """(_mean, sd) for a team.

    With >=2 games the sample SD is used but *floored at the pooled league SD*
    (a tight small sample understates the true week-to-week spread). With fewer
    than two games -- or a degenerate all-equal history -- the SD falls back to
    the pooled SD, then to ``config.WINPROB_DEFAULT_SD``. None if no games.
    """
    if not scores:
        return None
    mu = fmean(scores)
    floor = pooled if pooled else config.WINPROB_DEFAULT_SD
    if len(scores) >= 2:
        sd = max(math.sqrt(variance(scores)), floor)
    else:
        sd = floor
    if sd <= 0:                          # degenerate (all-equal, no pooled)
        sd = config.WINPROB_DEFAULT_SD
    return mu, sd


def win_probability(dist_a, dist_b) -> float:
    """P(team A beats team B) from their (mean, sd) tuples.

    If either side has no history, returns 0.5. A zero combined spread decides
    by mean (1/0), or 0.5 on equal means.
    """
    if dist_a is None or dist_b is None:
        return 0.5
    mu_a, sd_a = dist_a
    mu_b, sd_b = dist_b
    denom = math.hypot(sd_a, sd_b)
    if denom == 0:
        return 0.5 if mu_a == mu_b else (1.0 if mu_a > mu_b else 0.0)
    return _normal_cdf((mu_a - mu_b) / denom)


def matchup_winprobs(conn: sqlite3.Connection, week: int) -> list[dict]:
    """Win probabilities for every matchup in `week`, using only the scoring
    history from weeks *before* `week`.

    Each row: home/away team ids + names, home_wp, away_wp (= 1 - home_wp),
    and n_home/n_away (games of history each side had going in).
    """
    names = {r["team_id"]: r["team_name"]
             for r in conn.execute("SELECT team_id, team_name FROM teams")}
    hist = team_weekly_scores(conn, before_week=week)
    pooled = pooled_sd(hist)
    out = []
    for m in conn.execute(
            """SELECT home_team_id, away_team_id FROM matchups
                WHERE week = ? ORDER BY matchup_id""", (week,)).fetchall():
        h, a = m["home_team_id"], m["away_team_id"]
        dh = team_dist(hist.get(h, []), pooled)
        da = team_dist(hist.get(a, []), pooled)
        hwp = win_probability(dh, da)
        out.append({
            "home_team_id": h, "away_team_id": a,
            "home": names.get(h, "?"), "away": names.get(a, "?"),
            "home_wp": round(hwp, 4), "away_wp": round(1.0 - hwp, 4),
            "n_home": len(hist.get(h, [])), "n_away": len(hist.get(a, [])),
        })
    return out
