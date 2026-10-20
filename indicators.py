"""Pure indicator math. No I/O, no dependencies — fully unit-testable.

Conventions:
- ``closes`` / ``values`` are oldest-first lists of floats.
- Functions return ``None`` where there is not enough data (warm-up).
"""
from __future__ import annotations


def sma(values: list[float], n: int) -> float | None:
    """Simple moving average of the last n values."""
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema(values: list[float], n: int) -> float | None:
    """Exponential moving average, seeded with the SMA of the first n values."""
    if n <= 0 or len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n  # seed
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi_wilder(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI. Returns a list aligned with ``closes``; None until warmed.

    Seeding: first avg gain/loss = simple average of the first ``period``
    price changes, then Wilder smoothing:
        avg = (prev_avg * (period - 1) + current) / period
    """
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) < period + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(chg if chg > 0 else 0.0)
        losses.append(-chg if chg < 0 else 0.0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    idx = period  # first RSI value sits at closes[period]
    while True:
        if avg_loss == 0:
            out[idx] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[idx] = 100.0 - (100.0 / (1.0 + rs))
        idx += 1
        if idx >= len(closes):
            break
        avg_gain = (avg_gain * (period - 1) + gains[idx - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx - 1]) / period
    return out


def atr(bars: list[dict], period: int = 14) -> float | None:
    """Average True Range (Wilder smoothing). Each bar: {h, l, c}."""
    if period <= 0 or len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, c = bars[i]["h"], bars[i]["l"], bars[i]["c"]
        prev_c = bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def crossed_above(fast: list[float | None], slow: list[float | None]) -> bool:
    """True if fast crossed above slow on the most recent bar."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    f0, f1, s0, s1 = fast[-2], fast[-1], slow[-2], slow[-1]
    if None in (f0, f1, s0, s1):
        return False
    return f0 <= s0 and f1 > s1


def crossed_below(fast: list[float | None], slow: list[float | None]) -> bool:
    """True if fast crossed below slow on the most recent bar."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    f0, f1, s0, s1 = fast[-2], fast[-1], slow[-2], slow[-1]
    if None in (f0, f1, s0, s1):
        return False
    return f0 >= s0 and f1 < s1


def volume_spike(current_volume: float, recent_volumes: list[float],
                 multiplier: float = 3.0, lookback: int = 20) -> bool:
    """True if current 1m volume exceeds ``multiplier`` × mean of last N bars."""
    window = recent_volumes[-lookback:]
    if len(window) < lookback:
        return False
    avg = sum(window) / len(window)
    return avg > 0 and current_volume > multiplier * avg


def day_pct_change(price: float, prev_close: float) -> float | None:
    """Percent change vs prior close. None if prev_close is missing/zero."""
    if not prev_close:
        return None
    return (price - prev_close) / prev_close * 100.0


# ------------------------------------------------- Bollinger / Fibonacci ---
def bollinger_bands(closes: list[float], n: int = 20,
                    k: float = 2.0) -> tuple[float | None, float | None, float | None]:
    """(mid, upper, lower) = SMA(n) ± k·σ. Nones when not enough data."""
    if n <= 0 or len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((v - mid) ** 2 for v in window) / n
    sd = var ** 0.5
    return mid, mid + k * sd, mid - k * sd


def bollinger_bandwidth(closes: list[float], n: int = 20,
                        k: float = 2.0) -> float | None:
    """Bandwidth = (upper − lower) / mid. Squeeze = bandwidth at multi-month low."""
    mid, upper, lower = bollinger_bands(closes, n, k)
    if mid is None or mid == 0:
        return None
    return (upper - lower) / mid  # type: ignore[operator]


def bollinger_pctb(price: float, upper: float, lower: float) -> float | None:
    """%B: where price sits inside the bands (1.0 = upper band, 0.0 = lower)."""
    if upper is None or lower is None or upper == lower:
        return None
    return (price - lower) / (upper - lower)


def swing_high_low(bars: list[dict], lookback: int = 50) -> tuple[float | None, float | None]:
    """Recent swing high/low = max high and min low over the last N bars."""
    window = bars[-lookback:] if lookback > 0 else bars
    if not window:
        return None, None
    return max(b["h"] for b in window), min(b["l"] for b in window)


FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


def fib_levels(swing_low: float, swing_high: float) -> dict[float, float]:
    """Retracement levels per the guide: level = H − (H − L) × ratio."""
    return {r: swing_high - (swing_high - swing_low) * r for r in FIB_RATIOS}


# ------------------------------------------------------------------ MACD ---
def ema_series(values: list[float], n: int) -> list[float | None]:
    """EMA per bar, seeded with SMA of first n; None until warmed."""
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def macd_series(closes: list[float], fast: int = 12, slow: int = 26,
                signal: int = 9) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """(macd_line, signal_line, histogram), each aligned with closes."""
    n = len(closes)
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    macd: list[float | None] = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(ef, es)]
    # signal = EMA of the macd line over its valid span
    valid = [(i, v) for i, v in enumerate(macd) if v is not None]
    sig: list[float | None] = [None] * n
    if len(valid) >= signal:
        vals = [v for _, v in valid]
        k = 2.0 / (signal + 1)
        e = sum(vals[:signal]) / signal
        sig[valid[signal - 1][0]] = e
        for j in range(signal, len(vals)):
            e = vals[j] * k + e * (1 - k)
            sig[valid[j][0]] = e
    hist = [(m - s) if (m is not None and s is not None) else None
            for m, s in zip(macd, sig)]
    return macd, sig, hist


# ------------------------------------------------------- misc features ---
def volume_zscore(current: float, recent: list[float],
                  lookback: int = 20) -> float | None:
    """How unusual current volume is, in standard deviations."""
    window = recent[-lookback:]
    if len(window) < lookback:
        return None
    mean = sum(window) / len(window)
    var = sum((v - mean) ** 2 for v in window) / len(window)
    sd = var ** 0.5
    if sd == 0:
        return None
    return (current - mean) / sd


def atr_pct(bars: list[dict], period: int = 14) -> float | None:
    """ATR as a fraction of the last close (volatility in price units)."""
    a = atr(bars, period)
    if a is None or not bars or not bars[-1]["c"]:
        return None
    return a / bars[-1]["c"]


def aggregate_bars(bars_1m: list[dict], minutes: int) -> list[dict]:
    """Fold 1-minute bars into N-minute bars. Input oldest-first, ts=epoch min."""
    if minutes <= 1:
        return list(bars_1m)
    span = minutes * 60
    out: list[dict] = []
    cur: dict | None = None
    cur_bucket: int | None = None
    for b in bars_1m:
        bucket = b["ts"] // span
        if cur is None or bucket != cur_bucket:
            if cur is not None:
                out.append(cur)
            cur = {"ts": bucket * span, "o": b["o"], "h": b["h"],
                   "l": b["l"], "c": b["c"], "v": b.get("v", 0)}
            cur_bucket = bucket
        else:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
            cur["v"] = cur.get("v", 0) + b.get("v", 0)
    if cur is not None:
        out.append(cur)
    return out
