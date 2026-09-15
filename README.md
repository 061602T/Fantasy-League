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
- [x] **5. Weekly scoring cycle** — balanced double round-robin schedule
      (14 weeks), deterministic best-by-projection lineups, head-to-head scoring
      from real per-player weekly points, and standings recomputed by
      aggregating final matchups (re-scoring a week is idempotent). League week
      N = NFL week N; scoring is parameterized by season. Verified **live** on
      real 2026 week-1 results (4 matchups, realistic 150–194 pt totals) and via
      a full **2025 backtest** (14 weeks scored on the drafted rosters, on a
      snapshot copy — real DB untouched). Lineups are deterministic (no API
      calls), so scoring is cheap and reproducible. Offline tests in
      `scripts/test_season.py`.
- [x] **6. Trade & waiver systems** — the league's first Sonnet-driven "real
      decisions," gated by a cheap Haiku "do you want to act?" check.
      **Trades:** a proposer builds an even N-for-N package (1–3 each way, rosters
      stay 15); over ≤3 rounds the decider accepts, walks away, or counters by
      haggling the FAAB sweetener (players fixed → counters stay unambiguous). A
      deal executes only if both rosters stay legal and FAAB is affordable.
      **Waivers:** one blind FAAB claim (add + drop) per team; resolved
      highest-bid-first, tie-broken by remaining FAAB then draft slot; FAAB spent
      only on a win. All movement persists to `transactions`/`rosters`; trade
      talk + waiver results go to `chat_log`. Verified **live**: a full 3-round
      in-character FAAB negotiation, and a contested waiver (higher bid won,
      loser kept its budget, add/drop executed). Offline tests in
      `scripts/test_market.py`.
- [x] **7. Event-aware group chat** — GMs banter in a shared channel, reacting
      in character to what just happened. A Haiku gate (weighted by each GM's
      `chattiness`) decides who chimes in; Sonnet writes the line; a couple of
      rounds let them reply to each other. `react_to_event(headline, detail,
      involvement)` is generic (the tick loop can fire it for any event);
      `react_to_week` builds the summary from real matchup results. Verified
      **live** on week 1: GMs cited real scores/margins and the week high/low,
      threaded replies formed rivalries, and the one `quiet` GM stayed silent
      despite winning the biggest blowout. Persists to `chat_log`. Offline tests
      in `scripts/test_chat.py`.
- [x] **8. Tick-loop scheduler + check-in dashboard** — `ffl/tick.py` runs one
      autonomous step: refresh real results, and when a new NFL week has
      finished, score that league week, run waivers, and let the GMs react in
      chat; every tick regenerates the dashboard, and an advancing tick backs
      up. Idle ticks are cheap (no API) and safe to run hourly; catch-up is
      automatic. `ffl/dashboard.py` renders a self-contained HTML check-in
      (standings, latest matchups, recent moves, chat) that works in light/dark
      and at phone width. Verified **live**: one tick scored week 1, resolved a
      waiver, posted 14 chat lines, and wrote the dashboard. Offline tests in
      `scripts/test_tick.py`. **Decided:** hourly tick (`FFL_TICK_INTERVAL`),
      HTML dashboard.

## Layout

    ffl/
      config.py       league timeline, scoring rules, projection params, pool targets
      data.py         nflverse data access + local parquet cache
      scoring.py      offense (official PPR) + kicker + DST scoring
      projections.py  game logs, ROS projection, draft pool
      llm.py          Anthropic SDK wrapper (Haiku gate / Sonnet decision tiers)
      personas.py     persona generation, name-collision negotiation, persistence
      draft.py        snake-draft order, roster/needs logic, live pick decisions
      season.py       schedule, lineups, weekly scoring, standings
      rosters.py      shared roster/legality helpers (used by the market)
      market.py       trades (negotiation) + FAAB waivers, with Haiku gates
      chat.py         event-aware group chat (Haiku gate + Sonnet banter)
      tick.py         one autonomous league step (score/waivers/chat/dashboard)
      dashboard.py    self-contained HTML check-in dashboard
      backup.py       online SQLite backup helper (cron + post-draft one-off)
    scripts/
      show_pool.py      print the current draft pool
      gen_personas.py   generate + persist the 8 GM personas (real API calls)
      test_personas.py  offline tests for the collision/persistence logic
      run_draft.py      run the live snake draft (real API calls)
      test_draft.py     offline tests for the draft engine
      run_week.py       score one league week vs real data + print standings
      backtest_2025.py  score the drafted rosters over the finished 2025 season
      test_season.py    offline tests for the scoring cycle
      run_market.py     run live trades / waivers (real API calls)
      test_market.py    offline tests for trades + waivers
      run_chat.py       generate live group-chat reactions to a week
      test_chat.py      offline tests for the group chat
      run_tick.py       run the tick loop (one step, or --loop)
      test_tick.py      offline tests for the tick loop + dashboard
      backup_db.py      cron / on-demand DB backup CLI
      test_backup.py    offline tests for the backup helper

## Setup

    pip install -r requirements.txt
    python -m scripts.show_pool

Steps 3+ make real Claude API calls and need a key. Export it, or drop it in a
gitignored `.env` (the SDK wrapper reads either):

    export ANTHROPIC_API_KEY=sk-ant-...      # or: echo 'ANTHROPIC_API_KEY=...' > .env
    python -m scripts.gen_personas           # generate + persist the 8 GM personas
    python -m scripts.run_draft              # run the snake draft
    python -m scripts.run_tick --loop        # then run the league (or cron --once)
    python -m scripts.test_personas          # offline logic tests (no API key needed)

The tick loop drives everything after the draft: run `python -m scripts.run_tick`
once per interval (hourly by default, `FFL_TICK_INTERVAL`) via cron, or with
`--loop` under systemd. It scores completed weeks, runs waivers, posts chat, and
rewrites the dashboard to `FFL_DASHBOARD_PATH` (default `~/ffl-data/dashboard.html`).

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
  to tune thinking depth — but only for models that accept it (Haiku 4.5 rejects
  `effort`, so `llm.gate()` omits it). Future steps must follow the same shape.
- **Tick cadence:** hourly (`config.TICK_INTERVAL_SECONDS`, env `FFL_TICK_INTERVAL`).
  Idle ticks are cheap no-ops; LLM spend only lands when a new NFL week completes.
- **Check-in format:** a self-contained HTML dashboard regenerated each tick
  (`ffl/dashboard.py`, `FFL_DASHBOARD_PATH`).
- **API key on the managed cloud runtime:** the host reserves the name
  `ANTHROPIC_API_KEY`, so a value set under that name in the cloud env doesn't
  reach app code. Supply the key as `FFL_ANTHROPIC_API_KEY` (cloud env var) or
  in a local gitignored `.env`; `ffl/llm.py` reads either.

## Build complete

All 8 steps are built and verified against real data/API. The league runs
itself: `run_tick` scores completed weeks, runs the market, posts chat, and
refreshes the dashboard. Possible future work (not in the original brief):
season playoffs/bracket, mid-week (not just post-week) market activity, and a
push/email digest alongside the HTML dashboard.
