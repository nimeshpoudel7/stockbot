"""Momentum leaders: the 1-3 month list ("Hold 1-3 mo").

The best-documented stock edge (Jegadeesh & Titman 1993 and decades of
follow-ups): stocks that rose most over the past 12 months, skipping the
most recent month, tend to keep outperforming over the next 3-12 months.

Rule (study_position.py, point-in-time S&P 500 membership):
  - S&P 500 stocks, >= $3, >= $5M/day, above their 50- and 200-day averages
  - rank by 12-month return excluding the last month; top 10
  - hold about 3 months, refresh monthly, equal money in each
  - CASH (empty list) while SPY is below its 200-day average — momentum
    has its worst crashes in falling markets

Results are in BACKTEST below and on the page, with their caveats: the
history available here is short (2024-03..2026-08, mostly a bull market)
and stocks removed from the index are missing. Run
`study_position.py --fetch --years 8` on the server for a longer check.
"""
from __future__ import annotations

import statistics

TOP_N = 10
MIN_PRICE = 3.0
MIN_DVOL = 5_000_000
LOOKBACK, SKIP = 252, 21


def row(symbol: str, bars: list[dict]) -> dict | None:
    if not bars or len(bars) < LOOKBACK + 1:
        return None
    c = [b["c"] for b in bars]
    if c[-1] < MIN_PRICE or not c[-1 - LOOKBACK] or not c[-1 - SKIP]:
        return None
    med = statistics.median(b["v"] for b in bars[-20:])
    if med * c[-1] < MIN_DVOL:
        return None
    s50 = sum(c[-50:]) / 50
    s200 = sum(c[-200:]) / 200
    hi = max(b["h"] for b in bars[-252:])
    return {"symbol": symbol, "price": c[-1],
            "mom12": round((c[-1 - SKIP] / c[-1 - LOOKBACK] - 1) * 100, 1),
            "ret1m": round((c[-1] / c[-1 - SKIP] - 1) * 100, 1),
            "above50": c[-1] > s50, "above200": c[-1] > s200,
            "from_high": round((c[-1] / hi - 1) * 100, 1),
            "sma50": round(s50, 4), "sma200": round(s200, 4)}


def build(rows: list[dict], spy_closes: list[float] | None) -> dict:
    uniq = {r["symbol"]: r for r in rows if r}       # one row per symbol
    risk_off = None
    if spy_closes and len(spy_closes) >= 200:
        risk_off = spy_closes[-1] < sum(spy_closes[-200:]) / 200
    # the tested rule: only stocks in their own uptrend (above 50 & 200-day)
    trend = [r for r in uniq.values() if r["above50"] and r["above200"]]
    rows = sorted(trend, key=lambda r: r["mom12"], reverse=True)
    top = [] if risk_off else rows[:TOP_N]
    for k, r in enumerate(top, 1):
        r["rank"] = k
        r["score"] = k                       # for the scorecard log
        notes = []
        if r["ret1m"] > 20:
            notes.append(f"up {r['ret1m']:.0f}% in the last month: stretched, consider waiting for a pullback")
        r["notes"] = notes
        # simple exit guide: a close below the 200-day ends the trend
        r["exit"] = ("Hold about 3 months, re-check monthly (the tested rule). Sell "
                     "early if the whole list turns to cash (SPY below its 200-day). "
                     f"Extra safety, not in the backtest: a close below this stock's "
                     f"200-day ({r['sma200']:.2f}).")
    return {"BUY": top, "risk_off": risk_off, "ranked": len(rows),
            "watch": rows[:TOP_N] if risk_off else []}


# study_position.py, point-in-time S&P 500, train 2024-03..2025-04,
# test 2025-05..2026-08 (rule mom12_trend, hold 63, top 10)
BACKTEST = {
    "rule": "12-1 momentum, S&P 500, uptrend only, cash when SPY < 200-day, top 10, hold ~3 months",
    "train_period": "2024-03 to 2025-04",
    "test_period": "2025-05 to 2026-08",
    "train": {"hit": 62.3, "month_hit": 69.2, "avg_trade": 8.14, "year": 28.6, "maxdd": 4.1},
    "test": {"hit": 56.7, "month_hit": 75.0, "avg_trade": 8.39, "year": 32.5, "maxdd": 6.7, "trades": 120},
    "spy_year_test": 16.9,
}
