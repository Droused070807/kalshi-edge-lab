"""Turn raw Kalshi markets + sportsbook games into priced quotes. Ported from the scanner's buildRows()."""
from __future__ import annotations

from datetime import datetime

from ..adapters.kalshi import normalize
from ..config import Settings
from ..db import parse_iso
from .consensus import consensus
from .ev import evaluate
from .matching import assign_team, match_events


def build_quotes(markets: list[dict], games: list[dict], now: datetime, s: Settings) -> tuple[list[dict], int]:
    quotes: list[dict] = []
    matched = 0
    for ev_ticker, ms, game in match_events(markets, games):
        start = parse_iso(game["commence_time"])
        hours = (start - now).total_seconds() / 3600
        if hours <= 0 and not s.include_live:
            continue
        cons = consensus(game, now, s.odds_max_age_min, s.min_books)
        if not cons:
            continue
        matched += 1
        for raw in ms:
            m = normalize(raw)
            team = assign_team(raw, game)
            p_team = cons["p"].get(team)
            if p_team is None:
                continue
            other = game["away_team"] if team == game["home_team"] else game["home_team"]
            for side in ("yes", "no"):
                ask, bid = m[f"{side}_ask"], m[f"{side}_bid"]
                if ask is None or ask <= 0.01 or ask >= 0.99:
                    continue
                p = p_team if side == "yes" else 1 - p_team
                e = evaluate(p, ask, s.taker_fee_rate)
                spread = (ask - bid) if bid is not None else None
                quotes.append({
                    "ticker": m["ticker"], "event_ticker": ev_ticker, "side": side,
                    "game_id": game["id"], "home_team": game["home_team"], "away_team": game["away_team"],
                    "team": team, "backs_team": team if side == "yes" else other,
                    "commence_time": game["commence_time"], "hours_to_start": round(hours, 3),
                    "consensus_p": p, "consensus_src": cons["src"], "n_books": cons["n_books"],
                    "ask": ask, "bid": bid, "spread": spread,
                    "fee": e["fee"], "ev": e["ev"], "roi": e["roi"], "kelly_info": e["kelly_info"],
                    "early": int(hours > s.early_hours),
                    "wide": int(spread is not None and spread > s.wide_spread),
                })
    quotes.sort(key=lambda q: q["roi"], reverse=True)
    return quotes, matched
