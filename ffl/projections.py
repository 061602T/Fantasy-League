"""Projections & draft pool.

Naive rest-of-season (ROS) projection: a recency-weighted average of each
entity's most recent PROJECTION_WINDOW games (weights PROJECTION_WEIGHTS,
most-recent-first). "Entity" = an offensive player, a kicker, or a team DST.

Because the league launches early in a season (2026 wk1), the current season
rarely has WINDOW games yet, so game logs are backfilled with the prior
season(s): we simply take each entity's most recent games in chronological
order spanning BACKFILL_SEASONS -> SEASON. If an entity has fewer than WINDOW
games total, the weights are renormalized over what exists.
"""
from __future__ import annotations

import pandas as pd

from . import config, data, scoring


def _time_index(season: int, week: int) -> int:
    """Chronological sort key; larger = more recent."""
    return season * 100 + week


def build_game_logs(as_of_season: int = None, as_of_week: int = None) -> pd.DataFrame:
    """Long per-game fantasy scores for every entity, up to the as-of point.

    Columns: entity_id, entity_type, position, name, team, season, week,
    time_index, fpts.
    """
    as_of_season = as_of_season if as_of_season is not None else config.SEASON
    as_of_week = as_of_week if as_of_week is not None else config.AS_OF_WEEK
    seasons = sorted(set(config.BACKFILL_SEASONS + [as_of_season]))

    ps = data.player_stats(seasons)
    ts = data.team_stats(seasons)
    sch = data.schedules(seasons)
    pa = scoring.points_allowed_map(sch)

    def in_window(season, week):
        return season < as_of_season or (season == as_of_season and week <= as_of_week)

    rows = []

    # --- Offense (QB/RB/WR/TE) and Kickers, from player_stats ---
    off_mask = ps["position"].isin(["QB", "RB", "WR", "TE"])
    k_mask = ps["position"] == "K"
    for r in ps[off_mask | k_mask].itertuples(index=False):
        if not in_window(r.season, r.week):
            continue
        is_k = r.position == "K"
        fpts = scoring.kicker_points(r) if is_k else scoring.offense_points(r)
        rows.append({
            "entity_id": r.player_id,
            "entity_type": "player",
            "position": r.position,
            "name": getattr(r, "player_display_name", r.player_id),
            "team": getattr(r, "team", None),
            "season": r.season, "week": r.week,
            "time_index": _time_index(r.season, r.week),
            "fpts": round(fpts, 2),
        })

    # --- Team DST, from team_stats + points allowed ---
    for r in ts.itertuples(index=False):
        if not in_window(r.season, r.week):
            continue
        points_allowed = pa.get((r.season, r.week, r.team))
        if points_allowed is None:
            continue  # bye week / unplayed
        fpts = scoring.dst_points(r, points_allowed)
        rows.append({
            "entity_id": r.team,
            "entity_type": "dst",
            "position": "DST",
            "name": f"{r.team} DST",
            "team": r.team,
            "season": r.season, "week": r.week,
            "time_index": _time_index(r.season, r.week),
            "fpts": round(fpts, 2),
        })

    return pd.DataFrame(rows)


def ros_projection(scores_recent_first: list[float]) -> float:
    """Recency-weighted average of up to WINDOW most-recent scores.

    Weights renormalized when fewer than WINDOW games exist.
    """
    scores = scores_recent_first[: config.PROJECTION_WINDOW]
    if not scores:
        return 0.0
    weights = list(config.PROJECTION_WEIGHTS[: len(scores)])
    total_w = sum(weights)
    weights = [w / total_w for w in weights]
    return round(sum(s * w for s, w in zip(scores, weights)), 2)


def build_projections(game_logs: pd.DataFrame = None) -> pd.DataFrame:
    """Per-entity ROS projection with metadata.

    Columns: entity_id, entity_type, position, name, team, n_games, proj_ppg.
    """
    if game_logs is None:
        game_logs = build_game_logs()
    out = []
    for eid, grp in game_logs.groupby("entity_id"):
        grp = grp.sort_values("time_index", ascending=False)
        scores = grp["fpts"].tolist()
        latest = grp.iloc[0]
        out.append({
            "entity_id": eid,
            "entity_type": latest["entity_type"],
            "position": latest["position"],
            "name": latest["name"],
            "team": latest["team"],
            "n_games": len(scores),
            "proj_ppg": ros_projection(scores),
        })
    return pd.DataFrame(out).sort_values("proj_ppg", ascending=False).reset_index(drop=True)


def build_player_pool(eligible_status=("ACT",)) -> pd.DataFrame:
    """Build the draft pool: top-N per position among currently-rostered
    players, plus all DSTs, using POOL_TARGETS.

    Returns columns: entity_id, position, name, team, proj_ppg, n_games.
    """
    proj = build_projections()

    # Eligibility: players on a current-season NFL roster with an allowed status.
    rosters = data.players_current_roster(config.SEASON, eligible_status)
    elig_ids = set(rosters["entity_id"])

    parts = []
    # Offense + kickers: restrict to eligible, rank per position.
    for pos, target in config.POOL_TARGETS.items():
        if pos == "DST":
            continue
        sub = proj[(proj["position"] == pos) & (proj["entity_id"].isin(elig_ids))]
        sub = sub.sort_values("proj_ppg", ascending=False).head(target)
        parts.append(sub)
    # DST: all teams, take target.
    dst = proj[proj["position"] == "DST"].sort_values("proj_ppg", ascending=False)
    dst = dst.head(config.POOL_TARGETS["DST"])
    parts.append(dst)

    pool = pd.concat(parts, ignore_index=True)
    return pool[["entity_id", "position", "name", "team", "proj_ppg", "n_games"]]
