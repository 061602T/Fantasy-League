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

Small-sample shrinkage
----------------------
Early in a season both mu and sigma come from a handful of games and are far too
trusting: one big week makes a team look like a lock. So each is shrunk toward a
league-wide prior -- the average team mean, and the pooled week-to-week spread
(root-mean of the per-team sample variances, falling back to
``config.WINPROB_DEFAULT_SD``). With ``K = config.PRED_PRIOR_GAMES`` pseudo-games
of prior weight, a team with n games gets weight ``n/(n+K)`` on its own data:
the prior dominates at n=1 and fades to nothing by ~8-10 games, so late-season
forecasts are essentially the old sample-based ones.

The predictive spread also carries the *uncertainty in that shrunk mean* (a
factor ``sqrt(1 + 1/(n+K))``), which is widest when n is small -- this is what
stops a single good week from reading as a near-certain win. The blended
variance is still floored at the league spread, so no team is treated as
steadier than the norm. Displayed matchup probabilities are finally clamped to
``[PRED_PROB_CAP_LO, PRED_PROB_CAP_HI]``: a single head-to-head is never a lock.
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


def league_prior(all_scores: dict[int, list[float]]) -> tuple[float, float]:
    """(prior_mean, prior_sd): the league-wide prior a thin per-team sample is
    shrunk toward. prior_mean is the average of the teams' mean scores (0 with no
    games); prior_sd is the pooled week-to-week SD, or
    ``config.WINPROB_DEFAULT_SD`` when no team has >=2 games yet."""
    means = [fmean(s) for s in all_scores.values() if s]
    prior_mean = fmean(means) if means else 0.0
    pooled = pooled_sd(all_scores)
    return prior_mean, (pooled if pooled else config.WINPROB_DEFAULT_SD)


def team_dist(scores: list[float],
              prior: tuple[float, float]) -> tuple[float, float] | None:
    """(mean, sd) for a team, shrunk toward the league `prior` = (mean, sd).

    Mean and variance are blended with the prior by weight ``n/(n+K)`` on the
    team's own data (``K = config.PRED_PRIOR_GAMES``), so a 1-2 game sample leans
    on the prior and a full-season sample barely does. The SD then carries the
    uncertainty in that shrunk mean via ``sqrt(1 + 1/(n+K))`` -- widest when n is
    small -- and the blended variance is floored at the prior variance so no team
    reads as steadier than the league. Returns None only with no games at all.
    """
    n = len(scores)
    if n == 0:
        return None
    prior_mean, prior_sd = prior
    k = config.PRED_PRIOR_GAMES
    w = n / (n + k)                                  # weight on the team's own data
    mu = w * fmean(scores) + (1.0 - w) * prior_mean
    prior_var = prior_sd * prior_sd
    team_var = variance(scores) if n >= 2 else prior_var
    blended = max(w * team_var + (1.0 - w) * prior_var, prior_var)
    sd = math.sqrt(blended * (1.0 + 1.0 / (n + k)))  # + mean-estimate uncertainty
    if sd <= 0:                                      # pathological guard
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
    prior = league_prior(hist)
    lo, hi = config.PRED_PROB_CAP_LO, config.PRED_PROB_CAP_HI
    out = []
    for m in conn.execute(
            """SELECT home_team_id, away_team_id FROM matchups
                WHERE week = ? ORDER BY matchup_id""", (week,)).fetchall():
        h, a = m["home_team_id"], m["away_team_id"]
        dh = team_dist(hist.get(h, []), prior)
        da = team_dist(hist.get(a, []), prior)
        # Clamp the displayed probability: a single head-to-head is never a lock.
        hwp = min(hi, max(lo, win_probability(dh, da)))
        out.append({
            "home_team_id": h, "away_team_id": a,
            "home": names.get(h, "?"), "away": names.get(a, "?"),
            "home_wp": round(hwp, 4), "away_wp": round(1.0 - hwp, 4),
            "n_home": len(hist.get(h, [])), "n_away": len(hist.get(a, [])),
        })
    return out
