"""Performance, calibration and threshold analysis."""
from __future__ import annotations

import sqlite3

from . import db
from .config import Settings

# The last quote for the same contract side at or before game start: the "closing line".
CLOSE_SQL = """(SELECT {col} FROM quotes c WHERE c.ticker = {t}.ticker AND c.side = {t}.side
                AND c.ts <= {t}.commence_time ORDER BY c.ts DESC LIMIT 1)"""


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _brier(pairs):
    pairs = [(p, o) for p, o in pairs if p is not None]
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def performance(conn: sqlite3.Connection, s: Settings, league: str | None = None) -> dict:
    where, params = ("AND p.league = ?", [league]) if league else ("", [])
    pos = db.rows(conn, f"""
        SELECT p.*, {CLOSE_SQL.format(col='consensus_p', t='p')} AS close_p,
                    {CLOSE_SQL.format(col='ask', t='p')} AS close_ask
        FROM paper_positions p WHERE 1=1 {where} ORDER BY p.opened_at""", params)
    settled = [p for p in pos if p["status"] == "settled"]
    settled.sort(key=lambda p: p["settled_at"])
    open_ = [p for p in pos if p["status"] == "open"]

    eq, peak, max_dd, curve = s.paper_start_bankroll, s.paper_start_bankroll, 0.0, []
    curve.append({"t": settled[0]["opened_at"] if settled else None, "equity": eq})
    for p in settled:
        eq += p["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak else 0)
        curve.append({"t": p["settled_at"], "equity": round(eq, 2)})

    # Mark open positions at the latest stored Kalshi bid for that side: the price you could sell at now.
    marks, mark_ts = [], []
    for p in open_:
        r = conn.execute(f"""SELECT {p['side']}_bid, ts FROM kalshi_snapshots WHERE ticker=? AND {p['side']}_bid IS NOT NULL
                             ORDER BY ts DESC LIMIT 1""", (p["ticker"],)).fetchone()
        if r:
            marks.append(p["contracts"] * r[0] - p["cost"])
            mark_ts.append(r[1])
    unrealized = sum(marks) if open_ else 0.0

    staked = sum(p["cost"] for p in settled)
    pnl = sum(p["pnl"] for p in settled)
    wins = sum(1 for p in settled if p["side"] == p["result"])
    return {
        "start_bankroll": s.paper_start_bankroll,
        "equity": round(s.paper_start_bankroll + pnl, 2),
        "realized_pnl": round(pnl, 2),
        "open_positions": len(open_),
        "open_exposure": round(sum(p["cost"] for p in open_), 2),
        # Sizing stays on conservative equity (start + realized). These are for reporting only.
        "unrealized_pnl": round(unrealized, 2),
        "mtm_equity": round(s.paper_start_bankroll + pnl + unrealized, 2),
        "marked_positions": len(marks),
        "marks_as_of": min(mark_ts) if mark_ts else None,
        "cash": round(s.paper_start_bankroll + pnl - sum(p["cost"] for p in open_), 2),
        "settled_positions": len(settled),
        "win_rate": wins / len(settled) if settled else None,
        "roi_on_staked": pnl / staked if staked else None,
        "max_drawdown": max_dd,
        "brier": _brier([(p["entry_p"], 1.0 if p["side"] == p["result"] else 0.0) for p in settled]),
        "avg_clv_consensus": _avg([p["close_p"] - p["avg_price"] for p in settled if p["close_p"] is not None]),
        "avg_kalshi_move": _avg([p["close_ask"] - p["avg_price"] for p in settled if p["close_ask"] is not None]),
        "min_settled_target": 500,
        "equity_curve": curve,
    }


def thresholds(conn: sqlite3.Connection, s: Settings, league: str | None = None) -> list[dict]:
    """For each threshold, take the FIRST quote per contract side that reached it, from the same
    stored observations, and score it at flat one-contract stakes. No paper-fill selection involved."""
    out = []
    for t in s.thresholds:
        where, params = ("AND league = ?", [t, league]) if league else ("", [t])
        rows = db.rows(conn, f"""
            WITH first AS (
                SELECT q.*, ROW_NUMBER() OVER (PARTITION BY ticker, side ORDER BY ts) AS rn
                FROM quotes q WHERE ev >= ? {where}
            )
            SELECT f.*, st.result,
                   {CLOSE_SQL.format(col='consensus_p', t='f')} AS close_p
            FROM first f LEFT JOIN settlements st ON st.ticker = f.ticker
            WHERE f.rn = 1""", params)
        done = [r for r in rows if r["result"] in ("yes", "no")]
        outcome = lambda r: 1.0 if r["side"] == r["result"] else 0.0  # noqa: E731
        staked = sum(r["ask"] + r["fee"] for r in done)
        pnl = sum(outcome(r) - r["ask"] - r["fee"] for r in done)
        out.append({
            "threshold": t,
            "signals": len(rows),
            "games": len({r["game_id"] for r in rows}),
            "settled": len(done),
            "win_rate": _avg([outcome(r) for r in done]),
            "avg_predicted_p": _avg([r["consensus_p"] for r in done]),
            "avg_ev": _avg([r["ev"] for r in rows]),
            "flat_roi": pnl / staked if staked else None,
            "brier_books": _brier([(r["consensus_p"], outcome(r)) for r in done]),
            "brier_kalshi": _brier([(r["ask"] if r["bid"] is None else (r["ask"] + r["bid"]) / 2, outcome(r))
                                    for r in done]),
            "avg_clv": _avg([r["close_p"] - r["ask"] for r in done if r["close_p"] is not None]),
            "early_share": _avg([r["early"] for r in rows]),
        })
    return out
