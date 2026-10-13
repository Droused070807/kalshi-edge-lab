"""SQLite storage.

Snapshot tables (runs, kalshi_snapshots, odds_snapshots, quotes, settlements) are append-only:
the code never UPDATEs or DELETEs them, so any past moment can be reconstructed exactly.
Only paper_positions changes state (open -> settled), and its entry fields are never rewritten.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    league TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,              -- running | ok | skipped | error
    kalshi_markets INTEGER DEFAULT 0,
    odds_games INTEGER DEFAULT 0,
    matched_games INTEGER DEFAULT 0,
    quotes INTEGER DEFAULT 0,
    new_signals INTEGER DEFAULT 0,
    new_positions INTEGER DEFAULT 0,
    odds_credits_left INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS kalshi_snapshots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    league TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT,
    sub_title TEXT,
    status TEXT,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL, last_price REAL,
    volume REAL,
    close_time TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_ks_ticker_ts ON kalshi_snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    league TEXT NOT NULL,
    game_id TEXT NOT NULL,
    home_team TEXT, away_team TEXT, commence_time TEXT,
    bookmaker TEXT NOT NULL,
    book_last_update TEXT,
    team TEXT NOT NULL,
    decimal_price REAL NOT NULL,
    novig_p REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_os_game_ts ON odds_snapshots(game_id, ts);

-- One row per Kalshi contract side per run, whenever a valid consensus existed.
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts TEXT NOT NULL,
    league TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT,
    side TEXT NOT NULL,                -- yes | no
    game_id TEXT NOT NULL,
    home_team TEXT, away_team TEXT,
    team TEXT NOT NULL,                -- team named by the Kalshi market
    backs_team TEXT NOT NULL,          -- team this side wins with
    commence_time TEXT NOT NULL,
    hours_to_start REAL,
    consensus_p REAL NOT NULL,         -- probability this side wins
    consensus_src TEXT,
    n_books INTEGER,
    ask REAL NOT NULL, bid REAL, spread REAL,
    fee REAL NOT NULL,                 -- per-contract taker fee estimate (unrounded)
    ev REAL NOT NULL,                  -- consensus_p - ask - fee, in dollars per $1 contract
    roi REAL NOT NULL,
    kelly_info REAL,                   -- informational only, never used for sizing
    early INTEGER, wide INTEGER
);
CREATE INDEX IF NOT EXISTS ix_q_ticker_side_ts ON quotes(ticker, side, ts);
CREATE INDEX IF NOT EXISTS ix_q_run ON quotes(run_id);

-- First time each contract side crossed SIGNAL_MIN_EDGE.
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    quote_id INTEGER NOT NULL REFERENCES quotes(id),
    ts TEXT NOT NULL,
    league TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    game_id TEXT NOT NULL,
    UNIQUE(ticker, side)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER REFERENCES signals(id),
    quote_id INTEGER NOT NULL REFERENCES quotes(id),
    opened_at TEXT NOT NULL,
    league TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    game_id TEXT NOT NULL,
    backs_team TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    entry_p REAL NOT NULL,             -- consensus probability at entry
    contracts INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    fee REAL NOT NULL,                 -- rounded order fee in dollars
    cost REAL NOT NULL,                -- contracts * avg_price + fee
    fills TEXT,                        -- JSON list of [price, qty] levels taken
    status TEXT NOT NULL,              -- open | settled | void
    result TEXT,
    payout REAL,
    pnl REAL,
    settled_at TEXT,
    UNIQUE(ticker, side)
);

CREATE TABLE IF NOT EXISTS settlements (
    ticker TEXT PRIMARY KEY,
    result TEXT NOT NULL,              -- yes | no | void
    fetched_at TEXT NOT NULL,
    raw TEXT
);

-- Append-only enforcement: the database itself refuses edits to historical snapshots.
CREATE TRIGGER IF NOT EXISTS ro_ks_u BEFORE UPDATE ON kalshi_snapshots BEGIN SELECT RAISE(ABORT, 'kalshi_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_ks_d BEFORE DELETE ON kalshi_snapshots BEGIN SELECT RAISE(ABORT, 'kalshi_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_os_u BEFORE UPDATE ON odds_snapshots BEGIN SELECT RAISE(ABORT, 'odds_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_os_d BEFORE DELETE ON odds_snapshots BEGIN SELECT RAISE(ABORT, 'odds_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_q_u BEFORE UPDATE ON quotes BEGIN SELECT RAISE(ABORT, 'quotes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_q_d BEFORE DELETE ON quotes BEGIN SELECT RAISE(ABORT, 'quotes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_sg_u BEFORE UPDATE ON signals BEGIN SELECT RAISE(ABORT, 'signals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_sg_d BEFORE DELETE ON signals BEGIN SELECT RAISE(ABORT, 'signals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_st_u BEFORE UPDATE ON settlements BEGIN SELECT RAISE(ABORT, 'settlements is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ro_st_d BEFORE DELETE ON settlements BEGIN SELECT RAISE(ABORT, 'settlements is append-only'); END;
-- Paper positions: entry fields are frozen once written; only settlement fields may change.
CREATE TRIGGER IF NOT EXISTS ro_pp_entry BEFORE UPDATE OF quote_id, opened_at, ticker, side, game_id, backs_team,
    commence_time, entry_p, contracts, avg_price, fee, cost, fills ON paper_positions
    BEGIN SELECT RAISE(ABORT, 'paper position entry fields are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ro_pp_d BEFORE DELETE ON paper_positions BEGIN SELECT RAISE(ABORT, 'paper_positions cannot be deleted'); END;

-- Paper fills that were attempted but rejected (no depth at edge, caps hit, conflicts).
CREATE TABLE IF NOT EXISTS paper_rejections (
    id INTEGER PRIMARY KEY,
    quote_id INTEGER NOT NULL REFERENCES quotes(id),
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


_db_path: Path | None = None


def init(path: Path) -> None:
    global _db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = path
    with connect() as c:
        c.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    assert _db_path is not None, "db.init() not called"
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
