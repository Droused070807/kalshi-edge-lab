"""Kalshi market -> sportsbook game matching. Ported from the browser scanner's teamMatches()."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..db import parse_iso

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def norm(s: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def team_matches(sub: str | None, full: str | None) -> bool:
    """Kalshi subtitles look like "Kansas City", "Chiefs", or "New York G" (city + nickname initial)."""
    s, f = norm(sub), norm(full)
    if not s:
        return False
    if f == s or s in f:
        return True
    t = s.split(" ")
    last = t[-1]
    if len(last) == 1 and len(t) > 1:
        city = " ".join(t[:-1])
        nick = f.split(" ")[-1] if f else ""
        return f.startswith(city) and nick.startswith(last)
    return bool(f) and f.split(" ")[-1] == last and len(t) == 1


def market_team_name(m: dict) -> str:
    return m.get("yes_sub_title") or m.get("title") or ""


def ticker_date(event_ticker: str | None) -> datetime | None:
    """Kalshi game tickers embed the date, e.g. KXNFLGAME-26OCT18KCLV -> 2026-10-18."""
    mt = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not mt or mt.group(2) not in _MONTHS:
        return None
    try:
        return datetime(2000 + int(mt.group(1)), _MONTHS[mt.group(2)], int(mt.group(3)), 12, tzinfo=timezone.utc)
    except ValueError:
        return None


def assign_team(m: dict, game: dict) -> str | None:
    """Which of the game's two teams this market names. None if it matches both or neither."""
    sub = market_team_name(m)
    h, a = team_matches(sub, game["home_team"]), team_matches(sub, game["away_team"])
    if h == a:
        return None
    return game["home_team"] if h else game["away_team"]


def match_events(markets: list[dict], games: list[dict]) -> list[tuple[str, list[dict], dict]]:
    """Group Kalshi markets by event and pair each event with one sportsbook game.

    Same rule as the scanner (every market in the event must name one of the game's teams),
    plus two safety additions: each market must map to a distinct team, and when a team
    appears in several upcoming games, the game closest to the ticker's date wins.
    """
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)

    out = []
    for ev, ms in by_event.items():
        candidates = []
        for g in games:
            teams = [assign_team(m, g) for m in ms]
            if all(teams) and len(set(teams)) == len(teams):
                candidates.append(g)
        if not candidates:
            continue
        if len(candidates) > 1:
            td = ticker_date(ev)
            if td is None:
                continue  # ambiguous and no date to break the tie: skip rather than guess
            candidates.sort(key=lambda g: abs((parse_iso(g["commence_time"]) - td).total_seconds()))
        out.append((ev, ms, candidates[0]))
    return out
