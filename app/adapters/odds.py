"""The Odds API client (the-odds-api.com). Each call costs credits: 1 per region per market."""
from __future__ import annotations

import httpx


class OddsError(Exception):
    pass


class OddsClient:
    BASE = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str, regions: str, client: httpx.AsyncClient):
        self.key = api_key
        self.regions = regions
        self.http = client

    async def h2h(self, sport: str) -> tuple[list[dict], int | None]:
        if not self.key:
            raise OddsError("ODDS_API_KEY is not set in .env")
        r = await self.http.get(f"{self.BASE}/sports/{sport}/odds", params={
            "apiKey": self.key, "regions": self.regions, "markets": "h2h", "oddsFormat": "decimal",
        })
        left = r.headers.get("x-requests-remaining")
        credits = int(float(left)) if left not in (None, "") else None
        if r.status_code == 401:
            raise OddsError("The Odds API rejected the key in .env")
        if r.status_code == 422:
            return [], credits  # sport out of season
        if r.status_code == 429:
            raise OddsError("The Odds API quota is used up for this period")
        r.raise_for_status()
        return r.json(), credits
