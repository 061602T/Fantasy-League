# AI Fantasy Football League

An autonomous 8-team fantasy football league where AI agents draft, trade,
chat, and manage teams against real NFL data. Build tool: Claude Code.
Production runtime: a Raspberry Pi running the tick-loop scheduler.

See `PROJECT_BRIEF` for the full design. This README tracks build status and
key implementation decisions.

## Data source (important deviation from the brief)

The brief specified `nfl_data_py`. **That package is abandoned** (final
release 0.3.3) and its `import_weekly_data()` reads a pre-aggregated file
nflverse stopped publishing after the 2024 season, so it returns HTTP 404 for
2025 and 2026. Verified: the underlying nflverse data for 2025 (complete) and
2026 (in progress) *does* exist and is reachable from this environment.

We use **`nflreadpy`** instead — nflverse's maintained successor package. It
returns official pre-computed weekly stats (including full-PPR
`fantasy_points_ppr`) for 2024–2026.

- **Offense (QB/RB/WR/TE):** official `fantasy_points_ppr`, used directly.
- **Kickers:** nflverse doesn't fantasy-score kickers, so we compute points
  from official FG-by-distance / PAT counts (`ffl/scoring.py`).
- **DST:** computed from official team defensive stats + points allowed
  (from the schedule).

All downloads are cached as parquet under `.cache/` (gitignored).

## Build status

- [x] **1. Data & projections layer** — real stats, ROS projections, 264-player
      draft pool. Verified: pbp→PPR aggregation matches nflverse's official
      numbers on 98.5% of 2024 player-weeks (we ultimately use the official
      numbers directly); kicker/DST scoring spot-checked on real games;
      projection formula verified against manual calculation.
- [x] **2. Database schema** — SQLite, WAL mode, foreign keys enforced. 10
      tables (league, teams w/ persona fields, players, rosters, lineups,
      draft_picks, transactions, matchups, player_weekly_scores, chat_log).
      Verified: pragmas active, FK violations rejected, and the real 264-player
      pool + 3,908 real weekly scores round-trip through it.
- [ ] 3. Agent personas (generation + collision negotiation)
- [ ] 4. Draft engine (snake, 15 rounds)
- [ ] 5. Weekly scoring cycle
- [ ] 6. Trade & waiver systems
- [ ] 7. Event-aware group chat
- [ ] 8. Tick-loop scheduler + check-in dashboard/digest

## Layout

    ffl/
      config.py       league timeline, scoring rules, projection params, pool targets
      data.py         nflverse data access + local parquet cache
      scoring.py      offense (official PPR) + kicker + DST scoring
      projections.py  game logs, ROS projection, draft pool
    scripts/
      show_pool.py    print the current draft pool

## Setup

    pip install -r requirements.txt
    python -m scripts.show_pool

## Open decisions (flagged, not yet finalized)

- **Kicker/DST scoring values:** using standard ESPN-style defaults
  (`config.KICKER_SCORING`, `config.DST_SCORING`) — pending confirmation.
- **Tick interval** for the continuous loop (needed at step 8).
- **Dashboard/digest format** for checking in (needed at step 8).
