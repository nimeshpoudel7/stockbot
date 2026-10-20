"""Market regime filter: SPY vs its 200-day SMA (+ slope) -> bull/bear/sideways.

Per the guide's reliability section, alerts can be gated to fire only in a
chosen regime. SPY daily bars are cached in the candles table (tf='1d') and
refreshed at most once per day, so this costs ~1 Alpaca call/day.
"""
from __future__ import annotations

import logging
import time

import indicators as ind

log = logging.getLogger("stockbot.regime")

SYMBOL = "SPY"
SMA_N = 200
SLOPE_DAYS = 20


def compute_regime(closes: list[float], n: int = SMA_N,
                   slope_days: int = SLOPE_DAYS) -> dict | None:
    """Pure function: -> {regime, price, sma200, slope_pct} or None if short."""
    if len(closes) < n + slope_days:
        return None
    price = closes[-1]
    sma = ind.sma(closes, n)
    if sma is None or sma == 0:
        return None
    past = ind.sma(closes[:-slope_days], n)
    slope_pct = ((sma - past) / past * 100.0) if past else 0.0
    if price > sma and slope_pct > 0:
        regime = "bull"
    elif price < sma and slope_pct < 0:
        regime = "bear"
    else:
        regime = "sideways"
    return {"regime": regime, "price": price, "sma200": sma,
            "slope_pct": slope_pct, "asof": time.strftime("%Y-%m-%d")}


class RegimeCache:
    """Caches SPY daily closes in the candles table; refreshes daily."""

    def __init__(self, store, daily_bars_fn):
        """
        daily_bars_fn(symbol, days) -> [bar dicts oldest-first, tf 1d]
        """
        self.store = store
        self.daily_bars_fn = daily_bars_fn
        self._cached: dict | None = None
        self._asof: str | None = None
        self._last_try: float = 0.0

    def _load_closes(self) -> list[float]:
        rows = self.store.candles_load(SYMBOL, "1d", 400)
        return [r["c"] for r in rows]

    def refresh(self, force: bool = False) -> dict | None:
        today = time.strftime("%Y-%m-%d")
        if not force and self._asof == today and self._cached:
            return self._cached
        # transient failures retry at most hourly, never on every call
        if not force and time.time() - self._last_try < 3600:
            return self._cached
        self._last_try = time.time()
        closes = self._load_closes()
        # stale/empty -> fetch a year of SPY daily bars via Alpaca (1 call)
        if len(closes) < SMA_N + SLOPE_DAYS:
            try:
                bars = self.daily_bars_fn(SYMBOL, 400)
            except Exception as e:  # noqa: BLE001
                log.warning("SPY daily fetch failed: %s", e)
                bars = []
            if bars:
                self.store.candles_save(SYMBOL, "1d", bars)
                closes = [b["c"] for b in bars]
        self._cached = compute_regime(closes)
        if self._cached:
            self._asof = today  # only "done" when we have a real answer
            log.info("regime: %s (SPY %.2f vs SMA200 %.2f)",
                     self._cached["regime"], self._cached["price"],
                     self._cached["sma200"])
        return self._cached

    def current(self) -> str | None:
        """Regime string for AlertEngine.regime_fn. Refreshes at most daily."""
        r = self.refresh()
        return r["regime"] if r else None
