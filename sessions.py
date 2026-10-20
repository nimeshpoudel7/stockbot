"""US market session calendar via Alpaca /v2/calendar (free, no rate-limit worry).

The engine uses this so daily-bar logic (bollinger_squeeze, sell_the_news,
regime) never misfires on holidays/half-days: a "next session" is always a
real trading day, and the bot logs the session state at boot.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

import requests

log = logging.getLogger("stockbot.sessions")

ALPACA_BASE = "https://paper-api.alpaca.markets"


def fetch_calendar(start: dt.date, end: dt.date) -> list[dict]:
    """Pull calendar days from Alpaca. Returns [{date, is_open, open, close}].

    NOTE: /v2/calendar only returns days the market is OPEN; weekends and
    holidays are absent, so we fill the full range locally and mark any
    date missing from the response as closed.
    """
    key = os.environ.get("ALPACA_KEY", "")
    secret = os.environ.get("ALPACA_SECRET", "")
    if not key or not secret:
        log.warning("ALPACA_KEY/SECRET not set; calendar unavailable")
        return []
    r = requests.get(
        f"{ALPACA_BASE}/v2/calendar",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        params={"start": start.isoformat(), "end": end.isoformat()},
        timeout=30)
    r.raise_for_status()
    hours = {d["date"]: (d.get("open"), d.get("close")) for d in r.json()}
    days = []
    d = start
    while d <= end:
        ds = d.isoformat()
        o, c = hours.get(ds, (None, None))
        days.append({"date": ds, "is_open": ds in hours,
                     "open": o, "close": c})
        d += dt.timedelta(days=1)
    return days


def refresh_cache(store, months_ahead: int = 3, months_back: int = 1) -> int:
    """Fetch ±months of calendar into SQLite. Returns days stored (0 on error)."""
    from storage import Store  # noqa: F401  (type hint only)
    today = dt.date.today()
    start = today - dt.timedelta(days=30 * months_back)
    end = today + dt.timedelta(days=30 * months_ahead)
    try:
        days = fetch_calendar(start, end)
    except Exception as e:  # noqa: BLE001
        log.warning("calendar fetch failed: %s", e)
        return 0
    if days:
        store.calendar_save(days)
        log.info("cached %d calendar days %s..%s", len(days), start, end)
    return len(days)


def is_trading_day(store, date: dt.date | str) -> bool | None:
    """True/False if cached, None when the date isn't in the cache."""
    d = date.isoformat() if isinstance(date, dt.date) else date
    return store.calendar_is_open(d)


def next_trading_day(store, date: dt.date | str, forward: bool = True) -> dt.date | None:
    """Step to the next (or previous) cached trading day. None if unknown."""
    d = (date if isinstance(date, dt.date)
         else dt.date.fromisoformat(date))
    for _ in range(14):  # two weeks is plenty
        d += dt.timedelta(days=1 if forward else -1)
        known = store.calendar_is_open(d.isoformat())
        if known is None:
            return None  # cache gap: don't guess
        if known:
            return d
    return None


def session_state_now(store) -> dict:
    """What main.py logs at boot: today's date, open/closed/holiday, hours."""
    today = dt.date.today()
    is_open = store.calendar_is_open(today.isoformat())
    return {"date": today.isoformat(), "is_trading_day": is_open,
            "note": ("market open" if is_open else
                     "market closed (holiday/weekend)" if is_open is False else
                     "calendar not cached")}
