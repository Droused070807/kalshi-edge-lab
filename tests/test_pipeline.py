"""Offline tests: engine math plus a full collect -> paper fill -> settle -> analytics cycle
against mocked Kalshi and Odds API responses."""
import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import analytics, db
from app.adapters.kalshi import KalshiClient
from app.adapters.odds import OddsClient
from app.collector import collect_league, settle
from app.config import Settings
from app.engine.consensus import consensus
from app.engine.ev import asks_for, evaluate, order_fee, parse_orderbook, simulate_fill
from app.engine.matching import match_events, team_matches

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- engine ----------

def test_team_matching():
    assert team_matches("Kansas City", "Kansas City Chiefs")
    assert team_matches("New York G", "New York Giants")
    assert not team_matches("New York G", "New York Jets")
    assert team_matches("Chiefs", "Kansas City Chiefs")
    assert not team_matches("", "Kansas City Chiefs")


def test_match_picks_game_nearest_ticker_date():
    g1 = {"id": "a", "home_team": "Kansas City Chiefs", "away_team": "Las Vegas Raiders",
          "commence_time": "2026-10-18T17:00:00Z"}
    g2 = {"id": "b", "home_team": "Las Vegas Raiders", "away_team": "Kansas City Chiefs",
          "commence_time": "2026-11-29T17:00:00Z"}
    ms = [{"ticker": "T-KC", "event_ticker": "KXNFLGAME-26NOV29KCLV", "yes_sub_title": "Kansas City"},
          {"ticker": "T-LV", "event_ticker": "KXNFLGAME-26NOV29KCLV", "yes_sub_title": "Las Vegas"}]
    (_, _, game), = match_events(ms, [g1, g2])
    assert game["id"] == "b"


def test_consensus_prefers_pinnacle_and_rejects_stale():
    fresh, stale = z(NOW), z(NOW - timedelta(hours=2))
    game = {"bookmakers": [
        {"key": "draftkings", "last_update": fresh, "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": 1.80}, {"name": "B", "price": 2.10}]}]},
        {"key": "pinnacle", "last_update": fresh, "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": 1.70}, {"name": "B", "price": 2.30}]}]},
    ]}
    c = consensus(game, NOW, 30, 3)
    assert c["src"] == "Pinnacle"
    assert c["p"]["A"] == pytest.approx((1 / 1.7) / (1 / 1.7 + 1 / 2.3))
    for b in game["bookmakers"]:
        b["last_update"] = stale
    assert consensus(game, NOW, 30, 3) is None  # stale lines -> no pick


def test_fees_and_ev():
    assert order_fee([(0.56, 1)], 0.07) == 0.02          # 1.72c rounds up to 2c
    assert order_fee([(0.50, 100)], 0.07) == 1.75
    e = evaluate(0.62, 0.57, 0.07)
    assert e["ev"] == pytest.approx(0.62 - 0.57 - 0.07 * 0.57 * 0.43)


def test_orderbook_walk_respects_edge_and_budget():
    book = parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.40", "500"]],
                                             "no_dollars": [["0.45", "10"], ["0.44", "20"], ["0.38", "50"]]}})
    asks = asks_for(book, "yes")
    assert asks[0] == (0.55, 10) and asks[1] == (0.56, 20)
    fill = simulate_fill(asks, p=0.63, min_edge=0.05, max_cost=100, rate=0.07)
    # 0.55 and 0.56 keep >=5pp after fee, 0.62 does not
    assert fill["contracts"] == 30 and all(p <= 0.56 for p, _ in fill["fills"])
    small = simulate_fill(asks, p=0.63, min_edge=0.05, max_cost=3.0, rate=0.07)
    assert small["cost"] <= 3.0 and small["contracts"] == 5


# ---------- full cycle ----------

def make_settings(tmp_path):
    return dataclasses.replace(Settings(), odds_api_key="test", leagues=("nfl",), db_path=tmp_path / "t.db",
                               min_books=1, odds_min_credits=0)


