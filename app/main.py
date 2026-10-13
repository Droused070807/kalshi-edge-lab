"""FastAPI app: JSON API plus the dashboard at /."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from . import analytics, db
from .collector import Scheduler
from .config import LEAGUES, settings as S

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
STATIC = Path(__file__).parent / "static"
scheduler = Scheduler(S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(S.db_path)
    task = asyncio.create_task(scheduler.loop()) if S.collector_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Kalshi Edge Lab", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    with db.connect() as c:
        runs = db.rows(c, "SELECT * FROM runs ORDER BY id DESC LIMIT 20")
    return {
        "leagues": [{"key": k, "label": LEAGUES[k]["label"]} for k in S.leagues],
        "odds_key_set": bool(S.odds_api_key),
        "collector_enabled": S.collector_enabled,
        "interval_min": S.collect_interval_min,
        "next_run": scheduler.next_run,
        "running": scheduler.lock.locked(),
        "rules": {
            "signal_min_edge": S.signal_min_edge, "paper_min_edge": S.paper_min_edge,
            "start_bankroll": S.paper_start_bankroll, "max_position_pct": S.paper_max_position_pct,
            "max_daily_loss_pct": S.paper_max_daily_loss_pct, "taker_fee_rate": S.taker_fee_rate,
            "min_books": S.min_books, "odds_max_age_min": S.odds_max_age_min,
            "real_money_execution": False,
        },
        "recent_runs": runs,
    }


@app.post("/api/collect")
async def collect_now():
    if scheduler.lock.locked():
        raise HTTPException(409, "A collection run is already in progress")
    return {"results": await scheduler.run_once()}


@app.get("/api/markets")
def markets(league: str | None = None):
    """Every priced contract side from each league's latest successful run."""
    with db.connect() as c:
        lgs = [league] if league else list(S.leagues)
        out = []
        for lg in lgs:
            r = c.execute("SELECT id, finished_at FROM runs WHERE league=? AND status='ok' ORDER BY id DESC LIMIT 1",
                          (lg,)).fetchone()
            if r:
                out += db.rows(c, "SELECT * FROM quotes WHERE run_id=? ORDER BY roi DESC", (r[0],))
    return {"quotes": out}


@app.get("/api/signals")
def signals(league: str | None = None, min_edge: float = 0.0, limit: int = Query(200, le=2000)):
    sql = """SELECT q.*, sg.id AS signal_id, sg.ts AS signal_ts, st.result,
                    p.id AS position_id, p.status AS position_status, p.pnl AS position_pnl,
                    (SELECT ev FROM quotes l WHERE l.ticker=q.ticker AND l.side=q.side ORDER BY l.ts DESC LIMIT 1)
                        AS latest_ev
             FROM signals sg JOIN quotes q ON q.id = sg.quote_id
             LEFT JOIN settlements st ON st.ticker = sg.ticker
             LEFT JOIN paper_positions p ON p.ticker = sg.ticker AND p.side = sg.side
             WHERE q.ev >= ?"""
    params: list = [min_edge]
    if league:
        sql += " AND sg.league = ?"
        params.append(league)
    sql += " ORDER BY sg.ts DESC, q.ev DESC LIMIT ?"
    params.append(limit)
    with db.connect() as c:
        rows = db.rows(c, sql, params)
    for r in rows:
        r["quote_id"] = r["id"]
        r["id"] = r.pop("signal_id")
    return {"signals": rows}


@app.get("/api/signals/{signal_id}")
def signal_detail(signal_id: int):
    with db.connect() as c:
        sg = c.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        if not sg:
            raise HTTPException(404, "Signal not found")
        sg = dict(sg)
        first = dict(c.execute("SELECT * FROM quotes WHERE id=?", (sg["quote_id"],)).fetchone())
        history = db.rows(c, """SELECT ts, consensus_p, consensus_src, n_books, ask, bid, fee, ev, roi
                                FROM quotes WHERE ticker=? AND side=? ORDER BY ts""", (sg["ticker"], sg["side"]))
        books = db.rows(c, """SELECT bookmaker, team, decimal_price, novig_p, book_last_update FROM odds_snapshots
                              WHERE run_id=? AND game_id=? ORDER BY bookmaker""", (first["run_id"], first["game_id"]))
        pos = c.execute("SELECT * FROM paper_positions WHERE ticker=? AND side=?",
                        (sg["ticker"], sg["side"])).fetchone()
        st = c.execute("SELECT ticker, result, fetched_at FROM settlements WHERE ticker=?", (sg["ticker"],)).fetchone()
    position = dict(pos) if pos else None
    if position and position.get("fills"):
        position["fills"] = json.loads(position["fills"])
    return {"signal": sg, "at_signal": first, "books_at_signal": books, "history": history,
            "position": position, "settlement": dict(st) if st else None}


@app.get("/api/paper/positions")
def positions(status: str = Query("all", pattern="^(open|settled|void|all)$"), league: str | None = None):
    sql, params = "SELECT * FROM paper_positions WHERE 1=1", []
    if status != "all":
        sql += " AND status=?"
        params.append(status)
    if league:
        sql += " AND league=?"
        params.append(league)
    with db.connect() as c:
        rows = db.rows(c, sql + " ORDER BY opened_at DESC", params)
        rej = db.rows(c, "SELECT reason, COUNT(*) AS n FROM paper_rejections GROUP BY reason ORDER BY n DESC")
    for r in rows:
        r["fills"] = json.loads(r["fills"]) if r["fills"] else []
    return {"positions": rows, "rejections": rej}


@app.get("/api/paper/performance")
def performance(league: str | None = None):
    with db.connect() as c:
        return analytics.performance(c, S, league)


@app.get("/api/analytics/thresholds")
def thresholds(league: str | None = None):
    with db.connect() as c:
        return {"thresholds": analytics.thresholds(c, S, league)}


@app.get("/api/history")
def history(ticker: str | None = None, side: str | None = None, league: str | None = None,
            limit: int = Query(200, le=5000)):
    """With a ticker: that contract's full quote history. Without: recent collection runs."""
    with db.connect() as c:
        if ticker:
            sql, params = "SELECT * FROM quotes WHERE ticker=?", [ticker]
            if side:
                sql += " AND side=?"
                params.append(side)
            q = db.rows(c, sql + " ORDER BY ts LIMIT ?", params + [limit])
            st = c.execute("SELECT result FROM settlements WHERE ticker=?", (ticker,)).fetchone()
            return {"ticker": ticker, "quotes": q, "result": st[0] if st else None}
        sql, params = "SELECT * FROM runs", []
        if league:
            sql += " WHERE league=?"
            params.append(league)
        return {"runs": db.rows(c, sql + " ORDER BY id DESC LIMIT ?", params + [limit])}
