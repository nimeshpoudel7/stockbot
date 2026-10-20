"""Backtest stats per alert type (guide p27).

For each supported alert type, walk 1 year of daily bars per symbol, simulate
the type's firing rule (default params), and after every fire measure:

- target-first rate: how often +5% was reached before -3% (bar high/low,
  first touch wins; a bar touching both counts as stop-first, pessimistic)
- avg_move: mean 20-session return after the fire
- worst_move: worst 20-session return after the fire

Only rule-based types with a daily-bar meaning are covered: rsi_14,
bollinger_break, bollinger_squeeze, fib_cross, sma_cross, volume_spike,
day_pct_change. Event types (news/earnings/splits) need their own data feeds
and are excluded; atr_stop needs a per-trade entry price and is excluded.
"""
from __future__ import annotations

import logging

log = logging.getLogger("stockbot.backtest")

TARGET_PCT = 5.0
STOP_PCT = 3.0
HORIZON = 20  # sessions

BACKTESTABLE = ("rsi_14", "bollinger_break", "bollinger_squeeze",
                "fib_cross", "sma_cross", "volume_spike", "day_pct_change")


def _fires(alert_type: str, bars: list[dict], params: dict) -> list[int]:
    """Indices (into bars) where the rule fires on the bar's close."""
    import indicators as ind
    closes = [b["c"] for b in bars]
    out: list[int] = []
    if alert_type == "rsi_14":
        above, below = float(params.get("above", 70)), float(params.get("below", 30))
        if len(closes) >= 16:
            s = ind.rsi_wilder(closes, 14)
            for i in range(15, len(s)):
                if s[i - 1] is None or s[i] is None:
                    continue
                if s[i - 1] <= above < s[i] or s[i - 1] >= below > s[i]:
                    out.append(i)
    elif alert_type == "bollinger_break":
        n, k = int(params.get("n", 20)), float(params.get("k", 2.0))
        for i in range(n + 1, len(closes)):
            mid, upper, lower = ind.bollinger_bands(closes[:i], n, k)
            if mid is None:
                continue
            if lower < closes[i - 1] < upper and \
                    (closes[i] >= upper or closes[i] <= lower):
                out.append(i)
    elif alert_type == "bollinger_squeeze":
        lookback = int(params.get("lookback_days", 126))
        for i in range(lookback + 21, len(closes)):
            now_bw = ind.bollinger_bandwidth(closes[:i + 1])
            if now_bw is None:
                continue
            past = [ind.bollinger_bandwidth(closes[:j])
                    for j in range(20, i + 1)]
            past = [b for b in past[:-1] if b is not None]
            if past and now_bw <= min(past[-lookback:]):
                out.append(i)
    elif alert_type == "fib_cross":
        lookback = int(params.get("lookback", 50))
        for i in range(lookback + 2, len(bars)):
            hi, lo = ind.swing_high_low(bars[:i + 1], lookback)
            if hi is None or lo is None or hi <= lo:
                continue
            for lvl in ind.fib_levels(lo, hi).values():
                if (closes[i - 1] - lvl) * (closes[i] - lvl) < 0:
                    out.append(i)
                    break
    elif alert_type == "sma_cross":
        fast_n, slow_n = int(params.get("fast", 20)), int(params.get("slow", 50))
        if len(closes) >= slow_n + 2:
            for i in range(slow_n + 1, len(closes)):
                f0 = ind.sma(closes[:i - 1], fast_n)
                s0 = ind.sma(closes[:i - 1], slow_n)
                f1 = ind.sma(closes[:i + 1], fast_n)
                s1 = ind.sma(closes[:i + 1], slow_n)
                if None in (f0, s0, f1, s1):
                    continue
                if (f0 <= s0 and f1 > s1) or (f0 >= s0 and f1 < s1):
                    out.append(i)
    elif alert_type == "volume_spike":
        mult, lb = float(params.get("mult", 3)), int(params.get("lookback", 20))
        vols = [b.get("v", 0) for b in bars]
        for i in range(lb + 1, len(bars)):
            if ind.volume_spike(vols[i], vols[:i], mult, lb):
                out.append(i)
    elif alert_type == "day_pct_change":
        pct = float(params.get("pct", 5))
        for i in range(1, len(bars)):
            if not closes[i - 1]:
                continue
            move = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
            if (pct >= 0 and move >= pct) or (pct < 0 and move <= pct):
                out.append(i)
    return out


def _outcome(bars: list[dict], i: int) -> dict | None:
    """Target-before-stop + 20-session move after a fire at index i."""
    closes = [b["c"] for b in bars]
    entry = closes[i]
    if not entry:
        return None
    target, stop = entry * 1.05, entry * 0.97
    hit_target_first: bool | None = None
    for b in bars[i + 1:i + 1 + HORIZON]:
        t_hit = b["h"] >= target
        s_hit = b["l"] <= stop
        if t_hit or s_hit:
            # same bar touching both -> pessimistic: stop wins
            hit_target_first = t_hit and not s_hit
            break
    end = closes[min(i + HORIZON, len(closes) - 1)]
    move = (end - entry) / entry * 100
    return {"target_first": hit_target_first, "move": move}


def run_backtest(symbols: list[str], daily_bars_fn,
                 types: list[str] | None = None,
                 days: int = 365) -> dict:
    """{alert_type: {fires, target_first_rate, avg_move_pct, worst_move_pct,
                     per_symbol: {sym: fires}}}"""
    types = [t for t in (types or BACKTESTABLE) if t in BACKTESTABLE]
    stats: dict[str, dict] = {
        t: {"fires": 0, "target_first": 0, "decided": 0,
            "moves": [], "per_symbol": {}} for t in types}
    for symbol in symbols:
        try:
            bars = daily_bars_fn(symbol, days + 60)
        except Exception as e:  # noqa: BLE001
            log.warning("%s backtest bars failed: %s", symbol, e)
            continue
        if len(bars) < 100:
            continue
        bars = bars[-(days + 40):]  # ~1y + warm-up room
        for t in types:
            fires = [i for i in _fires(t, bars, {}) if i + 1 < len(bars)]
            st = stats[t]
            st["per_symbol"][symbol] = len(fires)
            for i in fires:
                oc = _outcome(bars, i)
                if oc is None:
                    continue
                st["fires"] += 1
                st["moves"].append(oc["move"])
                if oc["target_first"] is not None:
                    st["decided"] += 1
                    st["target_first"] += int(oc["target_first"])
    result = {}
    for t, st in stats.items():
        moves = st["moves"]
        result[t] = {
            "fires": st["fires"],
            "target_first_rate": (st["target_first"] / st["decided"]
                                  if st["decided"] else None),
            "decided": st["decided"],
            "avg_move_pct": (sum(moves) / len(moves) if moves else None),
            "worst_move_pct": (min(moves) if moves else None),
            "per_symbol": st["per_symbol"],
        }
    return result
