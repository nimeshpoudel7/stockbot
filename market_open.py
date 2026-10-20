"""Market-open bell: one ntfy push each trading day at the open.

Run by the `stockbot-market-open` cron (daily ~09:30 America/Toronto).
Exits quietly on weekends/holidays via the sessions.py calendar, so the
daily schedule needs no calendar logic of its own.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional, mirrors config.py
    pass

import sessions as sessions_mod
from config import Settings
from notify import send_ntfy
from storage import Store

log = logging.getLogger("stockbot.market_open")
ET = ZoneInfo("America/Toronto")


def is_trading_day_today(store: Store) -> bool:
    today = dt.datetime.now(ET).date().isoformat()
    is_open = sessions_mod.is_trading_day(store, today)
    if is_open is None:
        sessions_mod.refresh_cache(store)
        is_open = sessions_mod.is_trading_day(store, today)
    return is_open is True


def universe_size(store: Store) -> int | None:
    """Best-effort read of the cached scanner universe size."""
    try:
        raw = store.poller_get("scanner_universe_v2")
        if raw:
            return len(json.loads(raw).get("symbols", []))
    except Exception:  # noqa: BLE001 - advisory only
        log.exception("universe size lookup failed")
    return None


def get_regime(store: Store) -> str | None:
    """Regime from the bot's cached SPY daily bars (no API call)."""
    try:
        import regime as regime_mod

        rc = regime_mod.RegimeCache(store, lambda _s, _d: [])
        return rc.current()
    except Exception:  # noqa: BLE001 - advisory only
        log.exception("regime lookup failed")
        return None


def build_message(universe: int | None, regime: str | None) -> tuple[str, str]:
    title = "Market open"
    scope = f"{universe:,} stocks" if universe else "the full market"
    lines = [
        f"Scanner watching {scope} — BUY/SELL setup alerts land here in real time."
    ]
    if regime:
        lines.append(f"Market regime: {regime}.")
    return title, "\n".join(lines)


def main() -> int:
    settings = Settings.from_env()
    store = Store(settings.db_path)
    if not is_trading_day_today(store):
        log.info("market_open: not a trading day, staying quiet")
        return 0
    title, message = build_message(universe_size(store), get_regime(store))
    ok = send_ntfy(settings.ntfy_server, settings.ntfy_topic, title, message,
                   priority=3, tags=["bell"])
    log.info("market_open: pushed=%s", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
