"""Momentum leaders: the 1-3 month list.

The best-documented stock edge (Jegadeesh & Titman 1993 and decades of
follow-ups): stocks that rose most over the past 12 months, skipping the
most recent month, tend to keep outperforming over the next 3-12 months.

Rule (chosen on 2024-03..2025-05, judged on 2025-06..2026-08, see
study_position.py): rank S&P 500 stocks (>= $3, >= $5M/day) by their
12-month return excluding the last month; take the top 10; hold about 3
months; refresh the list monthly.

On large caps the backtest returned far more than SPY, but it uses
today's S&P members (stocks that rose INTO the index are included), which
flatters momentum a lot; on small caps the same rule lost. Momentum also
crashes in sharp market reversals, so the list warns when SPY is below its
200-day average. Treat it as a starting watchlist for longer holds.
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
    rows = sorted(uniq.values(), key=lambda r: r["mom12"], reverse=True)
    top = rows[:TOP_N]
    for k, r in enumerate(top, 1):
        r["rank"] = k
        r["score"] = k                       # for the scorecard log
        notes = []
        if not r["above200"]:
            r["warning"] = ("Already below its 200-day average: the safety exit "
                            "would sell it, so many traders skip it.")
        elif not r["above50"]:
            notes.append("below its 50-day average: momentum is fading")
        if r["ret1m"] > 20:
            notes.append(f"up {r['ret1m']:.0f}% in the last month: stretched, consider waiting for a pullback")
        r["notes"] = notes
        # simple exit guide: a close below the 200-day ends the trend
        r["exit"] = (f"Hold about 3 months and re-check monthly (the tested rule). "
                     f"Extra safety, not in the backtest: sell on a close below the "
                     f"200-day average ({r['sma200']:.2f}).")
    risk_off = None
    if spy_closes and len(spy_closes) >= 200:
        risk_off = spy_closes[-1] < sum(spy_closes[-200:]) / 200
    return {"BUY": top, "risk_off": risk_off, "ranked": len(rows)}


# Backtest (study_position.py, S&P 500 + liquid small caps, 2024-03..2026-08)
BACKTEST = {
    "rule": "12-1 month momentum, top 10, hold ~3 months",
    "test_period": "2025-06 to 2026-08",
    "sp500_test": {"hit": 52.3, "avg_trade": 10.95, "year": 53.1, "maxdd": 6.9, "trades": 130},
    "spy_year": 16.9,
    "smallcap_test": {"hit": 43.1, "avg_trade": 0.57, "year": 1.8},
}
