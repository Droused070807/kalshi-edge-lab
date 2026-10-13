"""Audit tests: no real-money path, cash accounting, fee rounding, settlement P&L, append-only history."""
import asyncio
import dataclasses
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app import analytics, db, paper
from app.adapters.kalshi import KalshiClient
from app.adapters.odds import OddsClient
from app.collector import collect_league, settle
from app.config import Settings
from app.engine.ev import order_fee

from .test_pipeline import NOW, fake_transport, z

APP = Path(__file__).resolve().parent.parent / "app"


def settings(tmp_path, **kw):
    return dataclasses.replace(Settings(), odds_api_key="test", leagues=("nfl",), db_path=tmp_path / "a.db",
                               min_books=1, odds_min_credits=0, **kw)


def run_cycle(s, state=None, guard=None):
    inner = fake_transport(state or {})

    def handler(req):
        if guard is not None:
            guard.append((req.method, req.url.host, req.url.path))
        return inner.handle_request(req)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as h:
            k = KalshiClient(s.kalshi_base, h)
            r = await collect_league("nfl", s, k, OddsClient("test", "us,eu", h))
            await settle(s, k)
            return r
    return asyncio.run(go())


# ---------- no real-money path ----------

def test_every_outbound_request_is_a_read(tmp_path, monkeypatch):
    s = settings(tmp_path)
    db.init(s.db_path)
    seen: list = []
    run_cycle(s, guard=seen)
    import app.collector as col
    monkeypatch.setattr(col, "utcnow", lambda: NOW + timedelta(days=3))
    with db.connect() as c:
        t = c.execute("SELECT ticker FROM paper_positions").fetchone()[0]
    run_cycle(s, state={t: "yes"}, guard=seen)
    assert seen, "expected HTTP traffic"
    assert {m for m, _, _ in seen} == {"GET"}
    kalshi_paths = {p for _, host, p in seen if "kalshi" in host}
    assert not any("portfolio" in p or "orders" in p for p in kalshi_paths)


def test_source_has_no_order_or_auth_code():
    # Everything except main.py, whose one @app.post is the dashboard's local "Collect now" button.
    files = [p for p in APP.rglob("*.py") if p.name != "main.py"]
    src = "\n".join(p.read_text() for p in files)
    main = (APP / "main.py").read_text()
    assert re.findall(r"@app\.(post|put|delete|patch)\(\"([^\"]+)", main) == [("post", "/api/collect")]
    assert "KALSHI-ACCESS" not in main and "orders" not in main.replace("orderbook", "")
    for pattern in [r"\.post\(", r"\.put\(", r"\.delete\(", r"portfolio/orders", r"KALSHI-ACCESS",
                    r"private_key", r"create_order", r"place_order"]:
        assert not re.search(pattern, src), f"found {pattern!r} in app code"


# ---------- cash accounting ----------

def _open_position(c, i, cost):
    c.execute("INSERT INTO runs(id, league, started_at, status) VALUES (?, 'nfl', ?, 'ok')", (i, z(NOW)))
    c.execute("""INSERT INTO quotes(id, run_id, ts, league, ticker, side, game_id, team, backs_team, commence_time,
                 consensus_p, ask, fee, ev, roi) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (i, i, z(NOW), "nfl", f"T{i}", "yes", f"G{i}", "A", "A", z(NOW + timedelta(days=1)),
               0.6, 0.5, 0.0175, 0.08, 0.15))
    c.execute("""INSERT INTO paper_positions(quote_id, opened_at, league, ticker, side, game_id, backs_team,
                 commence_time, entry_p, contracts, avg_price, fee, cost, status)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
              (i, z(NOW), "nfl", f"T{i}", "yes", f"G{i}", "A", z(NOW + timedelta(days=1)), 0.6,
               int(cost / 0.5), 0.5, 0.0, cost))


def test_open_positions_reduce_available_cash(tmp_path):
    s = settings(tmp_path, paper_start_bankroll=1000)
    db.init(s.db_path)
    with db.connect() as c:
        for i in range(1, 4):
            _open_position(c, i, 300)
        assert paper.equity(c, s) == 1000
        assert paper.available_cash(c, s) == pytest.approx(100)
        assert analytics.performance(c, s)["cash"] == pytest.approx(100)


def test_fill_never_exceeds_uncommitted_cash(tmp_path):
    # $1,000 bankroll with $995 already committed: the 1% cap ($10) is larger than the $5 left
    s = settings(tmp_path, paper_start_bankroll=1000)
    db.init(s.db_path)
    with db.connect() as c:
        _open_position(c, 900, 995)
    run_cycle(s)
    with db.connect() as c:
        new = db.rows(c, "SELECT * FROM paper_positions WHERE ticker != 'T900'")
        assert len(new) == 1 and new[0]["cost"] <= 5.0 + 1e-9
        assert paper.available_cash(c, s) >= -1e-9


