"""No-vig sportsbook consensus. Ported from the scanner's consensus(), plus staleness and min-book checks."""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from ..db import parse_iso


def book_probs(game: dict) -> list[dict]:
    """Per-book no-vig probabilities for a two-way moneyline. Draw outcomes are dropped, as in the scanner."""
    out = []
    for bk in game.get("bookmakers") or []:
        mk = next((m for m in bk.get("markets") or [] if m.get("key") == "h2h"), None)
        if not mk:
            continue
        outs = [o for o in mk.get("outcomes") or [] if o.get("name") != "Draw"]
        if len(outs) != 2 or any(not o.get("price") or o["price"] <= 1 for o in outs):
            continue
        imp = [1 / o["price"] for o in outs]
        tot = sum(imp)
        out.append({
            "key": bk.get("key"),
            "last_update": bk.get("last_update"),
            "prices": {o["name"]: o["price"] for o in outs},
            "p": {o["name"]: imp[i] / tot for i, o in enumerate(outs)},
        })
    return out


def consensus(game: dict, now: datetime, max_age_min: float, min_books: int) -> dict | None:
    """Pinnacle when listed and fresh, otherwise the median of fresh books.

    Returns None (no pick) when lines are missing or stale, never a guess.
    """
    books = book_probs(game)
    cutoff = now - timedelta(minutes=max_age_min)
    fresh = [b for b in books if (lu := parse_iso(b["last_update"])) is None or lu >= cutoff]
    if not fresh:
        return None
    pin = next((b for b in fresh if b["key"] == "pinnacle"), None)
    if pin:
        return {"p": pin["p"], "src": "Pinnacle", "n_books": len(fresh)}
    if len(fresh) < min_books:
        return None
    names: dict[str, list[float]] = {}
    for b in fresh:
        for n, p in b["p"].items():
            names.setdefault(n, []).append(p)
    p = {n: median(v) for n, v in names.items()}
    return {"p": p, "src": f"Median of {len(fresh)} books", "n_books": len(fresh)}
