"""Configuration for the personal stock alert bot.

All secrets come from environment variables / a .env file. Nothing is
hardcoded here. Copy .env.example to .env and fill in the three values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional at import time
    pass


# ---- Engine-wide limits -----------------------------------------------------
MAX_SYMBOLS = 39          # hard cap on total subscribed symbols (user's ask)
ALPACA_MAX_SYMBOLS = 30   # Alpaca free Basic plan websocket cap (IEX feed)
FINNHUB_MAX_SYMBOLS = 50  # Finnhub free tier is generous; engine cap still wins

# ---- Market-data warm-up ----------------------------------------------------
WARMUP_1M_BARS = 100       # 1-minute bars fetched at startup per symbol
WARMUP_DAILY_BARS = 400    # daily bars fetched at startup (for 52-week hi/lo)

# ---- Alert engine defaults --------------------------------------------------
DEFAULT_COOLDOWN_MIN = 15.0
DEFAULT_HYSTERESIS_PCT = 0.005   # re-arm only after 0.5% move back past trigger
DEFAULT_NTFY_PRIORITY = 3        # ntfy priority 1..5 (5 = max)


@dataclass
class Settings:
    alpaca_key: str = ""
    alpaca_secret: str = ""
    finnhub_key: str = ""          # optional second free provider (overflow)
    marketaux_token: str = ""      # optional second news-sentiment source
    ntfy_topic: str = ""           # e.g. "stockbot-x7kq2-alerts"
    ntfy_server: str = "https://ntfy.sh"
    dashboard_token: str = ""   # optional token gate for the dashboard (?token= or Bearer)
    dashboard_host: str = "127.0.0.1"  # bind address; 0.0.0.0 on a standalone server
    port: int = 8000
    db_path: str = ""
    symbols: list[str] = field(default_factory=list)  # initial watchlist

    @classmethod
    def from_env(cls) -> "Settings":
        db_path = os.environ.get(
            "DB_PATH",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "stockbot.db"),
        )
        raw_symbols = os.environ.get("SYMBOLS", "")
        symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
        return cls(
            alpaca_key=os.environ.get("ALPACA_KEY", "").strip(),
            alpaca_secret=os.environ.get("ALPACA_SECRET", "").strip(),
            finnhub_key=os.environ.get("FINNHUB_KEY", "").strip(),
            marketaux_token=os.environ.get("MARKETAUX_API_TOKEN", "").strip(),
            ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip(),
            ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            dashboard_token=os.environ.get("DASHBOARD_TOKEN", "").strip(),
            dashboard_host=os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("PORT", "8000")),
            db_path=db_path,
            symbols=symbols,
        )

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty = good to go)."""
        problems: list[str] = []
        if not self.alpaca_key or not self.alpaca_secret:
            problems.append("ALPACA_KEY / ALPACA_SECRET missing — get free keys at https://alpaca.markets")
        if not self.ntfy_topic:
            problems.append(
                "NTFY_TOPIC missing — install the ntfy app (iOS/Android), "
                "subscribe to a hard-to-guess topic name, and put it in .env"
            )
        return problems
