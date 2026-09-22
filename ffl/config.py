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

# Head-to-head regular season. 8 teams -> a double round-robin is 14 weeks,
# which maps cleanly onto NFL weeks 1-14 (fantasy playoffs would be 15-17).
REGULAR_SEASON_WEEKS = 14

# Tick-loop cadence (seconds) for the continuous scheduler. Hourly by default;
# most ticks are cheap no-ops -- LLM spend only happens when a new NFL week
# completes. Override with FFL_TICK_INTERVAL.
TICK_INTERVAL_SECONDS = int(os.environ.get("FFL_TICK_INTERVAL", "3600"))

# --- Playoffs --------------------------------------------------------------
# Top PLAYOFF_TEAMS seeds (by regular-season standings) enter a single-
# elimination bracket. 4 -> semifinals then final, in NFL weeks
# REGULAR_SEASON_WEEKS+1 and +2. Playoff games don't count in regular records.
PLAYOFF_TEAMS = 4

# --- Mid-week activity between scored weeks --------------------------------
# On an idle tick (no new NFL week), the league can still show some life. These
# are per-idle-tick probabilities; kept low so hourly ticks stay cheap. The
# Haiku gate still decides whether a chosen GM actually engages.
MIDWEEK_TRADE_PROB = float(os.environ.get("FFL_MIDWEEK_TRADE_PROB", "0.08"))
MIDWEEK_CHAT_PROB = float(os.environ.get("FFL_MIDWEEK_CHAT_PROB", "0.12"))

# --- Ambient chat loop (scripts/run_chat_tick.py) --------------------------
# A lightweight chat-only loop, decoupled from the hourly scoring tick, meant to
# run every ~15 min so the group chat feels ongoing rather than tied to scoring.
# A firing does nothing unless it clears BOTH a cooldown since the last banter
# and a cheap probability pre-gate -- so most of the ~96 firings/day are free
# (no API), with natural quiet stretches, and it can't pile onto the hourly
# tick's own chat. CHAT_TICK_PROB is per-firing; CHAT_TICK_COOLDOWN is in
# seconds; CHAT_TICK_THREAD_PROB is how often a starter replies to recent chat
# (threaded) vs opening a fresh topic.
CHAT_TICK_PROB = float(os.environ.get("FFL_CHAT_TICK_PROB", "0.4"))
CHAT_TICK_COOLDOWN = int(os.environ.get("FFL_CHAT_TICK_COOLDOWN", "360"))
CHAT_TICK_THREAD_PROB = float(os.environ.get("FFL_CHAT_TICK_THREAD_PROB", "0.65"))
# The 15-min loop also carries the GMs' between-game DECISIONS (trades and
# governance), not just chat. Governance runs every firing (self-gated, cheap).
# A trade negotiation is heavier (several LLM calls), so it's rarer: this is the
# per-firing chance the loop attempts one. Kept low because the loop fires often
# (every 15 min); tune for how busy you want the trade market to feel.
CHAT_TICK_TRADE_PROB = float(os.environ.get("FFL_CHAT_TICK_TRADE_PROB", "0.05"))

# --- Real-world context for chat (scripts/refresh_context.py) --------------
# A separate, infrequent job (cron'd ~every 4h) web-searches current NFL news
# and general pop-culture/news via the Anthropic server-side web_search tool and
# caches a handful of short bullets to CONTEXT_PATH. The chat paths read them as
# OPTIONAL flavor a GM can occasionally reference -- never required, never
# blocking. Missing / stale / unreadable cache -> chat just runs league-only.
CONTEXT_PATH = os.environ.get(
    "FFL_CONTEXT_PATH",
    os.path.expanduser(os.path.join("~", "ffl-data", "world_context.json")))
# Ignore a cache older than this so the GMs don't reference stale "current
# events" if the refresh job has been failing.
CONTEXT_MAX_AGE_HOURS = float(os.environ.get("FFL_CONTEXT_MAX_AGE_HOURS", "24"))
# Per-message chance the cached context is even shown to the model. Kept well
# below 1 so most messages are league-only regardless of what the model does
# with it; of the messages that DO see it, the prompt still says to reference it
# only rarely. Net effect: an occasional real-world aside, not a news crawl.
CONTEXT_INJECT_PROB = float(os.environ.get("FFL_CONTEXT_INJECT_PROB", "0.35"))

