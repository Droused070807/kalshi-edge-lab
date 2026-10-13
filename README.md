# Kalshi Edge Lab

A local research tool that compares Kalshi game-winner prices with the no-vig sportsbook consensus, logs every apparent edge, paper-trades the strongest ones against the real orderbook, and scores the results once games settle.

**Paper mode only.** There is no order-placement code and no Kalshi login. It reads public market data.

## Run it (Windows)

1. Install Python 3.11 or newer from python.org (tick "Add python.exe to PATH").
2. Double-click `run.bat`. The first run creates `.env` and opens it in Notepad.
3. Paste your key from [the-odds-api.com](https://the-odds-api.com) after `ODDS_API_KEY=`, save, and double-click `run.bat` again.
4. The dashboard opens at http://127.0.0.1:8000. Keep the window open; closing it stops collection.

macOS/Linux: `./run.sh`.

## How it works

```
Kalshi API ──┐
             ├─> adapters ─> event matching ─> consensus + EV ─> quotes (every contract, every run)
Odds API ────┘                                                    ├─> signals (first cross of 2pp)
                                                                  ├─> paper fills (5pp, real book depth)
                                                                  └─> settlement ─> P&L, Brier, CLV
```

- **Consensus:** Pinnacle's no-vig line when available, otherwise the median across books. Missing or stale lines produce no quote at all.
- **Net edge:** `book probability − Kalshi ask − taker fee`, in dollars per $1 contract (shown as percentage points).
- **Fees:** `ceil(rate × contracts × P × (1 − P))` per order, rounded up to the cent. `KALSHI_TAKER_FEE_RATE` defaults to 0.07; check Kalshi's fee schedule and update it if it changes.
- **Paper fills:** walks the live orderbook cheapest-first and stops where the edge after fees falls below the paper threshold, or where cost would exceed 1% of equity. One position per game; trading pauses after a 3% loss day.
- **Kelly** is calculated and displayed for reference only. Sizing is a flat bankroll percentage until the probabilities are validated.

## Storage

`data/edgelab.db` (SQLite). Snapshot tables are append-only:

| Table | Holds |
|---|---|
| `runs` | One row per collection run, with counts and API credits |
| `kalshi_snapshots` | Every Kalshi market quote, with the raw JSON |
| `odds_snapshots` | Every bookmaker price and its no-vig probability |
| `quotes` | Every priced contract side per run: consensus, ask, fee, edge, flags |
| `signals` | First time each contract side crossed `SIGNAL_MIN_EDGE` |
| `paper_positions` | Simulated fills, settlement and P&L |
| `paper_rejections` | Paper fills skipped and why |
| `settlements` | Final Kalshi results |

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Rules, schedule and recent runs |
| `POST /api/collect` | Run a collection cycle now |
| `GET /api/markets` | Latest priced contracts per league |
| `GET /api/signals` | Logged signals, newest first |
| `GET /api/signals/{id}` | Pricing breakdown, book prices and edge history |
| `GET /api/paper/positions` | Simulated positions |
| `GET /api/paper/performance` | Equity, ROI, drawdown, Brier, CLV |
| `GET /api/analytics/thresholds` | 2/3/5/8pp thresholds replayed on the same quotes |
| `GET /api/history` | Collection runs, or a contract's full quote history with `?ticker=` |

Interactive docs at http://127.0.0.1:8000/docs.

## Reading the results

- Judge it on **CLV** and the **threshold research** table, not win rate. Positive average CLV means you are getting better prices than the market settles at.
- **Brier books vs Brier Kalshi** tests the core idea directly: if the sportsbook consensus isn't better calibrated than Kalshi's own price, there's no edge to find.
- Don't draw conclusions before roughly 500 settled signals, and look at each sport separately.

## Credits budget

The free Odds API tier gives 500 credits a month. With `ODDS_REGIONS=us,eu` (needed for Pinnacle), each league costs 2 credits per run, so one league every 180 minutes uses about 480 a month. Track more leagues or run more often with a paid plan. The collector skips the odds call when Kalshi has no open markets for a league and stops when credits drop below `ODDS_MIN_CREDITS`.

## Tests

```
python -m pytest
```

The tests run a full collect → paper fill → settle → analytics cycle against mocked Kalshi and Odds API responses.
