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
      playoffs.py     single-elimination bracket (top seeds, weeks 15-16)
      dashboard.py    self-contained HTML check-in dashboard
      digest.py       text notification digest + webhook/command delivery
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
      test_playoffs.py  offline tests for the playoff bracket
      test_digest.py    offline tests for the notification digest
      send_digest_email.py  email the digest via Gmail SMTP (FFL_DIGEST_CMD target)
      test_email.py     offline tests for the emailer (mocked SMTP)
      backup_db.py      cron / on-demand DB backup CLI
      test_backup.py    offline tests for the backup helper
      test_integrity.py offline tests for the integrity gate + restore

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
- **One-off backups:** `gen_personas.py` (after creating the league) and
  `run_draft.py` (after the draft) each take an immediate backup, since those
  non-reproducible real-API events can't be regenerated identically. The tick
  also takes a dedicated backup when a champion is crowned.
- **Not an export/import feature:** backups only restore the same DB
  byte-for-byte — there is no path to import test data into a real league.
- **Startup integrity gate (wired):** `db.preflight()` runs at the top of
  `gen_personas`, `run_draft`, and `run_tick`. It `PRAGMA quick_check`s the DB
  and, if corrupt, restores the newest *verified* backup via
  `backup.restore_latest_backup()` (moving the corrupt file aside as
  `*.corrupt-<ts>`); if no valid backup exists it refuses to run rather than
  operate on a broken database. Covered by `scripts/test_integrity.py` (corrupts
  a real DB and asserts recovery).

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
refreshes the dashboard.

### Extensions (beyond the original brief)

- **Playoffs** (`ffl/playoffs.py`): the top `PLAYOFF_TEAMS` seeds (default 4)
  play a single-elimination bracket in NFL weeks after the regular season
  (1 v 4, 2 v 3 → final); higher seed is home and advances on a tie; playoff
  games don't count toward regular-season records. The tick drives the bracket
  automatically; the dashboard shows a champion banner. Verified on the 2025
  backtest (a real champion crowned from weeks 15-16).
- **Mid-week market activity** (`ffl/tick.py` + `market.attempt_one_trade`): on
  an otherwise idle tick, a small, probability-gated chance (`FFL_MIDWEEK_TRADE_PROB`
  / `FFL_MIDWEEK_CHAT_PROB`) of a trade attempt or ambient chat, so the league
  has life between games. The Haiku gate still decides who engages, keeping
  hourly ticks cheap.
