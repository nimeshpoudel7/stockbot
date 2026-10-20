"""Top 10 trending — "what's moving right now", rebuilt on every 15-minute
watch pass.

How a real-time trader builds a momentum list, in order:

1. Movers: % change vs yesterday's close (with the gap from the open).
2. Relative volume, *paced* for the time of day — 1M shares at 10:00 is
   not the same as 1M at 15:30 (chart_analysis.paced_volume_ratio).
3. Where the stock sits in today's range (closing strong near the high,
   or weak near the low) and whether it broke the 20-day high/low.
4. Trend alignment: price vs EMA 9 / EMA 21 / SMA 50.
5. A full chart read (chart_analysis.analyze — candles, patterns, levels,
   volume, indicators, liquidity, context) on the short list only.

The 0-100 score ranks *how strongly and cleanly* a stock is moving, not
whether to buy it. It is NOT a backtested signal — momentum lists are a
starting point for your own read, and big movers reverse often.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time

import chart_analysis as CA

MIN_PRICE = 3.0
MIN_DOLLAR_VOL = 5_000_000      # 20-day median $ volume
SHORTLIST = 60                  # chart reads per pass (cost control)
TOP_N = 10
READ_BUDGET_SECS = 45.0         # stop chart reads after this; the rest are
                                # ranked on the cheap metrics alone
STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                          "trending_stats.json")


def history() -> dict | None:
    """What the list did next historically (study_trending.py output)."""
    try:
        with open(STATS_PATH) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    keep = ("n", "avg1", "avg5", "avg10", "up1", "up5", "up10")
    return {"meta": {k: d.get("meta", {}).get(k) for k in ("from", "to", "symbols")},
            **{k: {x: d[k].get(x) for x in keep} for k in ("up", "down", "base")
               if k in d}}


def _ema(xs: list[float], n: int) -> float | None:
    if len(xs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def _atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))
           for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / n


def mover_metrics(bars: list[dict], minutes: float | None) -> dict | None:
    """Cheap per-stock numbers from daily bars (last bar = today, possibly
    partial). None when the stock is too thin or too cheap to matter."""
    if not bars or len(bars) < 60:
        return None
    t, p = bars[-1], bars[-2]
    price, prev = t["c"], p["c"]
    if not prev or price < MIN_PRICE:
        return None
    base = [b["v"] for b in bars[-21:-1]]
    med_v = statistics.median(base) if len(base) >= 20 else None
    if not med_v or med_v * prev < MIN_DOLLAR_VOL:
        return None
    closes = [b["c"] for b in bars]
    rng = t["h"] - t["l"]
    hi20 = max(b["h"] for b in bars[-21:-1])
    lo20 = min(b["l"] for b in bars[-21:-1])
    ema9, ema21 = _ema(closes, 9), _ema(closes, 21)
    sma50 = sum(closes[-50:]) / 50
    return {
        "price": price, "prev_close": prev,
        "pct": (price / prev - 1) * 100,
        "gap_pct": (t["o"] / prev - 1) * 100 if t["o"] else 0.0,
        "rvol": CA.paced_volume_ratio(t["v"], med_v, minutes),
        "cpos": (price - t["l"]) / rng if rng > 0 else 0.5,
        "new_high20": price > hi20, "new_low20": price < lo20,
        "ema9": ema9, "ema21": ema21, "sma50": sma50,
        "atr": _atr(bars),
        "dollar_vol_m": t["v"] * price / 1e6,
    }


def is_candidate(m: dict) -> bool:
    """Moving with conviction: a real % move on above-normal volume, or a
    big volume surge (news/flow) even on a smaller move."""
    rv = m["rvol"] or 0
    mv = abs(m["pct"])
    return (mv >= 3 and rv >= 1.5) or (rv >= 3 and mv >= 1.5) or mv >= 8


def prelim_score(m: dict) -> float:
    """Ranking for the shortlist (before the expensive chart read)."""
    return min(abs(m["pct"]), 15) * 2 + min(math.log2(max(m["rvol"] or 1, 1)), 4) * 6


def score(m: dict, summary: dict | None) -> tuple[int, list[str]]:
    """0-100 strength of the move in its direction + the reasons."""
    up = m["pct"] >= 0
    why = []
    s = min(abs(m["pct"]) / 8, 1) * 30
    why.append(f"{'+' if up else '−'}{abs(m['pct']):.1f}% today"
               + (f" (gapped {m['gap_pct']:+.1f}%)" if abs(m["gap_pct"]) >= 2 else ""))
    rv = m["rvol"] or 0
    s += min(math.log2(max(rv, 1)) / 3, 1) * 25
    if rv >= 1.5:
        why.append(f"{rv:.1f}× normal volume for this time of day")
    pos = m["cpos"] if up else 1 - m["cpos"]
    s += pos * 15
    if pos >= 0.8:
        why.append("holding near the day's " + ("high" if up else "low"))
    elif pos <= 0.35:
        why.append("faded from the day's " + ("high" if up else "low")
                   + " — move losing steam")
    if (up and m["new_high20"]) or (not up and m["new_low20"]):
        s += 10
        why.append("broke the 20-day " + ("high" if up else "low"))
    e9, e21, s50, px = m["ema9"], m["ema21"], m["sma50"], m["price"]
    if e9 and e21:
        aligned = (px > e9 > e21 and px > s50) if up else (px < e9 < e21 and px < s50)
        if aligned:
            s += 10
            why.append("EMA 9 > 21 and above the 50-day" if up
                       else "EMA 9 < 21 and below the 50-day")
    if summary:
        b, br = summary.get("bull", 0), summary.get("bear", 0)
        if b + br:
            share = (b - br) / (b + br) * (1 if up else -1)
            s += max(0.0, share) * 10
            if share < -0.2:
                why.append("chart read leans the other way")
    return int(round(min(s, 100))), why


def rating(sc: int) -> str:
    return "Hot" if sc >= 70 else "Strong" if sc >= 50 else "Active"


def build(cands: dict[str, tuple[dict, list[dict]]], spy: list[float] | None,
          minutes: float | None, now=None,
          budget: float = READ_BUDGET_SECS) -> dict:
    """cands: {symbol: (metrics, bars)} already filtered by is_candidate.
    Returns {"up": [...], "down": [...]} — top 10 each, best first.

    Chart reads run strongest-first and stop when `budget` seconds are
    used, so a slow server can't make the 15-minute pass run long."""
    t0 = time.monotonic()
    short = sorted(cands.items(), key=lambda kv: prelim_score(kv[1][0]),
                   reverse=True)[:SHORTLIST]
    rows = []
    read = 0
    for sym, (m, bars) in short:
        rep = None
        if time.monotonic() - t0 < budget:
            try:
                rep = CA.analyze(bars, spy_closes=spy, now=now)
                read += 1
            except Exception:  # noqa: BLE001 - one bad chart never stops the list
                rep = None
        summ = rep["summary"] if rep else None
        sc, why = score(m, summ)
        up = m["pct"] >= 0
        want = "bull" if up else "bear"
        finds = []
        if rep:
            act = [f for sec in rep["sections"] for f in sec["items"]
                   if f["active"] and f["bias"] == want]
            act.sort(key=lambda f: (-f["weight"], -(f["ts"] or 0)))
            finds = [{"name": f["name"], "status": f["status"]} for f in act[:4]]
        A = m["atr"] or 0
        px = m["price"]
        rows.append({
            "symbol": sym, "dir": "up" if up else "down",
            "price": round(px, 4), "pct": round(m["pct"], 2),
            "gap_pct": round(m["gap_pct"], 2),
            "rvol": round(m["rvol"], 2) if m["rvol"] is not None else None,
            "cpos": round(m["cpos"], 2),
            "breakout": bool(m["new_high20"] if up else m["new_low20"]),
            "dollar_vol_m": round(m["dollar_vol_m"], 1),
            "score": sc, "rating": rating(sc), "why": why,
            "bias": summ["bias"] if summ else None,
            "headline": summ["headline"] if summ else None,
            "findings": finds,
            # reference levels for a momentum trade: 1.5 ATR risk, 2R target
            "stop": round(px - 1.5 * A if up else px + 1.5 * A, 4) if A else None,
            "target": round(px + 3 * A if up else px - 3 * A, 4) if A else None,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {"up": [r for r in rows if r["dir"] == "up"][:TOP_N],
            "down": [r for r in rows if r["dir"] == "down"][:TOP_N],
            "candidates": len(cands), "analyzed": read,
            "build_secs": round(time.monotonic() - t0, 1),
            "history": history()}