# --- Governance: GM-proposed bylaws (free-form) ----------------------------
# GMs can propose free-text bylaws/punishments, discuss, and vote in character.
# A passed vote NEVER auto-executes: it lands in 'passed_pending' for the
# commissioner (you) to enact via scripts/review_bylaws.py -- either as displayed
# lore (text only) or as ONE bounded mechanical effect (ffl/effects.py). None of
# this is wired into the live tick loop yet; that is a separate, reviewed step.
GOV_VOTING_WINDOW_HOURS = float(os.environ.get("FFL_GOV_WINDOW_HOURS", "6"))
GOV_QUORUM = int(os.environ.get("FFL_GOV_QUORUM", "4"))   # min yes/no votes to be valid
GOV_PROPOSE_PROB = float(os.environ.get("FFL_GOV_PROPOSE_PROB", "0.05"))  # per-tick, pre-gate
GOV_TITLE_MAX = 120        # chars, sanitized
GOV_TEXT_MAX = 600         # chars, sanitized (pitch / rationale)
# Bounded effects toolbox (the commissioner's mechanical enactment options):
GOV_FAAB_MAX_DELTA = 50    # |delta| per faab_adjust; result clamped to [0, max(cap, current)]
GOV_FREEZE_MAX_WEEKS = 3   # trade_freeze duration cap
GOV_BACKSEAT_MAX_WEEKS = 3 # waiver_backseat duration cap
GOV_KICKER_FLEX_MAX_WEEKS = 3  # kicker_flex_lock duration cap
GOV_LOSER_LABEL_MAX = 80   # loser_flag label chars, sanitized

# --- Draft-day chatter -----------------------------------------------------
# Per-pick probability that a notable pick draws live reactions from a few
# rival GMs (a reach, a steal, or grabbing a position a rival also needs).
# Kept low on purpose: at 120 picks this is ~10-15 reaction moments, not one
# per pick, so the draft stays lively without 120x the noise/cost. The Haiku
# gate still decides whether each chosen rival actually chimes in.
DRAFT_CHAT_PROB = float(os.environ.get("FFL_DRAFT_CHAT_PROB", "0.12"))

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
# Used for the draft pool / who-starts ranking (ffl/projections.py).
PROJECTION_WEIGHTS = (0.5, 0.3, 0.2)
PROJECTION_WINDOW = 3

# Weekly score projection (ffl/scoreproj.py): a player's projected points for a
# league week are the simple mean of their most recent SCORE_PROJ_WINDOW actual
# weekly scores before that week. A statistical estimate from recent scoring --
# not a prediction of the real NFL game and not the league's actual points.
SCORE_PROJ_WINDOW = 4

# Matchup win probability (ffl/winprob.py): a normal-approximation on the score
# margin using each team's season-so-far mean and variance of weekly totals.
# WINPROB_DEFAULT_SD is the fallback week-to-week standard deviation used only
# when there isn't enough scored history to estimate spread (an NFL-fantasy
# team week is empirically ~28 pts sd). A statistical estimate, not a lock.
WINPROB_DEFAULT_SD = 28.0

# --- Prediction regularization (winprob + playoffodds) ---------------------
# Small samples early in a season make the forecasts overconfident: a team's
# mean and variance come from one or two games and get trusted as its true
# talent. Both are shrunk toward a league-wide prior with this many pseudo-games
# of weight -- weight on the team's own data is n/(n+K), so the prior dominates
# at n=1 and barely matters by ~8-10 games. Displayed matchup win probabilities
# are also clamped to [CAP_LO, CAP_HI] so a single head-to-head never shows a
# flat 100%/0%.
PRED_PRIOR_GAMES = float(os.environ.get("FFL_PRED_PRIOR_GAMES", "4"))
PRED_PROB_CAP_LO = float(os.environ.get("FFL_PRED_PROB_CAP_LO", "0.02"))
PRED_PROB_CAP_HI = float(os.environ.get("FFL_PRED_PROB_CAP_HI", "0.98"))

# Playoff odds (ffl/playoffodds.py): Monte-Carlo simulation count. Each rest-of-
# season simulation draws every remaining game from the teams' scoring
# distributions and re-seeds the standings; the odds are the share of sims a
# team lands in the top-`PLAYOFF_TEAMS`. Vectorized, so 10k runs in ~ms.
PLAYOFF_SIMS = 10000

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
