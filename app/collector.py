"""Scheduled data collection, signal logging, paper fills and settlement."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

import httpx

from . import db, paper
from .adapters.kalshi import KalshiClient, normalize
from .adapters.odds import OddsClient, OddsError
from .config import LEAGUES, Settings
from .db import iso, utcnow
from .engine.consensus import book_probs
from .engine.quotes import build_quotes

log = logging.getLogger("edgelab")

QUOTE_COLS = ["run_id", "ts", "league", "ticker", "event_ticker", "side", "game_id", "home_team", "away_team",
              "team", "backs_team", "commence_time", "hours_to_start", "consensus_p", "consensus_src", "n_books",
              "ask", "bid", "spread", "fee", "ev", "roi", "kelly_info", "early", "wide"]


def _last_credits() -> int | None:
    with db.connect() as c:
        r = c.execute("SELECT odds_credits_left FROM runs WHERE odds_credits_left IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1").fetchone()
        return r[0] if r else None


async def collect_league(league: str, s: Settings, kalshi: KalshiClient, odds: OddsClient) -> dict:
    cfg = LEAGUES[league]
    now = utcnow()
    ts = iso(now)
    with db.connect() as c:
        run_id = c.execute("INSERT INTO runs(league, started_at, status) VALUES (?,?, 'running')",
                           (league, ts)).lastrowid
    summary = {"run_id": run_id, "league": league, "status": "ok", "note": None}

    def finish(**kw):
        summary.update(kw)
        with db.connect() as c:
            c.execute("""UPDATE runs SET finished_at=?, status=?, kalshi_markets=?, odds_games=?, matched_games=?,
                         quotes=?, new_signals=?, new_positions=?, odds_credits_left=?, note=? WHERE id=?""",
                      (iso(utcnow()), summary["status"], summary.get("kalshi_markets", 0),
                       summary.get("odds_games", 0), summary.get("matched_games", 0), summary.get("quotes", 0),
                       summary.get("new_signals", 0), summary.get("new_positions", 0),
                       summary.get("odds_credits_left"), summary.get("note"), run_id))
        return summary

    try:
        markets = await kalshi.open_markets(cfg["kalshi"])
        with db.connect() as c:
            c.executemany(
                """INSERT INTO kalshi_snapshots(run_id, ts, league, ticker, event_ticker, sub_title, status,
                   yes_bid, yes_ask, no_bid, no_ask, last_price, volume, close_time, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, ts, league, n["ticker"], n["event_ticker"], n["sub_title"], n["status"], n["yes_bid"],
                  n["yes_ask"], n["no_bid"], n["no_ask"], n["last_price"], n["volume"], n["close_time"],
                  json.dumps(m, separators=(",", ":"))) for m in markets for n in [normalize(m)]])
        if not markets:
            return finish(status="skipped", kalshi_markets=0, note="No open Kalshi markets; odds not fetched")

        credits = _last_credits()
        if credits is not None and credits < s.odds_min_credits:
            return finish(status="skipped", kalshi_markets=len(markets),
                          note=f"Odds API credits low ({credits}); raise ODDS_MIN_CREDITS or wait for reset")

        games, credits = await odds.h2h(cfg["odds"])
        with db.connect() as c:
            c.executemany(
                """INSERT INTO odds_snapshots(run_id, ts, league, game_id, home_team, away_team, commence_time,
                   bookmaker, book_last_update, team, decimal_price, novig_p) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, ts, league, g["id"], g["home_team"], g["away_team"], g["commence_time"], b["key"],
                  b["last_update"], team, b["prices"][team], p)
                 for g in games for b in book_probs(g) for team, p in b["p"].items()])

        quotes, matched = build_quotes(markets, games, now, s)
        new_signals = new_positions = 0
        with db.connect() as c:
            for q in quotes:
                q.update(run_id=run_id, ts=ts, league=league)
                q["id"] = c.execute(
                    f"INSERT INTO quotes({','.join(QUOTE_COLS)}) VALUES ({','.join('?' * len(QUOTE_COLS))})",
                    [q[k] for k in QUOTE_COLS]).lastrowid
                if q["ev"] >= s.signal_min_edge:
                    cur = c.execute("INSERT OR IGNORE INTO signals(quote_id, ts, league, ticker, side, game_id) "
                                    "VALUES (?,?,?,?,?,?)",
                                    (q["id"], ts, league, q["ticker"], q["side"], q["game_id"]))
                    new_signals += cur.rowcount
            # Paper trading, best edges first. Kelly is stored for reference only; sizing is a flat bankroll %.
            for q in quotes:
                if await paper.consider(c, q, kalshi, s, now):
                    new_positions += 1

        return finish(kalshi_markets=len(markets), odds_games=len(games), matched_games=matched,
                      quotes=len(quotes), new_signals=new_signals, new_positions=new_positions,
                      odds_credits_left=credits,
                      note=None if matched or not games else "Sportsbook games loaded but none matched Kalshi markets")
    except OddsError as e:
        return finish(status="error", note=str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("collect %s failed", league)
        return finish(status="error", note=f"{type(e).__name__}: {e}")


async def settle(s: Settings, kalshi: KalshiClient) -> int:
    """Record results for finished games (signals and positions) and settle paper positions."""
    cutoff = iso(utcnow() - timedelta(hours=3))
    with db.connect() as c:
        tickers = [r[0] for r in c.execute(
            """SELECT DISTINCT q.ticker FROM signals sg JOIN quotes q ON q.id = sg.quote_id
               WHERE q.commence_time < ? AND sg.ticker NOT IN (SELECT ticker FROM settlements)
               UNION
               SELECT DISTINCT ticker FROM paper_positions
               WHERE status='open' AND commence_time < ? AND ticker NOT IN (SELECT ticker FROM settlements)""",
            (cutoff, cutoff))]
    found = 0
    for t in tickers:
        try:
            raw = await kalshi.market(t)
        except Exception:  # noqa: BLE001
            continue
        result = (raw.get("result") or "").lower()
        if result not in ("yes", "no", "void") or raw.get("status") not in ("settled", "finalized", "determined"):
            continue
        with db.connect() as c:
            c.execute("INSERT OR IGNORE INTO settlements(ticker, result, fetched_at, raw) VALUES (?,?,?,?)",
                      (t, result, iso(utcnow()), json.dumps(raw, separators=(",", ":"))))
        found += 1

    with db.connect() as c:
        for p in db.rows(c, """SELECT p.id, p.side, p.contracts, p.cost, st.result FROM paper_positions p
                               JOIN settlements st ON st.ticker = p.ticker WHERE p.status='open'"""):
            if p["result"] == "void":
                payout, status = p["cost"], "void"  # refunded at cost
            else:
                payout, status = (float(p["contracts"]) if p["side"] == p["result"] else 0.0), "settled"
            c.execute("UPDATE paper_positions SET status=?, result=?, payout=?, pnl=?, settled_at=? WHERE id=?",
                      (status, p["result"], payout, round(payout - p["cost"], 4), iso(utcnow()), p["id"]))
    return found


class Scheduler:
    def __init__(self, s: Settings):
        self.s = s
        self.lock = asyncio.Lock()
        self.next_run: str | None = None
        self.last_results: list[dict] = []

    async def run_once(self) -> list[dict]:
        async with self.lock:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "kalshi-edge-lab/0.1"}) as http:
                kalshi = KalshiClient(self.s.kalshi_base, http)
                odds = OddsClient(self.s.odds_api_key, self.s.odds_regions, http)
                results = [await collect_league(lg, self.s, kalshi, odds) for lg in self.s.leagues]
                try:
                    settled = await settle(self.s, kalshi)
                    results.append({"league": "settlement", "status": "ok", "note": f"{settled} new results"})
                except Exception as e:  # noqa: BLE001
                    results.append({"league": "settlement", "status": "error", "note": str(e)})
            self.last_results = results
            return results

    async def loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("collection cycle failed")
            self.next_run = iso(utcnow() + timedelta(minutes=self.s.collect_interval_min))
            await asyncio.sleep(self.s.collect_interval_min * 60)