- **Notification digest** (`ffl/digest.py`): a concise text digest written each
  advancing tick to `FFL_DIGEST_PATH`, plus provider-agnostic delivery — set
  `FFL_DIGEST_WEBHOOK` (POST) or `FFL_DIGEST_CMD` (stdin, e.g. `ntfy publish`,
  `mail`) to route it to push/email without any third-party dependency.
  - **Email via Gmail** (`scripts/send_digest_email.py`, stdlib only): reads the
    digest (piped on stdin, else `FFL_DIGEST_PATH`) and sends it over SMTP_SSL
    (port 465) with a Gmail **app password**. Env: `FFL_GMAIL_ADDRESS`,
    `FFL_GMAIL_APP_PASSWORD`, and optional `FFL_DIGEST_TO` (defaults to the Gmail
    address, so you email yourself). Nothing is hardcoded; a bad-credentials or
    network failure logs to stderr and exits non-zero, so a failed email never
    crashes the tick. To enable, set (once you've made an app password):

        export FFL_GMAIL_ADDRESS=you@gmail.com
        export FFL_GMAIL_APP_PASSWORD=...            # Gmail app password
        export FFL_DIGEST_CMD='python3 -m scripts.send_digest_email'

    Leave `FFL_DIGEST_CMD` unset until you're ready — the digest still writes to
    disk regardless.

- **Championship backup:** the tick takes a dedicated explicit backup the moment
  a champion is crowned (a non-reproducible, high-value event), on top of the
  routine per-advance and cron backups.

- **GitHub Pages publishing** (`ffl/ghpages.py`): the freshly generated
  dashboard is pushed to a GitHub Pages repo as `index.html`, giving the league
  a public live page. Two things trigger a push: `run_tick` after an
  advancing/notable tick (same trigger as the digest), **and** `run_chat_tick`
  whenever the ambient chat loop actually posts a message — so the live page
  tracks the conversation between the hourly scoring ticks (pass `--no-publish`
  to the chat tick to opt out). It clones the Pages repo once into a directory
  *outside* the code checkout, and only commits/pushes when the file actually
  changed (no empty commits). Any git/network/auth failure is logged and
  swallowed, never crashing the loop. Disabled (a no-op) unless configured via
  env:
  - `FFL_GH_DASHBOARD_TOKEN` — a GitHub token with push access to the Pages repo.
  - `FFL_GH_DASHBOARD_REPO` — `owner/repo` (or a full GitHub URL). **Use a repo
    dedicated to the Pages site, not the code repo** — this pushes `index.html`
    to its `main` root.
  - `FFL_GH_DASHBOARD_DIR` — where to keep the clone (default
    `~/ffl-data/dashboard-repo`).
  - `FFL_GH_DASHBOARD_BRANCH` — Pages branch (default `main`).

  The token is read at runtime and embedded only in the push URL passed to
  `git push`; the clone's stored `origin` is reset to the clean URL, so the
  token is never written to `.git/config`, and it's scrubbed from any error
  text. Offline test: `scripts/test_ghpages.py` (git mocked).

## Analytics (statistical estimates, not scores)

Deterministic, statistical add-ons — no LLM calls, no extra API spend per tick.
These are **estimates from historical scoring, not predictions of the real NFL
games and not the league's actual points.** Built and validated one at a time.

- **Weekly score projections** (`ffl/scoreproj.py`): the projected total the
  dashboard shows as “proj N.N” beside each team's actual score.
  - *Per player:* the simple mean of that player's most recent
    `SCORE_PROJ_WINDOW` (default **4**) actual weekly scores *before* the target
    week, from `player_weekly_scores`. “Most recent” spans the season boundary,
    so an early-season week isn't projected off a single game. No prior game →
    no projection (contributes 0, flagged as uncovered).
  - *Byes:* a starter whose NFL team is idle that week is projected at 0 (they
    can't score). Byes are known in advance from the schedule, so this isn't
    hindsight — `scoreproj.teams_on_bye()` derives them from the loaded
    schedule; the core math takes the bye set as an argument and stays pure.
  - *Per team:* the sum of its starters' projections — the *actual* starters
    when a lineup is set (a scored week), otherwise the optimal lineup by these
    same projections (an upcoming week).
  - *Validation (real 2025 backtest, `scripts/validate_scoreproj.py`):* over 104
    team-weeks, **MAE ≈ 22 pts, bias ≈ +5 pts, corr ≈ 0.59** against actual team
    totals. Bye-awareness is what makes it usable — without it the model reads
    ~13 pts high on bye-heavy weeks (MAE ≈ 28). The residual +5 is genuine
    game-day inactives, which recent form can't foresee. Offline tests:
    `scripts/test_scoreproj.py`.

- **Matchup win probability** (`ffl/winprob.py`): the “Upcoming — Week N”
  section shows each side's chance of winning.
  - *Model:* a normal-approximation on the margin. Each team's weekly total is
    treated as `Normal(mean, sd²)` from its **season-so-far** actual totals, so
    the margin is `Normal(mean_a−mean_b, sd_a²+sd_b²)` and
    `P(A wins) = Φ((mean_a−mean_b) / √(sd_a²+sd_b²))` (Φ = standard-normal CDF).
    The two sides' probabilities sum to 1 (a tie has ~0 probability under a
    continuous model).
  - *Spread when thin:* a team's `sd` is **floored at the pooled league sd**
    (root-mean of per-team variances) so a freakishly tight early sample doesn't
    make the model overconfident; with <2 games it falls back to that pooled sd,
    then to `WINPROB_DEFAULT_SD`. Zero games → 50/50.
  - *Validation (real 2025 backtest, `scripts/validate_winprob.py`):* over 52
    regular-season matchups, **Brier 0.237** and **log loss 0.677 — both beating
    the coin-flip baseline (0.250 / 0.693)** — with the favourite winning 63.5%
    of the time. The sd-floor is what pulls log loss below the baseline (an
    un-floored normal-approx is overconfident and loses to a coin flip on log
    loss). Offline tests: `scripts/test_winprob.py`.
  - *Note:* win % (season-long mean/variance) and “proj” (recent form) are
    independent estimates, so a team can be favoured to win yet carry a lower
    recent-form projection, or vice versa — they answer different questions.

- **Playoff odds** (`ffl/playoffodds.py`): the “Playoff%” column on the
  standings — each team's chance of finishing in the top `PLAYOFF_TEAMS`.
  - *Method:* a Monte-Carlo simulation. Current records are fixed; each of
    `PLAYOFF_SIMS` (default **10,000**) runs simulates every remaining game by
    drawing both teams' scores from their scoring distribution (the same floored
    `Normal(mean, sd)` as win probability), re-seeds the final standings exactly
    as the league does (`wins` desc, then `points_for` desc), and flags the top
    seeds. The odds are the share of runs a team made the cut. Vectorized with
    numpy — ~18 ms for a full season, so it recomputes every tick. With no games
    left it's deterministic (current top seeds 100%, rest 0%).
  - *Validation (real 2025 backtest, `scripts/validate_playoffodds.py`):* scoring
    week by week and forecasting from each week's state, **Brier 0.032** vs a
    naive flat-`4/8` baseline of 0.25 — the odds converge correctly (the four
    eventual playoff teams stay 68–100%, the rest 0–2%, and the one true bubble
    team reads ~46% mid-season before fading). Offline tests:
    `scripts/test_playoffodds.py`.
