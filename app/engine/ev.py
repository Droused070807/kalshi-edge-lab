"""Fees, expected value, and realistic fills against the Kalshi orderbook."""
from __future__ import annotations

import math


def fee_per_contract(price: float, rate: float) -> float:
    """Unrounded taker fee for one contract. Used for screening quotes (same as the scanner)."""
    return rate * price * (1 - price)


def order_fee(fills: list[tuple[float, int]], rate: float) -> float:
    """Kalshi rounds the taker fee up to the next cent per order."""
    raw = sum(rate * q * p * (1 - p) for p, q in fills)
    return math.ceil(round(raw * 100, 6)) / 100


def evaluate(p: float, ask: float, rate: float) -> dict:
    f = fee_per_contract(ask, rate)
    cost = ask + f
    ev = p - cost
    return {
        "fee": f,
        "ev": ev,
        "roi": ev / cost,
        # Kelly fraction for a $1 binary bought at `cost`. Informational only until probabilities are validated.
        "kelly_info": max((p - cost) / (1 - cost), 0.0) if cost < 1 else 0.0,
    }


def parse_orderbook(j: dict) -> dict[str, list[tuple[float, int]]]:
    """Kalshi books list resting bids for YES and NO. Handles dollar and cent formats."""
    ob = j.get("orderbook_fp") or j.get("orderbook") or {}
    out: dict[str, list[tuple[float, int]]] = {"yes": [], "no": []}
    for side in ("yes", "no"):
        lv = ob.get(f"{side}_dollars")
        if lv:
            out[side] = [(float(p), int(float(q))) for p, q in lv]
            continue
        lv = ob.get(side) or []
        out[side] = [(float(p) / 100, int(float(q))) for p, q in lv]
    return out


def asks_for(book: dict[str, list[tuple[float, int]]], side: str) -> list[tuple[float, int]]:
    """Buying YES at price X fills against a NO bid at 1 - X, and vice versa. Cheapest first."""
    other = "no" if side == "yes" else "yes"
    asks = [(round(1 - p, 4), q) for p, q in book[other] if q > 0 and 0 < p < 1]
    return sorted(asks)


def simulate_fill(asks: list[tuple[float, int]], p: float, min_edge: float,
                  max_cost: float, rate: float) -> dict:
    """Walk the book cheapest-first. Stop at the first level where the edge after fees drops
    below min_edge, or when the next contract would push total cost (with the rounded order
    fee) over max_cost."""
    fills: list[tuple[float, int]] = []
    for price, qty in asks:
        if p - price - fee_per_contract(price, rate) < min_edge:
            break
        take = 0
        for _ in range(qty):
            trial = fills + [(price, take + 1)]
            cost = sum(pp * qq for pp, qq in trial) + order_fee(trial, rate)
            if cost > max_cost + 1e-9:
                break
            take += 1
        if take:
            fills.append((price, take))
        if take < qty:
            break
    contracts = sum(q for _, q in fills)
    if not contracts:
        return {"contracts": 0, "fills": [], "avg_price": None, "fee": 0.0, "cost": 0.0}
    notional = sum(pp * qq for pp, qq in fills)
    fee = order_fee(fills, rate)
    return {"contracts": contracts, "fills": fills, "avg_price": notional / contracts,
            "fee": fee, "cost": round(notional + fee, 4)}