def fake_transport(state):
    start = NOW + timedelta(hours=40)

    def handler(req: httpx.Request):
        url = str(req.url)
        if "the-odds-api" in url:
            return httpx.Response(200, headers={"x-requests-remaining": "480"}, json=[{
                "id": "g1", "home_team": "Kansas City Chiefs", "away_team": "Las Vegas Raiders",
                "commence_time": z(start), "bookmakers": [{
                    "key": "pinnacle", "last_update": z(NOW), "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Kansas City Chiefs", "price": 1.55},
                        {"name": "Las Vegas Raiders", "price": 2.70}]}]}]}])
        if url.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.40", "100"]],
                                                              "no_dollars": [["0.44", "300"]]}})
        if "/markets/" in url:
            t = url.rsplit("/", 1)[-1]
            res = state.get(t)
            return httpx.Response(200, json={"market": {"ticker": t, "status": "finalized" if res else "open",
                                                        "result": res or ""}})
        if url.split("?")[0].endswith("/markets"):
            ev = "KXNFLGAME-" + start.strftime("%y%b%d").upper() + "LVKC"
            return httpx.Response(200, json={"cursor": "", "markets": [
                {"ticker": ev + "-KC", "event_ticker": ev, "yes_sub_title": "Kansas City", "status": "active",
                 "yes_bid_dollars": "0.54", "yes_ask_dollars": "0.56", "no_bid_dollars": "0.44",
                 "no_ask_dollars": "0.46"},
                {"ticker": ev + "-LV", "event_ticker": ev, "yes_sub_title": "Las Vegas", "status": "active",
                 "yes_bid_dollars": "0.43", "yes_ask_dollars": "0.45", "no_bid_dollars": "0.55",
                 "no_ask_dollars": "0.57"}]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_full_cycle(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    db.init(s.db_path)
    state: dict = {}

    async def go():
        async with httpx.AsyncClient(transport=fake_transport(state)) as http:
            k, o = KalshiClient(s.kalshi_base, http), OddsClient("test", "us,eu", http)
            r1 = await collect_league("nfl", s, k, o)
            r2 = await collect_league("nfl", s, k, o)  # second run: no duplicate signals/positions
            return r1, r2, k
    r1, r2, k = asyncio.run(go())
    assert r1["status"] == "ok" and r1["matched_games"] == 1 and r1["quotes"] == 4
    assert r1["new_signals"] >= 1 and r1["new_positions"] == 1
    assert r2["new_signals"] == 0 and r2["new_positions"] == 0

    with db.connect() as c:
        pos = db.rows(c, "SELECT * FROM paper_positions")[0]
        assert pos["side"] == "yes" and pos["backs_team"] == "Kansas City Chiefs"
        assert pos["cost"] <= 100.0  # 1% of $10,000

    # jump the clock past the game so settlement picks it up (history itself is never edited)
    import app.collector as col
    monkeypatch.setattr(col, "utcnow", lambda: NOW + timedelta(days=3))
    state[pos["ticker"]] = "yes"
    state[pos["ticker"].replace("-KC", "-LV")] = "no"

    async def do_settle():
        async with httpx.AsyncClient(transport=fake_transport(state)) as http:
            return await settle(s, KalshiClient(s.kalshi_base, http))
    assert asyncio.run(do_settle()) >= 1

    with db.connect() as c:
        p = db.rows(c, "SELECT * FROM paper_positions")[0]
        assert p["status"] == "settled" and p["pnl"] == pytest.approx(p["contracts"] - p["cost"])
        perf = analytics.performance(c, s)
        assert perf["settled_positions"] == 1 and perf["realized_pnl"] > 0
        th = analytics.thresholds(c, s)
        assert th[0]["threshold"] == 0.02 and th[0]["settled"] >= 1
        assert c.execute("SELECT COUNT(*) FROM kalshi_snapshots").fetchone()[0] == 4  # append-only, 2 runs
