"""Paper trading. Simulated only: nothing here can place a real order."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from .adapters.kalshi import KalshiClient
from .config import Settings
from .db import iso
from .engine.ev import asks_for, parse_orderbook, simulate_fill


def realized_pnl(conn: sqlite3.Connection) -> float:
    return conn.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE status='settled'").fetchone()[0]


def equity(conn: sqlite3.Connection, s: Settings) -> float:
    return s.paper_start_bankroll + realized_pnl(conn)


def open_cost(conn: sqlite3.Connection) -> float:
    return conn.execute("SELECT COALESCE(SUM(cost),0) FROM paper_positions WHERE status='open'").fetchone()[0]


def available_cash(conn: sqlite3.Connection, s: Settings) -> float:
    """Cash not tied up in open positions. A position can never cost more than this."""
    return equity(conn, s) - open_cost(conn)


def today_pnl(conn: sqlite3.Connection, s: Settings, now: datetime) -> float:
    tz = ZoneInfo(s.timezone)
    start_local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM paper_positions WHERE status='settled' AND settled_at >= ?",
        (iso(start_local),)).fetchone()[0]


def _reject(conn, q: dict, now: datetime, reason: str) -> None:
    conn.execute("INSERT INTO paper_rejections(quote_id, ts, ticker, side, reason) VALUES (?,?,?,?,?)",
                 (q["id"], iso(now), q["ticker"], q["side"], reason))


async def consider(conn: sqlite3.Connection, q: dict, kalshi: KalshiClient, s: Settings, now: datetime) -> bool:
    """Open a simulated position for quote q if every rule passes. Returns True when a position opens."""
    if q["ev"] < s.paper_min_edge:
        return False
    if conn.execute("SELECT 1 FROM paper_positions WHERE ticker=? AND side=?", (q["ticker"], q["side"])).fetchone():
        return False
    if conn.execute("SELECT 1 FROM paper_positions WHERE game_id=? AND status='open'", (q["game_id"],)).fetchone():
        _reject(conn, q, now, "already holding a position on this game")
        return False

    eq = equity(conn, s)
    if today_pnl(conn, s, now) <= -s.paper_max_daily_loss_pct * eq:
        _reject(conn, q, now, "daily loss limit reached")
        return False

    try:
        book = parse_orderbook(await kalshi.orderbook(q["ticker"]))
    except Exception as e:  # noqa: BLE001
        _reject(conn, q, now, f"orderbook unavailable: {e}")
        return False

    budget = min(s.paper_max_position_pct * eq, available_cash(conn, s))
    if budget < 1.0:
        _reject(conn, q, now, "not enough uncommitted cash")
        return False
    fill = simulate_fill(asks_for(book, q["side"]), q["consensus_p"], s.paper_min_edge, budget, s.taker_fee_rate)
    if not fill["contracts"]:
        _reject(conn, q, now, "no orderbook depth at a price that keeps the edge")
        return False

    sig = conn.execute("SELECT id FROM signals WHERE ticker=? AND side=?", (q["ticker"], q["side"])).fetchone()
    conn.execute(
        """INSERT INTO paper_positions(signal_id, quote_id, opened_at, league, ticker, side, game_id, backs_team,
           commence_time, entry_p, contracts, avg_price, fee, cost, fills, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
        (sig[0] if sig else None, q["id"], iso(now), q["league"], q["ticker"], q["side"], q["game_id"],
         q["backs_team"], q["commence_time"], q["consensus_p"], fill["contracts"], fill["avg_price"],
         fill["fee"], fill["cost"], json.dumps(fill["fills"])))
    return True