def test_no_fill_when_cash_is_exhausted(tmp_path):
    s = settings(tmp_path, paper_start_bankroll=1000)
    db.init(s.db_path)
    with db.connect() as c:
        _open_position(c, 900, 999.5)
    run_cycle(s)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 1
        reasons = [r[0] for r in c.execute("SELECT reason FROM paper_rejections")]
        assert "not enough uncommitted cash" in reasons


# ---------- fees ----------

@pytest.mark.parametrize("fills,expected", [
    ([(0.50, 4)], 0.07),                      # exactly 7.00c: no round-up from float noise
    ([(0.50, 100)], 1.75),
    ([(0.56, 1)], 0.02),                      # 1.72c -> 2c
    ([(0.90, 1)], 0.01),                      # 0.63c -> 1c
    ([(0.55, 10), (0.56, 20)], 0.52),         # 51.8c across two price levels -> 52c, one order
    ([(0.99, 1)], 0.01),
])
def test_order_fee_rounding(fills, expected):
    assert order_fee(fills, 0.07) == pytest.approx(expected)


# ---------- settlement ----------

def _settle_with(tmp_path, monkeypatch, result_for_kc):
    s = settings(tmp_path)
    db.init(s.db_path)
    run_cycle(s)
    import app.collector as col
    monkeypatch.setattr(col, "utcnow", lambda: NOW + timedelta(days=3))
    with db.connect() as c:
        t = c.execute("SELECT ticker FROM paper_positions").fetchone()[0]
    run_cycle(s, state={t: result_for_kc, t.replace("-KC", "-LV"): {"yes": "no", "no": "yes"}.get(result_for_kc, "void")})
    with db.connect() as c:
        return db.rows(c, "SELECT * FROM paper_positions WHERE ticker=?", (t,))[0], s


def test_settlement_win(tmp_path, monkeypatch):
    p, _ = _settle_with(tmp_path, monkeypatch, "yes")
    assert p["status"] == "settled" and p["payout"] == p["contracts"]
    assert p["pnl"] == pytest.approx(p["contracts"] - p["cost"])


def test_settlement_loss(tmp_path, monkeypatch):
    p, _ = _settle_with(tmp_path, monkeypatch, "no")
    assert p["status"] == "settled" and p["payout"] == 0 and p["pnl"] == pytest.approx(-p["cost"])


def test_settlement_void_refunds_cost(tmp_path, monkeypatch):
    p, s = _settle_with(tmp_path, monkeypatch, "void")
    assert p["status"] == "void" and p["pnl"] == 0
    with db.connect() as c:
        assert paper.available_cash(c, s) == pytest.approx(s.paper_start_bankroll)


def test_settlement_is_idempotent(tmp_path, monkeypatch):
    p, s = _settle_with(tmp_path, monkeypatch, "yes")
    run_cycle(s, state={p["ticker"]: "yes"})
    with db.connect() as c:
        again = db.rows(c, "SELECT * FROM paper_positions WHERE id=?", (p["id"],))[0]
        assert again["pnl"] == p["pnl"] and again["settled_at"] == p["settled_at"]
        assert c.execute("SELECT COUNT(*) FROM settlements WHERE ticker=?", (p["ticker"],)).fetchone()[0] == 1


# ---------- append-only history ----------

def test_history_is_append_only(tmp_path):
    s = settings(tmp_path)
    db.init(s.db_path)
    run_cycle(s)
    with db.connect() as c:
        before = db.rows(c, "SELECT * FROM quotes ORDER BY id")
    run_cycle(s)
    with db.connect() as c:
        after = db.rows(c, "SELECT * FROM quotes ORDER BY id")
        assert after[:len(before)] == before and len(after) == 2 * len(before)
        for sql in ["UPDATE quotes SET ask=0.01", "DELETE FROM quotes", "UPDATE kalshi_snapshots SET yes_ask=0",
                    "DELETE FROM odds_snapshots", "UPDATE paper_positions SET cost=0", "DELETE FROM signals"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)


# ---------- marked-to-market ----------

def test_mark_to_market_uses_latest_bid_but_sizing_does_not(tmp_path):
    s = settings(tmp_path)
    db.init(s.db_path)
    run_cycle(s)
    with db.connect() as c:
        p = db.rows(c, "SELECT * FROM paper_positions")[0]
        perf = analytics.performance(c, s)
        expected = p["contracts"] * 0.54 - p["cost"]  # fake book: KC yes_bid 54c
        assert perf["unrealized_pnl"] == pytest.approx(expected, abs=0.01)
        assert perf["mtm_equity"] == pytest.approx(s.paper_start_bankroll + expected, abs=0.01)
        assert paper.equity(c, s) == s.paper_start_bankroll  # sizing ignores unrealized P&L
