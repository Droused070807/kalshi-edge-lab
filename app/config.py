"""Settings, read from environment variables or a .env file in the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Kalshi series ticker and The Odds API sport key for each league.
LEAGUES: dict[str, dict[str, str]] = {
    "nfl": {"kalshi": "KXNFLGAME", "odds": "americanfootball_nfl", "label": "NFL"},
    "nba": {"kalshi": "KXNBAGAME", "odds": "basketball_nba", "label": "NBA"},
    "mlb": {"kalshi": "KXMLBGAME", "odds": "baseball_mlb", "label": "MLB"},
    "nhl": {"kalshi": "KXNHLGAME", "odds": "icehockey_nhl", "label": "NHL"},
    "ncaaf": {"kalshi": "KXNCAAFGAME", "odds": "americanfootball_ncaaf", "label": "College football"},
}


def _f(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _i(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v in (None, "") else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    odds_api_key: str = field(default_factory=lambda: os.getenv("ODDS_API_KEY", "").strip())
    leagues: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip().lower() for x in os.getenv("LEAGUES", "nfl").split(",") if x.strip().lower() in LEAGUES
        )
    )
    collect_interval_min: int = field(default_factory=lambda: _i("COLLECT_INTERVAL_MIN", 180))
    collector_enabled: bool = field(default_factory=lambda: _b("COLLECTOR_ENABLED", True))

    odds_regions: str = field(default_factory=lambda: os.getenv("ODDS_REGIONS", "us,eu"))
    odds_min_credits: int = field(default_factory=lambda: _i("ODDS_MIN_CREDITS", 20))
    odds_max_age_min: float = field(default_factory=lambda: _f("ODDS_MAX_AGE_MIN", 30))
    min_books: int = field(default_factory=lambda: _i("MIN_BOOKS", 3))
    include_live: bool = field(default_factory=lambda: _b("INCLUDE_LIVE", False))

    kalshi_base: str = field(
        default_factory=lambda: os.getenv("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")
    )
    # Kalshi taker fee per order: ceil(rate * contracts * P * (1 - P)) to the cent.
    # 0.07 is the published general rate as of Aug 2026. Check kalshi.com/fee-schedule and update if it changes.
    taker_fee_rate: float = field(default_factory=lambda: _f("KALSHI_TAKER_FEE_RATE", 0.07))

    signal_min_edge: float = field(default_factory=lambda: _f("SIGNAL_MIN_EDGE", 0.02))
    paper_min_edge: float = field(default_factory=lambda: _f("PAPER_MIN_EDGE", 0.05))
    paper_start_bankroll: float = field(default_factory=lambda: _f("PAPER_START_BANKROLL", 10000))
    paper_max_position_pct: float = field(default_factory=lambda: _f("PAPER_MAX_POSITION_PCT", 0.01))
    paper_max_daily_loss_pct: float = field(default_factory=lambda: _f("PAPER_MAX_DAILY_LOSS_PCT", 0.03))

    wide_spread: float = field(default_factory=lambda: _f("WIDE_SPREAD", 0.04))
    early_hours: float = field(default_factory=lambda: _f("EARLY_HOURS", 24))
    thresholds: tuple[float, ...] = (0.02, 0.03, 0.05, 0.08)

    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "America/Chicago"))
    db_path: Path = field(default_factory=lambda: ROOT / os.getenv("DB_PATH", "data/edgelab.db"))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _i("PORT", 8000))


settings = Settings()
