"""Data access layer: pull real NFL data from nflverse (via nflreadpy) and
cache it locally so repeated runs don't re-download.

All functions return pandas DataFrames. Downloaded data is cached as parquet
under config.CACHE_DIR (gitignored) keyed by season, because a completed
season never changes. The current, in-progress season is cached too but can
be force-refreshed with refresh=True once new games are played.
"""
from __future__ import annotations

import os
import warnings

import pandas as pd

from . import config

warnings.filterwarnings("ignore")


def _cache_path(name: str, season: int) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, f"{name}_{season}.parquet")


def _load_seasonal(loader, name: str, seasons: list[int], refresh: bool = False) -> pd.DataFrame:
    """Load a per-season nflreadpy table with local parquet caching."""
    import nflreadpy as nfl

    frames = []
    for yr in seasons:
        path = _cache_path(name, yr)
        if os.path.exists(path) and not refresh:
            frames.append(pd.read_parquet(path))
            continue
        fn = getattr(nfl, loader)
        df = fn(seasons=[yr])
        df = df.to_pandas() if hasattr(df, "to_pandas") else df
        df.to_parquet(path, index=False)
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def player_stats(seasons: list[int], refresh: bool = False) -> pd.DataFrame:
    """Weekly per-player stats incl. official fantasy_points_ppr (offense) and
    raw kicking columns (for our own kicker scoring)."""
    return _load_seasonal("load_player_stats", "player_stats", seasons, refresh)


def team_stats(seasons: list[int], refresh: bool = False) -> pd.DataFrame:
    """Weekly per-team stats incl. defensive counting stats, for DST scoring."""
    return _load_seasonal("load_team_stats", "team_stats", seasons, refresh)


def schedules(seasons: list[int], refresh: bool = False) -> pd.DataFrame:
    """Game schedule + final scores, for DST points-allowed and matchups."""
    import nflreadpy as nfl

    frames = []
    for yr in seasons:
        path = _cache_path("schedules", yr)
        if os.path.exists(path) and not refresh:
            frames.append(pd.read_parquet(path))
            continue
        df = nfl.load_schedules(seasons=[yr])
        df = df.to_pandas() if hasattr(df, "to_pandas") else df
        df.to_parquet(path, index=False)
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def rosters(seasons: list[int], refresh: bool = False) -> pd.DataFrame:
    """Season rosters (who is on which NFL team, with status)."""
    return _load_seasonal("load_rosters", "rosters", seasons, refresh)


def players_current_roster(season: int, eligible_status=("ACT",),
                           refresh: bool = False) -> pd.DataFrame:
    """Currently-rostered players eligible for the draft pool.

    Returns columns: entity_id (gsis_id, matches player_stats.player_id),
    name, position, team, status.
    """
    r = rosters([season], refresh)
    r = r[r["status"].isin(eligible_status)].copy()
    r = r.rename(columns={"gsis_id": "entity_id", "full_name": "name"})
    return r[["entity_id", "name", "position", "team", "status"]].dropna(subset=["entity_id"])


def players(refresh: bool = False) -> pd.DataFrame:
    """Player metadata (name, position, team, id). Not season-scoped."""
    import nflreadpy as nfl

    path = _cache_path("players", 0)
    if os.path.exists(path) and not refresh:
        return pd.read_parquet(path)
    df = nfl.load_players()
    df = df.to_pandas() if hasattr(df, "to_pandas") else df
    df.to_parquet(path, index=False)
    return df
