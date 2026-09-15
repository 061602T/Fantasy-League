"""SQLite persistence layer.

Single-file database, WAL mode, foreign keys enforced. Holds all runtime
league state; the file is gitignored (runtime state, not source).

Tables (per the brief):
  league               single-row league metadata / clock
  teams                the 8 AI teams, incl. persona fields
  players              draftable player/DST identities (the pool)
  rosters              team<->player ownership
  lineups              weekly starter/bench designations (added: needed to
                       score head-to-head weeks; not in the brief's list)
  draft_picks          snake-draft record
  transactions         trades & waiver/free-agent moves
  matchups             head-to-head weekly results
  player_weekly_scores actual fantasy points per player per week
  chat_log             league group chat (incl. collision negotiations)
"""
from __future__ import annotations

import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS league (
    id            INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    season        INTEGER NOT NULL,
    as_of_week    INTEGER NOT NULL,     -- last completed NFL week
    current_week  INTEGER NOT NULL,     -- league's active matchup week
    num_teams     INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'setup',  -- setup|draft|regular|complete
    settings_json TEXT,                 -- snapshot of scoring/roster settings
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS teams (
    team_id        INTEGER PRIMARY KEY,
    team_name      TEXT NOT NULL,
    gm_name        TEXT NOT NULL,
    personality    TEXT,                -- free-text persona summary
    risk_tolerance TEXT,                -- boom-bust | balanced | safe-floor
    valuation_bias TEXT,                -- e.g. "overvalues rookies"
    chattiness     TEXT,                -- quiet | moderate | trash-talker
    draft_slot     INTEGER,             -- 1..num_teams
    faab_budget    INTEGER NOT NULL DEFAULT 100,
    faab_remaining INTEGER NOT NULL DEFAULT 100,
    wins           INTEGER NOT NULL DEFAULT 0,
    losses         INTEGER NOT NULL DEFAULT 0,
    ties           INTEGER NOT NULL DEFAULT 0,
    points_for     REAL NOT NULL DEFAULT 0,
    points_against REAL NOT NULL DEFAULT 0,
    persona_json   TEXT,                -- full raw persona as generated
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS players (
    player_id  TEXT PRIMARY KEY,        -- nflverse gsis_id, or team abbr for DST
    name       TEXT NOT NULL,
    position   TEXT NOT NULL,           -- QB|RB|WR|TE|K|DST
    nfl_team   TEXT,                    -- current NFL team abbr
    is_dst     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rosters (
    roster_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id       INTEGER NOT NULL REFERENCES teams(team_id),
    player_id     TEXT NOT NULL REFERENCES players(player_id),
    acquired_via  TEXT NOT NULL,        -- draft|trade|waiver|free_agent
    acquired_week INTEGER,
    dropped_week  INTEGER,              -- NULL while still rostered
    UNIQUE (team_id, player_id, acquired_week)
);
-- A player may be owned by at most one team at a time (enforced in code:
-- only one row per player with dropped_week IS NULL).
CREATE INDEX IF NOT EXISTS idx_rosters_active
    ON rosters(player_id) WHERE dropped_week IS NULL;

CREATE TABLE IF NOT EXISTS lineups (
    lineup_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id    INTEGER NOT NULL REFERENCES teams(team_id),
    week       INTEGER NOT NULL,
    player_id  TEXT NOT NULL REFERENCES players(player_id),
    slot       TEXT NOT NULL,           -- QB|RB|WR|TE|FLEX|K|DST|BENCH
    UNIQUE (team_id, week, player_id)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    pick_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    overall_pick  INTEGER NOT NULL UNIQUE,
    round         INTEGER NOT NULL,
    pick_in_round INTEGER NOT NULL,
    team_id       INTEGER NOT NULL REFERENCES teams(team_id),
    player_id     TEXT REFERENCES players(player_id),  -- NULL until made
    picked_at     TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT NOT NULL,     -- trade|waiver_claim|free_agent_add|drop
    status            TEXT NOT NULL,     -- proposed|countered|accepted|rejected|processed|failed
    from_team_id      INTEGER REFERENCES teams(team_id),
    to_team_id        INTEGER REFERENCES teams(team_id),
    round             INTEGER DEFAULT 0, -- negotiation round (trades)
    faab_bid          INTEGER,           -- waiver bids
    details_json      TEXT NOT NULL,     -- players/faab moving each way
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at       TEXT
);

CREATE TABLE IF NOT EXISTS matchups (
    matchup_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    week           INTEGER NOT NULL,
    home_team_id   INTEGER NOT NULL REFERENCES teams(team_id),
    away_team_id   INTEGER NOT NULL REFERENCES teams(team_id),
    home_points    REAL,
    away_points    REAL,
    winner_team_id INTEGER REFERENCES teams(team_id),  -- NULL=unplayed; final+NULL=tie
    status         TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|final
    UNIQUE (week, home_team_id, away_team_id)
);

CREATE TABLE IF NOT EXISTS player_weekly_scores (
    player_id       TEXT NOT NULL REFERENCES players(player_id),
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    fantasy_points  REAL NOT NULL,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, season, week)
);

CREATE TABLE IF NOT EXISTS chat_log (
    chat_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER REFERENCES teams(team_id),  -- NULL = system message
    event_type  TEXT,             -- trade_talk|trash_talk|collision|system|...
    message     TEXT NOT NULL,
    txn_id      INTEGER REFERENCES transactions(txn_id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: str = None) -> sqlite3.Connection:
    """Open a connection with WAL mode, full sync, and foreign keys enforced.

    Creates the database's parent directory if needed (the DB lives outside the
    git checkout). synchronous=FULL is set explicitly -- on a Pi that can lose
    power, it keeps committed transactions durable and the file uncorrupted at
    the SQLite layer (physical SD-card corruption is handled by off-card
    backups, not this).
    """
    path = path or config.DB_PATH
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(path: str = None) -> sqlite3.Connection:
    """Create the schema (idempotent) and return an open connection."""
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
