"""Central configuration for the AI Fantasy Football League.

Anything a human might reasonably want to tune lives here: which season the
league is playing, the scoring rules, projection parameters, and the draft
pool's positional targets.

Data note: offensive fantasy points come straight from nflverse's official
`fantasy_points_ppr` (full-PPR). Kicker and DST points are NOT scored by
nflverse, so we compute them here from official counting stats using the
KICKER_SCORING and DST_SCORING tables below.
"""

import os

# --- League timeline -------------------------------------------------------
# The current, in-progress NFL season the league is playing.
SEASON = 2026
# The most recent COMPLETED week of SEASON. Games through this week are known
# results; the rest-of-season draft happens "as of" here. At 2026-09-15 only
# week 1 of 2026 has been played (last game 2026-09-14).
AS_OF_WEEK = 1
# Seasons used to backfill game logs when the current season has < WINDOW
# games played (see projections.py). Most-recent-first.
BACKFILL_SEASONS = [2025]

# --- Roster / league shape (from the brief) --------------------------------
NUM_TEAMS = 8
STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}
FLEX_POSITIONS = ("RB", "WR", "TE")
BENCH_SPOTS = 6
ROSTER_SIZE = 15
DRAFT_ROUNDS = 15  # snake

# --- Full-PPR offensive scoring (reference; nflverse already applies this) --
# Kept here for documentation and for any custom recompute/audit.
PPR_SCORING = {
    "pass_yd": 1 / 25,
    "pass_td": 4,
    "interception": -2,
    "rush_yd": 1 / 10,
    "rush_td": 6,
    "rec": 1,          # full PPR
    "rec_yd": 1 / 10,
    "rec_td": 6,
    "fumble_lost": -2,
    "two_pt": 2,
}

# --- Kicker scoring --------------------------------------------------------
# DEFAULT = standard ESPN-style distance-based kicker scoring. These values
# are a proposed default, NOT specified in the brief -- confirm/adjust.
KICKER_SCORING = {
    "fg_made_0_19": 3,
    "fg_made_20_29": 3,
    "fg_made_30_39": 3,
    "fg_made_40_49": 4,
    "fg_made_50_59": 5,
    "fg_made_60_": 5,
    "pat_made": 1,
    "fg_missed": -1,     # any missed FG
    "pat_missed": -1,
}

# --- DST scoring -----------------------------------------------------------
# DEFAULT = standard ESPN-style team-defense scoring. Proposed default, NOT
# specified in the brief -- confirm/adjust.
DST_SCORING = {
    "sack": 1,
    "interception": 2,
    "fumble_recovery": 2,
    "touchdown": 6,       # defensive or special-teams TD
    "safety": 2,
    "blocked_kick": 2,
}
# Points-allowed tiers: (max_points_allowed_inclusive, fantasy_points).
DST_POINTS_ALLOWED_TIERS = [
    (0, 10),
    (6, 7),
    (13, 4),
    (20, 1),
    (27, 0),
    (34, -1),
    (99, -4),
]

# --- Projections -----------------------------------------------------------
# Recency-weighted rolling average of the last WINDOW games, most-recent-first.
PROJECTION_WEIGHTS = (0.5, 0.3, 0.2)
PROJECTION_WINDOW = 3

# --- Draft pool positional targets (from the brief, ~280 players) ----------
POOL_TARGETS = {"QB": 16, "RB": 70, "WR": 90, "TE": 24, "K": 32, "DST": 32}

# --- Paths -----------------------------------------------------------------
CACHE_DIR = ".cache"       # downloaded nflverse parquet, gitignored
# SQLite runtime state. Deliberately OUTSIDE the git checkout so that pulling
# code updates (or re-cloning) can never touch or orphan the live database.
# Override with FFL_DB_PATH; defaults under the user's home for dev and the Pi.
DB_PATH = os.environ.get(
    "FFL_DB_PATH", os.path.expanduser(os.path.join("~", "ffl-data", "league.db")))

# --- Waivers ---------------------------------------------------------------
FAAB_BUDGET = 100          # season-long FAAB budget per team

# --- Trades ----------------------------------------------------------------
MAX_TRADE_ROUNDS = 3       # propose -> counter -> ... capped at 3 rounds
MAX_COLLISION_ROUNDS = 3   # persona name-collision negotiation cap
