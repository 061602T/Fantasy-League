"""Fantasy scoring.

Offense (QB/RB/WR/TE): use nflverse's official `fantasy_points_ppr` directly.
Kicker & DST: nflverse does NOT fantasy-score these, so we compute them here
from official counting stats using the tables in config.

Every function takes a single row (a pandas Series / namedtuple-like mapping)
and returns a float point total, so they compose cleanly over weekly frames.
"""
from __future__ import annotations

import pandas as pd

from . import config


def _g(row, key, default=0.0):
    """Safe getter: missing/NaN -> default."""
    val = row.get(key, default) if hasattr(row, "get") else getattr(row, key, default)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return val


def offense_points(row) -> float:
    """Full-PPR offensive points, straight from nflverse's official column."""
    return float(_g(row, "fantasy_points_ppr", 0.0))


def kicker_points(row) -> float:
    """Distance-based kicker scoring from official FG/PAT counting stats."""
    s = config.KICKER_SCORING
    pts = 0.0
    for bucket in ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39",
                   "fg_made_40_49", "fg_made_50_59", "fg_made_60_"):
        pts += _g(row, bucket) * s[bucket]
    pts += _g(row, "pat_made") * s["pat_made"]
    pts += _g(row, "fg_missed") * s["fg_missed"]
    pts += _g(row, "pat_missed") * s["pat_missed"]
    return float(pts)


def points_allowed_to_tier(points_allowed: float) -> float:
    """Map points allowed to DST fantasy points via config tiers."""
    for max_pa, fpts in config.DST_POINTS_ALLOWED_TIERS:
        if points_allowed <= max_pa:
            return float(fpts)
    return float(config.DST_POINTS_ALLOWED_TIERS[-1][1])


def dst_points(row, points_allowed: float) -> float:
    """Team-defense scoring from official team counting stats + points allowed.

    Note: blocked kicks are omitted (rare, ~0.5/team/season, and not cleanly
    separated from kicks-suffered in the team table). Everything else standard
    is included.
    """
    s = config.DST_SCORING
    pts = 0.0
    pts += _g(row, "def_sacks") * s["sack"]
    pts += _g(row, "def_interceptions") * s["interception"]
    pts += _g(row, "fumble_recovery_opp") * s["fumble_recovery"]   # takeaways only
    pts += _g(row, "def_tds") * s["touchdown"]                     # defensive TDs
    pts += _g(row, "special_teams_tds") * s["touchdown"]           # return/ST TDs
    pts += _g(row, "def_safeties") * s["safety"]
    pts += points_allowed_to_tier(points_allowed)
    return float(pts)


def points_allowed_map(schedules: pd.DataFrame) -> dict:
    """Build {(season, week, team): points_allowed} from the schedule.

    A team's points allowed in a game = the opponent's final score.
    """
    out = {}
    for r in schedules.itertuples(index=False):
        if pd.isna(getattr(r, "home_score", None)) or pd.isna(getattr(r, "away_score", None)):
            continue  # unplayed game
        out[(r.season, r.week, r.home_team)] = float(r.away_score)
        out[(r.season, r.week, r.away_team)] = float(r.home_score)
    return out
