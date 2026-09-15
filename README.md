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
- [x] **3. Agent personas** — each of the 8 GMs self-generates a persona via a
      real Sonnet 5 call; duplicate team/GM names are negotiated in-character
      (≤3 rounds, then a coin flip; loser rebrands), with the full transcript
      written to `chat_log`. Teams persist to the `teams` table.
      Verified **live against the real API**: 8 distinct personas generated and
      persisted (unique draft slots, valid `persona_json`), and a forced
      collision drove a real 3-round negotiation → coin flip → in-character
      rebrand. Collision/persistence logic also has offline tests
      (`scripts/test_personas.py`, no key needed).
- [x] **4. Draft engine** — 15-round, 8-team snake draft (120 picks). Each pick
      is a real Sonnet decision by that team's GM: it sees its persona, current
      roster, remaining lineup needs, and a menu of the best available players,
      and picks one in character (quip written to `chat_log`). Two guards keep
      every roster legal — position caps and a must-fill rule that restricts the
      endgame to mandatory starter positions. Verified **live**: 120 unique
      picks, contiguous overall order, all 8 teams finished with legal, full
      15-man rosters. Offline tests in `scripts/test_draft.py`.
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
      llm.py          Anthropic SDK wrapper (Haiku gate / Sonnet decision tiers)
      personas.py     persona generation, name-collision negotiation, persistence
      draft.py        snake-draft order, roster/needs logic, live pick decisions
      backup.py       online SQLite backup helper (cron + post-draft one-off)
    scripts/
      show_pool.py      print the current draft pool
      gen_personas.py   generate + persist the 8 GM personas (real API calls)
      test_personas.py  offline tests for the collision/persistence logic
      run_draft.py      run the live snake draft (real API calls)
      test_draft.py     offline tests for the draft engine
      backup_db.py      cron / on-demand DB backup CLI
      test_backup.py    offline tests for the backup helper

## Setup

    pip install -r requirements.txt
    python -m scripts.show_pool

Steps 3+ make real Claude API calls and need a key. Export it, or drop it in a
gitignored `.env` (the SDK wrapper reads either):

    export ANTHROPIC_API_KEY=sk-ant-...      # or: echo 'ANTHROPIC_API_KEY=...' > .env
    python -m scripts.gen_personas           # generate + persist the 8 GM personas
    python -m scripts.test_personas          # offline logic tests (no API key needed)

## Persistence & deployment (Raspberry Pi)

The runtime database is **not** in the git checkout, so pulling code updates (or
re-cloning) can never touch or orphan the live league.

- **Location:** `config.DB_PATH`, from `FFL_DB_PATH`, defaults to
  `~/ffl-data/league.db` (dev and Pi alike). `db.connect()` creates the parent
  directory and opens WAL + `synchronous=FULL` + foreign keys. On the Pi, set
  `FFL_DB_PATH=/home/<user>/ffl-data/league.db` in the service env.
- **Backups:** `python -m scripts.backup_db` writes a dated, self-contained,
  integrity-checked snapshot via SQLite's online backup API (safe during writes;
  no `sqlite3` CLI needed). Tunable by env: `FFL_BACKUP_DIR`
  (default `~/ffl-data/backups`), `FFL_BACKUP_RETAIN_DAYS` (default 14).
  Suggested Pi cron (retune once tick cadence is set):

      0 */6 * * * cd /home/<user>/Fantasy-League && \
        FFL_DB_PATH=/home/<user>/ffl-data/league.db /usr/bin/python3 \
        -m scripts.backup_db >> /home/<user>/ffl-data/backup.log 2>&1

  Point `FFL_BACKUP_DIR` at storage **off the SD card** (USB/network): WAL keeps
  the DB uncorrupted at the SQLite layer through power loss, but physical SD-card
  corruption is below SQLite and can take the file *and* same-card backups.
- **One-off backups:** `run_draft.py` calls the backup helper immediately after
  the draft, since 120 non-deterministic picks can't be regenerated identically.
  Future one-shot events (e.g. season init) should do the same.
- **Not an export/import feature:** backups only restore the same DB
  byte-for-byte — there is no path to import test data into a real league.
- **Startup integrity gate (planned, step 8):** the service should
  `PRAGMA quick_check` on boot and refuse to run on a corrupt file, restoring the
  newest verified backup instead. Helper not wired yet.

## Decisions locked in

- **Data source:** `nflreadpy` (see above), not `nfl_data_py`.
- **Kicker/DST scoring:** standard ESPN-style — distance-based FG (0-39=3,
  40-49=4, 50+=5), and DST with points-allowed tiers. Confirmed; values in
  `config.KICKER_SCORING` / `config.DST_SCORING`.
- **Cold-start projections:** backfill from the prior season so there are
  always 3 games to weight (`BACKFILL_SEASONS = [2025]`).
- **Model tiers (steps 3+):** Haiku 4.5 (`claude-haiku-4-5`) for the frequent
  "do you want to act?" gate-check; Sonnet 5 (`claude-sonnet-5`) for real
  decisions (draft picks, trades, chat, waiver bids). Uses the `anthropic`
  SDK, reading `ANTHROPIC_API_KEY` from the environment (or `.env`, or the
  `FFL_ANTHROPIC_API_KEY` alias — see below).
- **Anthropic SDK 1.x API shape (`ffl/llm.py`):** this SDK generation removed
  `temperature`/`top_p`/`top_k` (400 if sent) and assistant-message prefill
  (also 400). So all agent calls: pass no sampling params, request JSON via the
  system prompt and parse it (no prefill), and use `output_config={"effort":…}`
  to tune thinking depth. Future steps must follow the same shape.
- **API key on the managed cloud runtime:** the host reserves the name
  `ANTHROPIC_API_KEY`, so a value set under that name in the cloud env doesn't
  reach app code. Supply the key as `FFL_ANTHROPIC_API_KEY` (cloud env var) or
  in a local gitignored `.env`; `ffl/llm.py` reads either.

## Open decisions (still not finalized)

- **Tick interval** for the continuous loop (needed at step 8). Rough cost at
  hourly ticks with two-tier models is ~$10-20/mo without caching, less with
  prompt caching on stable context — confirm against real usage.
- **Dashboard/digest format** for checking in (needed at step 8).
