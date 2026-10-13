"""Read-only Kalshi public API client. No authentication, no order placement."""
from __future__ import annotations

import asyncio

import httpx


def _price(m: dict, side: str, kind: str) -> float | None:
    for key, scale in ((f"{side}_{kind}_dollars", 1.0), (f"{side}_{kind}", 0.01)):
        v = m.get(key)
        if v not in (None, ""):
            try:
                return float(v) * scale
            except (TypeError, ValueError):
                pass
    return None


def normalize(m: dict) -> dict:
    vol = m.get("volume_fp", m.get("volume"))
    return {
        "ticker": m["ticker"],
        "event_ticker": m.get("event_ticker"),
        "sub_title": m.get("yes_sub_title") or m.get("title"),
        "status": m.get("status"),
        "yes_bid": _price(m, "yes", "bid"), "yes_ask": _price(m, "yes", "ask"),
        "no_bid": _price(m, "no", "bid"), "no_ask": _price(m, "no", "ask"),
        "last_price": _price(m, "last", "price") if m.get("last_price_dollars") is None
        else float(m["last_price_dollars"]),
        "volume": float(vol) if vol not in (None, "") else None,
        "close_time": m.get("close_time"),
        "result": m.get("result") or None,
    }


class KalshiClient:
    def __init__(self, base: str, client: httpx.AsyncClient):
        self.base = base
        self.http = client
        self._sem = asyncio.Semaphore(5)  # stay well under the public rate limit

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with self._sem:
            for attempt in range(4):
                r = await self.http.get(f"{self.base}{path}", params=params)
                if r.status_code == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            r.raise_for_status()
            return {}

    async def open_markets(self, series: str) -> list[dict]:
        out, cursor = [], ""
        for _ in range(10):
            params = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            j = await self._get("/markets", params)
            out += j.get("markets") or []
            cursor = j.get("cursor")
            if not cursor:
                break
        return out

    async def market(self, ticker: str) -> dict:
        return (await self._get(f"/markets/{ticker}")).get("market") or {}

    async def orderbook(self, ticker: str) -> dict:
        return await self._get(f"/markets/{ticker}/orderbook")
