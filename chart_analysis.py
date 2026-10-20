"""Chart reading for one stock: the techniques a chart reader checks, run
on daily bars (plus today's 1-minute bars when available).

What this is: a structured, plain-language read of the chart — gaps,
candlestick patterns, trend structure, support/resistance, classic chart
patterns, volume, the standard indicators, liquidity concepts, market
context and risk levels. Every technique is checked on every stock; the
page lists what fired and what was checked but not present.

What this is NOT: a signal. None of these detections is part of the
scanner's score (that is scanner.technical_screen, which is validated on
history — see study_trader_signals.py). Pattern rules are mechanical
approximations of shapes that chart readers judge by eye; they describe,
they do not predict.

Conventions: bars oldest-first {ts, o, h, l, c, v}; ts = epoch seconds.
Every finding is a dict:
    {"name", "status", "detail", "bias": "bull"|"bear"|"neutral",
     "active": bool, "ts": epoch|None, "weight": 1..3}
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Series helpers (pure python; n <= ~300 so speed is not a concern)
# ---------------------------------------------------------------------------


def sma_s(x: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema_s(x: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(x)
    if len(x) < n:
        return out
    k = 2 / (n + 1)
    e = sum(x[:n]) / n
    out[n - 1] = e
    for i in range(n, len(x)):
        e = x[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi_s(c: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(0.0, c[i] - c[i - 1]) for i in range(1, len(c))]
    lo = [max(0.0, c[i - 1] - c[i]) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + g[i - 1]) / n
        al = (al * (n - 1) + lo[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def tr_s(h, l, c) -> list[float]:
    out = [h[0] - l[0]]
    for i in range(1, len(c)):
        out.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return out


def atr_s(h, l, c, n: int = 14) -> list[float | None]:
    tr = tr_s(h, l, c)
    out: list[float | None] = [None] * len(c)
    if len(c) <= n:
        return out
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def macd_s(c, fast=12, slow=26, sig=9):
    ef, es = ema_s(c, fast), ema_s(c, slow)
    line = [(a - b) if a is not None and b is not None else None
            for a, b in zip(ef, es)]
    valid = [v for v in line if v is not None]
    first = next((i for i, v in enumerate(line) if v is not None), len(c))
    sg_valid = ema_s(valid, sig)
    signal: list[float | None] = [None] * first + sg_valid
    hist = [(a - b) if a is not None and b is not None else None
            for a, b in zip(line, signal)]
    return line, signal, hist


def rolling(x, n, fn):
    """fn over the window ENDING at i (inclusive)."""
    return [fn(x[i - n + 1:i + 1]) if i >= n - 1 else None
            for i in range(len(x))]


def stoch_s(h, l, c, n=14, d=3):
    k = []
    for i in range(len(c)):
        if i < n - 1:
            k.append(None)
            continue
        hh, ll = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        k.append(50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100)
    kv = [v if v is not None else 0.0 for v in k]
    dd = sma_s(kv, d)
    dd = [dd[i] if i >= n - 1 + d - 1 else None for i in range(len(c))]
    return k, dd


def adx_s(h, l, c, n=14):
    """Wilder ADX with +DI / -DI."""
    L = len(c)
    plus = [None] * L
    minus = [None] * L
    adx = [None] * L
    if L < 2 * n + 1:
        return adx, plus, minus
    tr = tr_s(h, l, c)
    pdm = [0.0] + [max(h[i] - h[i - 1], 0) if (h[i] - h[i - 1]) > (l[i - 1] - l[i]) else 0.0
                   for i in range(1, L)]
    mdm = [0.0] + [max(l[i - 1] - l[i], 0) if (l[i - 1] - l[i]) > (h[i] - h[i - 1]) else 0.0
                   for i in range(1, L)]
    atr_ = sum(tr[1:n + 1])
    p_ = sum(pdm[1:n + 1])
    m_ = sum(mdm[1:n + 1])
    dx = []
    for i in range(n, L):
        if i > n:
            atr_ = atr_ - atr_ / n + tr[i]
            p_ = p_ - p_ / n + pdm[i]
            m_ = m_ - m_ / n + mdm[i]
        pdi = 100 * p_ / atr_ if atr_ else 0.0
        mdi = 100 * m_ / atr_ if atr_ else 0.0
        plus[i], minus[i] = pdi, mdi
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
    a = sum(dx[:n]) / n
    adx[2 * n - 1] = a
    for j in range(n, len(dx)):
        a = (a * (n - 1) + dx[j]) / n
        adx[n + j] = a
    return adx, plus, minus


def cci_s(h, l, c, n=20):
    tp = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
    out = [None] * len(c)
    for i in range(n - 1, len(c)):
        w = tp[i - n + 1:i + 1]
        m = sum(w) / n
        md = sum(abs(v - m) for v in w) / n
        out[i] = 0.0 if md == 0 else (tp[i] - m) / (0.015 * md)
    return out


def obv_s(c, v):
    out = [0.0]
    for i in range(1, len(c)):
        out.append(out[-1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0))
    return out


def psar_s(h, l, step=0.02, mx=0.2):
    """Parabolic SAR -> (sar list, uptrend flags)."""
    L = len(h)
    if L < 3:
        return [None] * L, [None] * L
    up = h[1] + l[1] >= h[0] + l[0]
    sar = [None] * L
    trend = [None] * L
    af = step
    ep = h[1] if up else l[1]
    s = l[0] if up else h[0]
    for i in range(1, L):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < s:
                up, s, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(mx, af + step)
        else:
            s = max(s, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(mx, af + step)
        sar[i], trend[i] = s, up
    return sar, trend


def pivots(h, l, k=3):
    """Swing highs / lows: index i whose high (low) is the max (min) of the
    k bars on each side. The last k bars can't be confirmed yet."""
    ph, pl = [], []
    for i in range(k, len(h) - k):
        # strictly beyond the left side, at least equal to the right side:
        # a flat top of two equal bars yields one pivot (the first bar)
        if h[i] > max(h[i - k:i]) and h[i] >= max(h[i + 1:i + k + 1]):
            ph.append(i)
        if l[i] < min(l[i - k:i]) and l[i] <= min(l[i + 1:i + k + 1]):
            pl.append(i)
    return ph, pl


def linreg(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, my
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return b, my - b * mx


# ---------------------------------------------------------------------------
# Intraday volume pace
# ---------------------------------------------------------------------------
# Typical cumulative share of a US stock's daily volume by minutes since the
# 09:30 open (U-shaped intraday volume: heavy open and close).
_VOL_CURVE = [(0, 0.0), (30, 0.12), (60, 0.20), (90, 0.27), (120, 0.33),
              (150, 0.38), (180, 0.43), (210, 0.47), (240, 0.52),
              (270, 0.57), (300, 0.62), (330, 0.68), (360, 0.77),
              (390, 1.0)]


def expected_volume_fraction(minutes_since_open: float) -> float:
    """Share of a normal day's volume usually traded by this time."""
    m = max(0.0, min(390.0, minutes_since_open))
    for (m0, f0), (m1, f1) in zip(_VOL_CURVE, _VOL_CURVE[1:]):
        if m <= m1:
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


def session_minutes(now: dt.datetime | None = None) -> float | None:
    """Minutes since 09:30 ET today, or None outside 09:30-16:00 Mon-Fri."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:
        return None
    m = (now.hour * 60 + now.minute) - 570
    return m if 0 <= m < 390 else None


def paced_volume_ratio(today_vol: float, median_vol: float,
                       minutes: float | None) -> float | None:
    """Today's volume vs what a normal day has traded by this time.
    minutes=None means a full session (plain ratio)."""
    if not median_vol:
        return None
    if minutes is None:
        return today_vol / median_vol
    frac = max(0.05, expected_volume_fraction(minutes))
    return today_vol / (median_vol * frac)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
def F(name: str, status: str, detail: str, bias: str = "neutral",
      active: bool = True, ts: int | None = None, weight: int = 1,
      level: float | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail, "bias": bias,
            "active": active, "ts": ts, "weight": weight, "level": level}


def _absent(name: str, what: str = "Not on this chart right now.") -> dict:
    return F(name, "Not present", what, active=False)


def _p(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.4f}"


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100 if b else 0.0


def _day(ts: int) -> str:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
    return f"{d.strftime('%b')} {d.day}"


class Chart:
    """Pre-computed series for one symbol's daily bars."""

    def __init__(self, bars: list[dict]):
        self.bars = bars
        self.t = [b["ts"] for b in bars]
        self.o = [b["o"] for b in bars]
        self.h = [b["h"] for b in bars]
        self.l = [b["l"] for b in bars]
        self.c = [b["c"] for b in bars]
        self.v = [b.get("v", 0) or 0 for b in bars]
        self.n = len(bars)
        self.atr = atr_s(self.h, self.l, self.c)
        self.A = self.atr[-1] or (sum(self.h[-14:]) - sum(self.l[-14:])) / 14 or self.c[-1] * 0.02
        self.sma10 = sma_s(self.c, 10)
        self.sma20 = sma_s(self.c, 20)
        self.sma50 = sma_s(self.c, 50)
        self.sma200 = sma_s(self.c, 200)
        self.ema9 = ema_s(self.c, 9)
        self.ema21 = ema_s(self.c, 21)
        self.rsi = rsi_s(self.c)
        self.vmed = [statistics.median(self.v[i - 20:i]) if i >= 20 else None
                     for i in range(self.n)]
        self.ph, self.pl = pivots(self.h, self.l, 3)
        self.ph2, self.pl2 = pivots(self.h, self.l, 2)

    def vrel(self, i: int) -> float | None:
        m = self.vmed[i]
        return self.v[i] / m if m else None

    def body(self, i):
        return abs(self.c[i] - self.o[i])

    def rng(self, i):
        return self.h[i] - self.l[i]

    def upper(self, i):
        return self.h[i] - max(self.o[i], self.c[i])

    def lower(self, i):
        return min(self.o[i], self.c[i]) - self.l[i]

    def green(self, i):
        return self.c[i] > self.o[i]

    def red(self, i):
        return self.c[i] < self.o[i]

    def fell(self, i, n=5):
        return i >= n and self.c[i - 1] < self.c[i - n]

    def rose(self, i, n=5):
        return i >= n and self.c[i - 1] > self.c[i - n]


# ---------------------------------------------------------------------------
# 1. Trend and market structure
# ---------------------------------------------------------------------------
def structure(ch: Chart) -> dict:
    """HH/HL vs LH/LL from confirmed swing points + BOS / CHoCH."""
    ph, pl = ch.ph, ch.pl
    out = {"trend": "range", "label": "", "last_ph": None, "last_pl": None,
           "events": []}
    if len(ph) < 2 or len(pl) < 2:
        return out
    h1, h2 = ch.h[ph[-2]], ch.h[ph[-1]]
    l1, l2 = ch.l[pl[-2]], ch.l[pl[-1]]
    hh, hl = h2 > h1, l2 > l1
    trend = ("up" if hh and hl else "down" if (not hh and not hl) else "range")
    out.update(trend=trend, label=f"{'HH' if hh else 'LH'} + {'HL' if hl else 'LL'}",
               last_ph=ph[-1], last_pl=pl[-1])
    # breaks of the most recent swing levels after they formed
    for i in range(max(ph[-1], pl[-1]) + 1, ch.n):
        if ch.c[i] > ch.h[ph[-1]] and ch.c[i - 1] <= ch.h[ph[-1]]:
            out["events"].append(("bull", "BOS" if trend == "up" else "CHoCH", i, ch.h[ph[-1]]))
        if ch.c[i] < ch.l[pl[-1]] and ch.c[i - 1] >= ch.l[pl[-1]]:
            out["events"].append(("bear", "BOS" if trend == "down" else "CHoCH", i, ch.l[pl[-1]]))
    return out


def trend_section(ch: Chart, st: dict) -> list[dict]:
    items = []
    c = ch.c[-1]
    tr = st["trend"]
    if st["label"]:
        txt = {"up": "Uptrend: higher highs and higher lows.",
               "down": "Downtrend: lower highs and lower lows.",
               "range": "No clean trend: swing highs and lows disagree, so price is ranging."}[tr]
        items.append(F("Market structure", {"up": "Uptrend", "down": "Downtrend", "range": "Sideways"}[tr],
                       f"{txt} Last swings: {st['label']} (high {_p(ch.h[st['last_ph']])}, "
                       f"low {_p(ch.l[st['last_pl']])}).",
                       {"up": "bull", "down": "bear", "range": "neutral"}[tr], weight=3))
    else:
        items.append(_absent("Market structure", "Not enough swing points yet to judge structure."))
    bos = [e for e in st["events"] if e[1] == "BOS"]
    choch = [e for e in st["events"] if e[1] == "CHoCH"]
    if bos:
        b = bos[-1]
        items.append(F("Break of structure (BOS)", "Bullish" if b[0] == "bull" else "Bearish",
                       f"Closed {'above the last swing high' if b[0] == 'bull' else 'below the last swing low'} "
                       f"({_p(b[3])}) on {_day(ch.t[b[2]])}, in the direction of the trend — continuation.",
                       b[0], ts=ch.t[b[2]], weight=2, level=b[3], active=ch.n - 1 - b[2] <= 10))
    else:
        items.append(_absent("Break of structure (BOS)", "No close beyond the latest swing level in the trend's direction."))
    if choch:
        b = choch[-1]
        items.append(F("Change of character (CHoCH)", "Bullish" if b[0] == "bull" else "Bearish",
                       f"Closed {'above the last lower high' if b[0] == 'bull' else 'below the last higher low'} "
                       f"({_p(b[3])}) on {_day(ch.t[b[2]])} — an early sign the trend may be turning. Needs follow-through.",
                       b[0], ts=ch.t[b[2]], weight=2, level=b[3], active=ch.n - 1 - b[2] <= 5))
    else:
        items.append(_absent("Change of character (CHoCH)", "No break against the current structure."))
    # moving-average trend
    s20, s50, s200 = ch.sma20[-1], ch.sma50[-1], ch.sma200[-1]
    if s50 and s200:
        stack_up = c > s50 > s200 and (s20 or 0) > s50
        stack_dn = c < s50 < s200 and (s20 or 1e18) < s50
        items.append(F("Moving-average trend",
                       "Bullish stack" if stack_up else "Bearish stack" if stack_dn else "Mixed",
                       f"Price {_p(c)} vs SMA 20 {_p(s20)}, 50 {_p(s50)}, 200 {_p(s200)}. "
                       + ("All lined up upward — the classic uptrend picture." if stack_up else
                          "All lined up downward — the classic downtrend picture." if stack_dn else
                          "Averages are tangled — no clean trend by moving averages."),
                       "bull" if stack_up else "bear" if stack_dn else "neutral", weight=2))
    # pullback vs reversal
    if tr == "up" and st["last_pl"] is not None and s20:
        hl = ch.l[st["last_pl"]]
        if c < s20 and c > hl:
            items.append(F("Pullback vs reversal", "Pullback",
                           f"Dipped below the 20-day average but still above the last higher low ({_p(hl)}). "
                           "Inside an uptrend this is a pullback until that low breaks.", "bull"))
        elif c <= hl:
            items.append(F("Pullback vs reversal", "Possible reversal",
                           f"Closed below the last higher low ({_p(hl)}) — the uptrend's structure is broken.",
                           "bear", weight=2))
        else:
            items.append(_absent("Pullback vs reversal", "Price is holding above its 20-day average — no pullback underway."))
    elif tr == "down" and st["last_ph"] is not None and s20:
        lh = ch.h[st["last_ph"]]
        if c > s20 and c < lh:
            items.append(F("Pullback vs reversal", "Bounce in a downtrend",
                           f"Back above the 20-day average but still below the last lower high ({_p(lh)}). "
                           "Until that high breaks, treat it as a bounce.", "bear"))
        elif c >= lh:
            items.append(F("Pullback vs reversal", "Possible reversal",
                           f"Closed above the last lower high ({_p(lh)}) — the downtrend's structure is broken.",
                           "bull", weight=2))
        else:
            items.append(_absent("Pullback vs reversal", "Price is still under its 20-day average — no bounce underway."))
    else:
        items.append(_absent("Pullback vs reversal", "Only meaningful inside a clear trend."))
    return items


# ---------------------------------------------------------------------------
# 2. Support / resistance zones
# ---------------------------------------------------------------------------
def zones(ch: Chart, lookback: int = 250) -> list[dict]:
    """Cluster swing points into zones (tolerance = max(0.75 ATR, 1.2%))."""
    start = max(0, ch.n - lookback)
    pts = [(ch.h[i], i) for i in ch.ph if i >= start] + \
          [(ch.l[i], i) for i in ch.pl if i >= start]
    pts.sort()
    tol = max(0.75 * ch.A, ch.c[-1] * 0.012)
    out: list[list] = []
    for price, i in pts:
        if out and price - out[-1][-1][0] <= tol:
            out[-1].append((price, i))
        else:
            out.append([(price, i)])
    zs = []
    for cl in out:
        prices = [p for p, _ in cl]
        zs.append({"level": sum(prices) / len(prices), "lo": min(prices), "hi": max(prices),
                   "touches": len(cl), "last": max(i for _, i in cl)})
    return zs


def levels_section(ch: Chart, zs: list[dict]) -> tuple[list[dict], dict]:
    c = ch.c[-1]
    items = []
    sup = sorted([z for z in zs if z["level"] < c], key=lambda z: -z["level"])
    res = sorted([z for z in zs if z["level"] > c], key=lambda z: z["level"])
    key = {"support": sup[:3], "resistance": res[:3]}

    def zt(z):
        return f"{_p(z['level'])} ({z['touches']} touch{'es' if z['touches'] != 1 else ''})"
    if sup:
        s = sup[0]
        d = _pct(s["level"], c)
        near = abs(c - s["level"]) <= ch.A
        items.append(F("Support", f"{_p(s['level'])} ({d:+.1f}%)",
                       f"Nearest floor where buyers stepped in before: {zt(s)}. "
                       + ("Price is sitting right on it." if near else
                          f"{', '.join(zt(z) for z in sup[1:3])} below that." if len(sup) > 1 else ""),
                       "bull" if near else "neutral", weight=2 if near else 1, level=s["level"]))
    else:
        items.append(_absent("Support", "No swing low below the price in the last year — price is at or near its lows."))
    if res:
        r = res[0]
        d = _pct(r["level"], c)
        near = abs(r["level"] - c) <= ch.A
        items.append(F("Resistance", f"{_p(r['level'])} ({d:+.1f}%)",
                       f"Nearest ceiling where sellers showed up before: {zt(r)}. "
                       + ("Price is pressing against it." if near else
                          f"Then {', '.join(zt(z) for z in res[1:3])}." if len(res) > 1 else ""),
                       "bear" if near else "neutral", weight=2 if near else 1, level=r["level"]))
    else:
        items.append(_absent("Resistance", "No swing high above the price in the last year — blue sky above."))

    # breakout & retest / breakdown & retest (last 20 bars)
    found = None
    for z in [z for z in zs if z["touches"] >= 2]:
        lvl = z["level"]
        for i in range(max(1, ch.n - 15), ch.n):
            if ch.c[i - 1] <= lvl and ch.c[i] > lvl + 0.5 * ch.A:
                after = range(i + 1, ch.n)
                if after and min(ch.l[j] for j in after) <= lvl + 0.3 * ch.A and ch.c[-1] > lvl:
                    found = ("bull", lvl, i)
            if ch.c[i - 1] >= lvl and ch.c[i] < lvl - 0.5 * ch.A:
                after = range(i + 1, ch.n)
                if after and max(ch.h[j] for j in after) >= lvl - 0.3 * ch.A and ch.c[-1] < lvl:
                    found = ("bear", lvl, i)
    if found:
        b, lvl, i = found
        items.append(F("Breakout and retest" if b == "bull" else "Breakdown and retest",
                       "Retest held",
                       (f"Broke above {_p(lvl)} on {_day(ch.t[i])}, came back to test it and held — old resistance acting as support."
                        if b == "bull" else
                        f"Broke below {_p(lvl)} on {_day(ch.t[i])}, rallied back to test it and failed — old support acting as resistance."),
                       b, ts=ch.t[i], weight=3, level=lvl))
    else:
        items.append(_absent("Breakout and retest", "No recent break of a level followed by a successful retest."))

    # false breakout / trap (last 5 bars): closed through a zone, closed back
    trap = None
    for z in [z for z in zs if z["touches"] >= 2]:
        lvl = z["level"]
        for i in range(max(1, ch.n - 6), ch.n - 1):
            if ch.c[i] > lvl >= ch.c[i - 1] and any(ch.c[j] < lvl for j in range(i + 1, min(ch.n, i + 4))):
                trap = ("bear", "Bull trap", lvl, i)
            if ch.c[i] < lvl <= ch.c[i - 1] and any(ch.c[j] > lvl for j in range(i + 1, min(ch.n, i + 4))):
                trap = ("bull", "Bear trap", lvl, i)
    if trap:
        b, nm, lvl, i = trap
        items.append(F("False breakout", nm,
                       (f"Closed above {_p(lvl)} on {_day(ch.t[i])} then fell back under it — the breakout failed."
                        if b == "bear" else
                        f"Closed below {_p(lvl)} on {_day(ch.t[i])} then recovered above it — the breakdown failed."),
                       b, ts=ch.t[i], weight=2, level=lvl))
    else:
        items.append(_absent("False breakout", "No failed break of a level in the last week."))
    return items, key


# ---------------------------------------------------------------------------
# 3. Gaps and fair value gaps
# ---------------------------------------------------------------------------
def gaps_section(ch: Chart, trend: str) -> tuple[list[dict], list[dict]]:
    gaps = []
    for i in range(max(21, ch.n - 120), ch.n):
        up = ch.o[i] > ch.h[i - 1]
        dn = ch.o[i] < ch.l[i - 1]
        if not (up or dn):
            continue
        top, bot = (ch.o[i], ch.h[i - 1]) if up else (ch.l[i - 1], ch.o[i])
        size = _pct(ch.o[i], ch.c[i - 1])
        if abs(size) < 1.0:
            continue
        vr = ch.vrel(i)
        rng_hi = max(ch.h[i - 21:i - 1])
        rng_lo = min(ch.l[i - 21:i - 1])
        s50 = ch.sma50[i - 1]
        ext = s50 and abs(_pct(ch.c[i - 1], s50)) > 15
        # fill tracking after the gap day
        later = range(i, ch.n)
        if up:
            low_after = min(ch.l[j] for j in later)
            filled = low_after <= bot
            part = (top - max(low_after, bot)) / (top - bot) if top > bot else 1
        else:
            hi_after = max(ch.h[j] for j in later)
            filled = hi_after >= top
            part = (min(hi_after, top) - bot) / (top - bot) if top > bot else 1
        part = max(0.0, min(1.0, part))
        # classification
        if (up and ch.o[i] > rng_hi or dn and ch.o[i] < rng_lo) and (vr or 0) >= 1.5:
            kind = "Breakaway"
        elif ext and ((up and trend == "up") or (dn and trend == "down")) and filled:
            kind = "Exhaustion"
        elif (up and trend == "up") or (dn and trend == "down"):
            kind = "Runaway"
        else:
            kind = "Common"
        gaps.append({"i": i, "up": up, "top": top, "bot": bot, "size": size, "kind": kind,
                     "filled": filled, "part": part, "vr": vr})
    items = []
    fill_txt = lambda g: ("fully filled" if g["filled"] else
                          f"{g['part'] * 100:.0f}% filled" if g["part"] > 0.02 else "still open")
    def vol_txt(g):
        return f" on {g['vr']:.1f}× normal volume" if g["vr"] else ""

    meaning = {
        "Breakaway": "broke out of its 20-day range on heavy volume — can start a new trend",
        "Runaway": "in the direction of the trend — a sign of strong momentum",
        "Exhaustion": "late in a stretched move and already filled — the trend may be tiring",
        "Common": "inside the normal range — these often fill and mean little alone",
    }
    for kind in ("Breakaway", "Runaway", "Exhaustion", "Common"):
        g = next((g for g in reversed(gaps) if g["kind"] == kind), None)
        nm = f"{kind} gap"
        if not g:
            items.append(_absent(nm, "None in the last six months."))
            continue
        b = ("bull" if g["up"] else "bear") if kind in ("Breakaway", "Runaway") else \
            ("bear" if g["up"] else "bull") if kind == "Exhaustion" else "neutral"
        age = ch.n - 1 - g["i"]
        items.append(F(nm, f"Gap {'up' if g['up'] else 'down'} {abs(g['size']):.1f}%" + (
                           "" if age <= 20 else f" · {age} sessions ago"),
                       f"{_day(ch.t[g['i']])}: gapped {'up' if g['up'] else 'down'} between {_p(g['bot'])} and {_p(g['top'])}"
                       f"{vol_txt(g)} — {meaning[kind]}. Gap is {fill_txt(g)}.",
                       b, ts=ch.t[g["i"]],
                       active=age <= (5 if kind == "Common" else 20) and not (kind == "Common" and g["filled"]),
                       weight=2 if kind in ("Breakaway", "Exhaustion") else 1))
    open_gaps = [g for g in gaps if not g["filled"] and ch.n - 1 - g["i"] <= 60]
    if open_gaps:
        g = open_gaps[-1]
        items.append(F("Unfilled gap", f"{_p(g['bot'])} – {_p(g['top'])}",
                       f"The {_day(ch.t[g['i']])} gap {'up' if g['up'] else 'down'} is {fill_txt(g)}. Open gaps often act as "
                       f"{'support' if g['up'] else 'resistance'} and sometimes get filled later.",
                       "neutral", ts=ch.t[g["i"]]))
    else:
        items.append(_absent("Unfilled gap", "Every recent gap has been filled."))
    # fair value gaps (3-candle imbalance), last 40 bars, unfilled only
    fvgs = []
    for i in range(max(2, ch.n - 40), ch.n):
        if ch.h[i - 2] < ch.l[i]:
            lo, hi = ch.h[i - 2], ch.l[i]
            touched = any(ch.l[j] <= hi for j in range(i + 1, ch.n))
            fvgs.append({"bull": True, "lo": lo, "hi": hi, "i": i, "touched": touched})
        if ch.l[i - 2] > ch.h[i]:
            lo, hi = ch.h[i], ch.l[i - 2]
            touched = any(ch.h[j] >= lo for j in range(i + 1, ch.n))
            fvgs.append({"bull": False, "lo": lo, "hi": hi, "i": i, "touched": touched})
    fresh = [f for f in fvgs if not f["touched"] and (f["hi"] - f["lo"]) >= 0.5 * ch.A]
    if fresh:
        f = fresh[-1]
        items.append(F("Fair value gap (FVG)", f"{'Bullish' if f['bull'] else 'Bearish'} {_p(f['lo'])} – {_p(f['hi'])}",
                       f"Three-candle imbalance from {_day(ch.t[f['i']])} that price hasn't revisited. "
                       f"Some traders expect price to come back into it; it doesn't have to.",
                       "bull" if f["bull"] else "bear", ts=ch.t[f["i"]]))
    else:
        items.append(_absent("Fair value gap (FVG)", "No untouched three-candle imbalance in the last two months."))
    zones_out = [{"lo": g["bot"], "hi": g["top"], "kind": "gap", "bull": g["up"], "ts": ch.t[g["i"]]}
                 for g in open_gaps[-3:]] + \
                [{"lo": f["lo"], "hi": f["hi"], "kind": "fvg", "bull": f["bull"], "ts": ch.t[f["i"]]}
                 for f in fresh[-2:]]
    markers = [{"ts": ch.t[g["i"]], "pos": "below" if g["up"] else "above",
                "bias": "bull" if g["up"] else "bear", "text": "Gap"} for g in gaps[-6:]]
    return items, {"zones": zones_out, "markers": markers}


# ---------------------------------------------------------------------------
# 4. Candlestick patterns
# ---------------------------------------------------------------------------
def candle_hits(ch: Chart, i: int, near_support: bool, near_resist: bool) -> list[tuple]:
    """Patterns completing on bar i -> [(name, bias, text)]."""
    hits = []
    if i < 3:
        return hits
    rng, body = ch.rng(i), ch.body(i)
    if rng <= 0:
        return hits
    small = body <= rng * 0.35
    # hammer / shooting star (context: after a decline / rise)
    if small and ch.lower(i) >= 2 * max(body, rng * 0.05) and ch.upper(i) <= max(body, rng * 0.1) and ch.fell(i):
        hits.append(("Hammer", "bull", "Long lower wick after a decline — sellers pushed it down, buyers pushed it back"
                     + (", right at support." if near_support else ".")))
    if small and ch.upper(i) >= 2 * max(body, rng * 0.05) and ch.lower(i) <= max(body, rng * 0.1) and ch.rose(i):
        hits.append(("Shooting star", "bear", "Long upper wick after a rise — buyers pushed it up, sellers pushed it back"
                     + (", right at resistance." if near_resist else ".")))
    # doji
    if body <= rng * 0.1:
        hits.append(("Doji", "neutral", "Open and close almost equal — indecision, not a reversal by itself."))
    # engulfing
    p = i - 1
    if ch.red(p) and ch.green(i) and ch.o[i] <= ch.c[p] and ch.c[i] >= ch.o[p] and body > ch.body(p):
        hits.append(("Bullish engulfing", "bull", "Today's up candle swallowed yesterday's down candle — buyers took control."))
    if ch.green(p) and ch.red(i) and ch.o[i] >= ch.c[p] and ch.c[i] <= ch.o[p] and body > ch.body(p):
        hits.append(("Bearish engulfing", "bear", "Today's down candle swallowed yesterday's up candle — sellers took control."))
    # piercing line / dark cloud cover
    mid_p = (ch.o[p] + ch.c[p]) / 2
    if ch.red(p) and ch.green(i) and ch.o[i] < ch.l[p] and mid_p < ch.c[i] < ch.o[p] and ch.body(p) > 0.5 * ch.rng(p):
        hits.append(("Piercing line", "bull", "Opened below yesterday's low and closed past the middle of its body."))
    if ch.green(p) and ch.red(i) and ch.o[i] > ch.h[p] and ch.o[p] < ch.c[i] < mid_p and ch.body(p) > 0.5 * ch.rng(p):
        hits.append(("Dark cloud cover", "bear", "Opened above yesterday's high and closed below the middle of its body."))
    # morning / evening star (3 bars)
    a, b = i - 2, i - 1
    if ch.red(a) and ch.body(a) > 0.5 * ch.rng(a) and ch.body(b) <= 0.3 * max(ch.rng(b), 1e-9) and \
            ch.green(i) and ch.c[i] > (ch.o[a] + ch.c[a]) / 2 and ch.fell(a):
        hits.append(("Morning star", "bull", "Big down day, small indecision day, then a strong up day — a classic bottoming pattern."))
    if ch.green(a) and ch.body(a) > 0.5 * ch.rng(a) and ch.body(b) <= 0.3 * max(ch.rng(b), 1e-9) and \
            ch.red(i) and ch.c[i] < (ch.o[a] + ch.c[a]) / 2 and ch.rose(a):
        hits.append(("Evening star", "bear", "Big up day, small indecision day, then a strong down day — a classic topping pattern."))
    # three soldiers / crows
    tri = [i - 2, i - 1, i]
    if all(ch.green(k) and ch.body(k) > 0.5 * ch.rng(k) for k in tri) and ch.c[i] > ch.c[i - 1] > ch.c[i - 2]:
        hits.append(("Three white soldiers", "bull", "Three strong up days in a row, each closing higher."))
    if all(ch.red(k) and ch.body(k) > 0.5 * ch.rng(k) for k in tri) and ch.c[i] < ch.c[i - 1] < ch.c[i - 2]:
        hits.append(("Three black crows", "bear", "Three strong down days in a row, each closing lower."))
    # inside / outside bar
    if ch.h[i] < ch.h[p] and ch.l[i] > ch.l[p]:
        hits.append(("Inside bar", "neutral", "Today's range fits inside yesterday's — the market is pausing; watch which side breaks."))
    if ch.h[i] > ch.h[p] and ch.l[i] < ch.l[p]:
        hits.append(("Outside bar", "bull" if ch.green(i) else "bear",
                     f"Today's range swallowed yesterday's and closed {'up' if ch.green(i) else 'down'} — volatility expanding."))
    return hits


SHORT = {"Hammer": "Hammer", "Shooting star": "Star", "Bullish engulfing": "Engulf",
         "Bearish engulfing": "Engulf", "Morning star": "M-star", "Evening star": "E-star",
         "Three white soldiers": "3 up", "Three black crows": "3 down", "Piercing line": "Pierce",
         "Dark cloud cover": "Cloud", "Outside bar": "Outside"}

CANDLE_NAMES = ["Hammer", "Shooting star", "Bullish engulfing", "Bearish engulfing", "Doji",
                "Morning star", "Evening star", "Three white soldiers", "Three black crows",
                "Piercing line", "Dark cloud cover", "Inside bar", "Outside bar"]


def candles_section(ch: Chart, sup: list[dict], res: list[dict]) -> tuple[list[dict], list[dict]]:
    def near(i, zs):
        return any(abs(ch.l[i] - z["level"]) <= ch.A or abs(ch.h[i] - z["level"]) <= ch.A for z in zs[:2])
    latest: dict[str, tuple] = {}
    markers = []
    for i in range(max(3, ch.n - 30), ch.n):
        for name, bias, txt in candle_hits(ch, i, near(i, sup), near(i, res)):
            latest[name] = (i, bias, txt)
            if name not in ("Inside bar", "Doji"):
                markers.append({"ts": ch.t[i], "pos": "below" if bias == "bull" else "above",
                                "bias": bias, "text": SHORT.get(name, name)})
    items = []
    for name in CANDLE_NAMES:
        if name in latest:
            i, bias, txt = latest[name]
            age = ch.n - 1 - i
            when = "Today" if age == 0 else "Yesterday" if age == 1 else f"{age} sessions ago"
            minor = name in ("Doji", "Inside bar", "Outside bar")
            items.append(F(name, when, f"{_day(ch.t[i])}: {txt}", bias, active=age <= (1 if minor else 5), ts=ch.t[i],
                           weight=2 if age <= 1 and bias != "neutral" else 1))
        else:
            items.append(_absent(name, "Not in the last 30 sessions."))
    items.sort(key=lambda x: (not x["active"], x["ts"] is None, -(x["ts"] or 0)))
    return items, markers[-12:]


# ---------------------------------------------------------------------------
# 5. Chart patterns
# ---------------------------------------------------------------------------
def _double(ch: Chart, top: bool) -> dict | None:
    piv = ch.ph if top else ch.pl
    arr = ch.h if top else ch.l
    if len(piv) < 2:
        return None
    a, b = piv[-2], piv[-1]
    if b - a < 8 or ch.n - b > 40:
        return None
    pa, pb = arr[a], arr[b]
    if abs(pa - pb) > max(0.5 * ch.A, 0.015 * pa):
        return None
    seg = range(a + 1, b)
    neck = min(ch.l[k] for k in seg) if top else max(ch.h[k] for k in seg)
    depth = abs(_pct(neck, (pa + pb) / 2))
    if depth < 3:
        return None
    after = range(b + 1, ch.n)
    ever = any((ch.c[k] < neck) if top else (ch.c[k] > neck) for k in after)
    now = (ch.c[-1] < neck) if top else (ch.c[-1] > neck)
    return {"a": a, "b": b, "level": (pa + pb) / 2, "neck": neck,
            "confirmed": now, "failed": ever and not now}


def _hs(ch: Chart, inverse: bool) -> dict | None:
    piv = ch.pl if inverse else ch.ph
    arr = ch.l if inverse else ch.h
    if len(piv) < 3:
        return None
    p1, p2, p3 = piv[-3:]
    if ch.n - p3 > 30 or p3 - p1 > 120:
        return None
    s1, hd, s2 = arr[p1], arr[p2], arr[p3]
    if not inverse and not (hd > max(s1, s2) * 1.02 and abs(s1 - s2) / hd <= 0.04):
        return None
    if inverse and not (hd < min(s1, s2) * 0.98 and abs(s1 - s2) / max(s1, s2) <= 0.04):
        return None
    if inverse:
        n1 = max(ch.h[k] for k in range(p1, p2 + 1))
        n2 = max(ch.h[k] for k in range(p2, p3 + 1))
    else:
        n1 = min(ch.l[k] for k in range(p1, p2 + 1))
        n2 = min(ch.l[k] for k in range(p2, p3 + 1))
    neck = (n1 + n2) / 2
    ever = any((ch.c[k] > neck) if inverse else (ch.c[k] < neck) for k in range(p3 + 1, ch.n))
    now = (ch.c[-1] > neck) if inverse else (ch.c[-1] < neck)
    return {"p": (p1, p2, p3), "neck": neck, "head": hd, "confirmed": now,
            "failed": ever and not now}


def _lines(ch: Chart, window: int = 60):
    """Trendlines through recent swing highs / lows (k=2 pivots)."""
    start = ch.n - window
    hs = [i for i in ch.ph2 if i >= start][-5:]
    ls = [i for i in ch.pl2 if i >= start][-5:]
    if len(hs) < 2 or len(ls) < 2:
        return None
    bh, ah = linreg(hs, [ch.h[i] for i in hs])
    bl, al = linreg(ls, [ch.l[i] for i in ls])
    px = ch.c[-1]
    first = min(hs[0], ls[0])
    last = ch.n - 1
    up_now, lo_now = ah + bh * last, al + bl * last
    up_then, lo_then = ah + bh * first, al + bl * first
    if up_now <= lo_now:
        return None
    return {"sh": bh / px * 100, "sl": bl / px * 100, "upper": up_now, "lower": lo_now,
            "w_now": up_now - lo_now, "w_then": up_then - lo_then, "first": first,
            "hs": hs, "ls": ls, "ah": ah, "bh": bh, "al": al, "bl": bl}


def _shape(L: dict) -> tuple[str, str] | None:
    flat = 0.05  # % of price per bar
    sh, sl = L["sh"], L["sl"]
    conv = L["w_now"] < 0.6 * L["w_then"]
    par = abs(sh - sl) < 0.04
    if abs(sh) < flat and abs(sl) < flat:
        return "Rectangle (range)", "neutral"
    if abs(sh) < flat and sl > flat:
        return "Ascending triangle", "bull"
    if sh < -flat and abs(sl) < flat:
        return "Descending triangle", "bear"
    if sh < -flat and sl > flat:
        return "Symmetrical triangle", "neutral"
    if sh > flat and sl > flat:
        if conv and sl > sh:
            return "Rising wedge", "bear"
        if par:
            return "Rising channel", "bull"
    if sh < -flat and sl < -flat:
        if conv and sh < sl:
            return "Falling wedge", "bull"
        if par:
            return "Falling channel", "bear"
    return None


def _flag(ch: Chart) -> dict | None:
    """Sharp pole (>= 8% in <= 10 bars) then a tight 3-15 bar consolidation."""
    for end in range(ch.n - 4, max(ch.n - 20, 11), -1):
        for ln in range(3, 11):
            st = end - ln
            if st < 0:
                break
            move = _pct(ch.c[end], ch.c[st])
            if abs(move) < 8:
                continue
            seg = range(end + 1, ch.n)
            if not (3 <= len(seg) <= 15):
                continue
            hi = max(ch.h[k] for k in seg)
            lo = min(ch.l[k] for k in seg)
            pole = abs(ch.c[end] - ch.c[st])
            if hi - lo > 0.5 * pole:
                continue
            bull = move > 0
            retrace = (ch.c[end] - lo) / pole if bull else (hi - ch.c[end]) / pole
            if retrace > 0.5:
                continue
            hs = [ch.h[k] for k in seg]
            ls = [ch.l[k] for k in seg]
            xs = list(range(len(hs)))
            sh, _ = linreg(xs, hs)
            sl, _ = linreg(xs, ls)
            pennant = sh < 0 < sl
            brk = ch.c[-1] > max(hs[:-1] or hs) if bull else ch.c[-1] < min(ls[:-1] or ls)
            return {"bull": bull, "pennant": pennant, "move": move, "st": st, "end": end,
                    "hi": hi, "lo": lo, "breakout": brk}
    return None


def _cup(ch: Chart) -> dict | None:
    if ch.n < 60:
        return None
    w0 = max(0, ch.n - 160)
    left_rng = range(w0, ch.n - 35)
    if not left_rng:
        return None
    L = max(left_rng, key=lambda k: ch.h[k])
    if ch.n - L < 30:
        return None
    B = min(range(L + 1, ch.n), key=lambda k: ch.l[k])
    depth = _pct(ch.h[L], ch.l[B])
    if not (12 <= abs(_pct(ch.l[B], ch.h[L])) <= 40) or B >= ch.n - 5:
        return None
    R = max(range(B + 1, ch.n), key=lambda k: ch.h[k])
    if ch.h[R] < 0.95 * ch.h[L]:
        return None
    # rounded, not V: bottom third of the cup lasts a while
    floor = ch.l[B] + (ch.h[L] - ch.l[B]) / 3
    low_bars = sum(1 for k in range(L, R + 1) if ch.l[k] <= floor)
    if low_bars < 10 or R - L < 40:
        return None
    if ch.c[-1] < 0.93 * ch.h[R]:
        return None
    handle = range(R + 1, ch.n)
    has_handle = 3 <= len(handle) <= 25 and min(ch.l[k] for k in handle) >= ch.h[R] * 0.88
    return {"L": L, "B": B, "R": R, "rim": ch.h[R], "depth": depth, "handle": has_handle,
            "breakout": ch.c[-1] > ch.h[R] and len(handle) > 0}


def patterns_section(ch: Chart) -> tuple[list[dict], list[dict], dict | None]:
    items, lines = [], []
    c = ch.c[-1]
    for top in (True, False):
        d = _double(ch, top)
        nm = "Double top" if top else "Double bottom"
        if d:
            items.append(F(nm, "Confirmed" if d["confirmed"] else "Failed" if d["failed"] else "Forming",
                           f"Two {'peaks' if top else 'lows'} near {_p(d['level'])} ({_day(ch.t[d['a']])} and {_day(ch.t[d['b']])}). "
                           f"Neckline {_p(d['neck'])}: "
                           + ("price has closed through it — the reversal is confirmed." if d["confirmed"] else
                              "price broke it but has since moved back — the pattern failed." if d["failed"] else
                              f"a close {'below' if top else 'above'} it would confirm the reversal."),
                           "neutral" if d["failed"] else ("bear" if top else "bull"),
                           weight=3 if d["confirmed"] else 2, active=not d["failed"],
                           ts=ch.t[d["b"]], level=d["neck"]))
            if not d["failed"]:
                lines.append({"price": d["neck"], "title": "Neckline", "bias": "bear" if top else "bull"})
        else:
            items.append(_absent(nm))
    for inv in (False, True):
        h = _hs(ch, inv)
        nm = "Inverse head and shoulders" if inv else "Head and shoulders"
        if h:
            items.append(F(nm, "Confirmed" if h["confirmed"] else "Failed" if h["failed"] else "Forming",
                           f"Three {'lows' if inv else 'peaks'} with the middle one {'deepest' if inv else 'highest'} "
                           f"({_p(h['head'])}). Neckline {_p(h['neck'])} — "
                           + ("price is through it — confirmed." if h["confirmed"] else
                              "price broke it but has since moved back — the pattern failed." if h["failed"] else
                              f"a close {'above' if inv else 'below'} it would confirm."),
                           "neutral" if h["failed"] else ("bull" if inv else "bear"),
                           weight=3 if h["confirmed"] else 2, active=not h["failed"],
                           ts=ch.t[h["p"][2]], level=h["neck"]))
            if not h["failed"]:
                lines.append({"price": h["neck"], "title": "H&S neckline", "bias": "bull" if inv else "bear"})
        else:
            items.append(_absent(nm))
    shape_names = ["Ascending triangle", "Descending triangle", "Symmetrical triangle",
                   "Rising wedge", "Falling wedge", "Rising channel", "Falling channel", "Rectangle (range)"]
    L = _lines(ch)
    sh = _shape(L) if L else None
    tl = None
    if sh:
        nm, bias = sh
        brk = "up" if c > L["upper"] else "down" if c < L["lower"] else None
        status = ("Broke out up" if brk == "up" else "Broke down" if brk == "down" else "Inside the pattern")
        what = {
            "Ascending triangle": "flat ceiling with rising lows — buyers keep stepping in higher",
            "Descending triangle": "flat floor with falling highs — sellers keep pressing lower",
            "Symmetrical triangle": "highs falling and lows rising — a coil that can break either way",
            "Rising wedge": "both lines rising but converging — momentum fading, often breaks down",
            "Falling wedge": "both lines falling but converging — selling fading, often breaks up",
            "Rising channel": "price climbing between two parallel lines",
            "Falling channel": "price sliding between two parallel lines",
            "Rectangle (range)": "flat top and flat bottom — trade the range or wait for a break",
        }[nm]
        items.append(F(nm, status, f"Over the last ~{ch.n - L['first']} sessions: {what}. Upper line {_p(L['upper'])}, "
                       f"lower line {_p(L['lower'])}.",
                       ("bull" if brk == "up" else "bear" if brk == "down" else bias), weight=2))
        tl = {"upper": [(ch.t[L["first"]], L["ah"] + L["bh"] * L["first"]), (ch.t[-1], L["upper"])],
              "lower": [(ch.t[L["first"]], L["al"] + L["bl"] * L["first"]), (ch.t[-1], L["lower"])]}
    for nm in shape_names:
        if not sh or sh[0] != nm:
            items.append(_absent(nm))
    fl = _flag(ch)
    for nm, want_pennant in (("Bull flag", False), ("Bear flag", False), ("Pennant", True)):
        ok = fl and fl["pennant"] == want_pennant and (want_pennant or fl["bull"] == (nm == "Bull flag"))
        if ok:
            direction = "up" if fl["bull"] else "down"
            items.append(F(nm if nm != "Pennant" else f"{'Bullish' if fl['bull'] else 'Bearish'} pennant",
                           "Breaking out" if fl["breakout"] else "Consolidating",
                           f"A sharp {abs(fl['move']):.0f}% move {direction} ({_day(ch.t[fl['st']])}–{_day(ch.t[fl['end']])}), "
                           f"then a tight pause between {_p(fl['lo'])} and {_p(fl['hi'])}. Traders watch for a break "
                           f"{'above' if fl['bull'] else 'below'} {_p(fl['hi'] if fl['bull'] else fl['lo'])}.",
                           "bull" if fl["bull"] else "bear", weight=2, ts=ch.t[fl["end"]]))
        else:
            items.append(_absent(nm))
    cup = _cup(ch)
    if cup and cup["handle"]:
        items.append(F("Cup and handle", "Breaking out" if cup["breakout"] else "Handle forming",
                       f"Rounded base from {_day(ch.t[cup['L']])} ({abs(cup['depth']):.0f}% deep), back near the rim at {_p(cup['rim'])}, "
                       f"then a small pullback. A close above {_p(cup['rim'])} is the classic breakout.",
                       "bull", weight=2, level=cup["rim"]))
        lines.append({"price": cup["rim"], "title": "Cup rim", "bias": "bull"})
    else:
        items.append(_absent("Cup and handle"))
    if cup and not cup["handle"]:
        items.append(F("Rounded bottom", "Forming", f"A gradual U-shaped base since {_day(ch.t[cup['L']])}, "
                       f"now back near {_p(cup['rim'])}.", "bull", weight=1))
    else:
        items.append(_absent("Rounded bottom"))
    items.sort(key=lambda x: not x["active"])
    return items, lines, tl


# ---------------------------------------------------------------------------
# 6. Volume
# ---------------------------------------------------------------------------
def volume_profile(ch: Chart, lookback: int = 120, bins: int = 24) -> dict | None:
    s = max(0, ch.n - lookback)
    hi, lo = max(ch.h[s:]), min(ch.l[s:])
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for i in range(s, ch.n):
        a = int((ch.l[i] - lo) / step)
        b = int((ch.h[i] - lo) / step)
        a, b = max(0, min(bins - 1, a)), max(0, min(bins - 1, b))
        share = ch.v[i] / (b - a + 1)
        for k in range(a, b + 1):
            vol[k] += share
    poc = max(range(bins), key=lambda k: vol[k])
    total = sum(vol)
    lo_i = hi_i = poc
    acc = vol[poc]
    while acc < 0.7 * total and (lo_i > 0 or hi_i < bins - 1):
        down = vol[lo_i - 1] if lo_i > 0 else -1
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            acc += vol[hi_i]
        else:
            lo_i -= 1
            acc += vol[lo_i]
    mid = lambda k: lo + (k + 0.5) * step
    return {"poc": mid(poc), "vah": lo + (hi_i + 1) * step, "val": lo + lo_i * step,
            "bins": [{"price": mid(k), "vol": vol[k]} for k in range(bins)]}


def volume_section(ch: Chart, minutes: float | None, partial: bool) -> tuple[list[dict], dict | None]:
    items = []
    med = ch.vmed[-1]
    if med:
        raw = ch.v[-1] / med
        if partial and minutes is not None:
            paced = paced_volume_ratio(ch.v[-1], med, minutes)
            frac = expected_volume_fraction(minutes)
            items.append(F("Relative volume (today, paced)", f"{paced:.1f}× normal pace",
                           f"Today's volume so far is {raw:.2f}× a full normal day — but by this time a stock usually "
                           f"trades only ~{frac * 100:.0f}% of its day. Adjusted for the time of day that is "
                           f"{paced:.1f}× the normal pace"
                           + (" — busy." if paced >= 1.5 else " — quiet." if paced < 0.7 else " — about normal."),
                           "neutral", weight=1))
        else:
            items.append(F("Relative volume", f"{raw:.1f}× normal",
                           "Last session's volume vs the 20-day median"
                           + (" — heavy, a lot of participation." if raw >= 1.5 else
                              " — light, little conviction." if raw < 0.7 else " — about normal."),
                           "neutral"))
    # confirmation of the latest 20-day breakout / breakdown
    conf = None
    for i in range(max(21, ch.n - 5), ch.n):
        p_hi, p_lo = max(ch.h[i - 20:i]), min(ch.l[i - 20:i])
        vr = ch.vrel(i) or 0
        if ch.c[i] > p_hi:
            conf = ("bull", i, vr)
        elif ch.c[i] < p_lo:
            conf = ("bear", i, vr)
    if conf:
        b, i, vr = conf
        ok = vr >= 1.5
        items.append(F("Volume confirmation", "Confirmed" if ok else "Weak volume",
                       f"{_day(ch.t[i])}: closed at a 20-day {'high' if b == 'bull' else 'low'} on {vr:.1f}× normal volume — "
                       + ("strong participation backs the move." if ok else "light volume, so the move is less convincing."),
                       b if ok else "neutral", ts=ch.t[i], weight=2 if ok else 1))
    else:
        items.append(_absent("Volume confirmation", "No 20-day breakout or breakdown in the last week to confirm."))
    # price/volume divergence over the last 10 vs prior 20 sessions
    if ch.n >= 40:
        v10 = sum(ch.v[-10:]) / 10
        v20 = sum(ch.v[-30:-10]) / 20
        hi20, lo20 = max(ch.h[-20:]), min(ch.l[-20:])
        if ch.c[-1] >= 0.98 * hi20 and v20 and v10 < 0.8 * v20:
            items.append(F("Price–volume divergence", "Bearish",
                           f"Price is near its 20-day high but volume has dropped to {v10 / v20:.0%} of the prior pace — fewer buyers are pushing it up.",
                           "bear", weight=2))
        elif ch.c[-1] <= 1.02 * lo20 and v20 and v10 < 0.8 * v20:
            items.append(F("Price–volume divergence", "Bullish",
                           f"Price is near its 20-day low but volume has dropped to {v10 / v20:.0%} of the prior pace — selling is drying up.",
                           "bull", weight=2))
        else:
            items.append(_absent("Price–volume divergence", "Volume is moving in step with price."))
    # climax
    cl = None
    for i in range(max(21, ch.n - 10), ch.n):
        vr = ch.vrel(i) or 0
        if vr >= 3 and ch.rng(i) >= 2 * (ch.atr[i - 1] or ch.A):
            cl = (i, vr)
    if cl:
        i, vr = cl
        down = ch.c[i] < ch.o[i]
        items.append(F("Volume climax", "Selling climax" if down and ch.fell(i) else "Buying climax" if not down and ch.rose(i) else "Climax bar",
                       f"{_day(ch.t[i])}: {vr:.1f}× normal volume on a very wide {'down' if down else 'up'} day — "
                       + ("panic selling often marks a low (capitulation)." if down else "a blow-off can mark a short-term top."),
                       "bull" if down else "bear", ts=ch.t[i], weight=2))
    else:
        items.append(_absent("Volume climax", "No extreme volume on an extreme range in the last two weeks."))
    # OBV
    obv = obv_s(ch.c, ch.v)
    if ch.n > 25:
        ob_chg = obv[-1] - obv[-21]
        px_chg = ch.c[-1] - ch.c[-21]
        if ob_chg > 0 and px_chg > 0:
            st, txt, b = "Confirming", "OBV and price both rose over 20 sessions — volume supports the advance.", "bull"
        elif ob_chg < 0 and px_chg < 0:
            st, txt, b = "Confirming", "OBV and price both fell over 20 sessions — volume supports the decline.", "bear"
        elif ob_chg > 0:
            st, txt, b = "Bullish divergence", "Price fell but OBV rose — buyers may be accumulating.", "bull"
        else:
            st, txt, b = "Bearish divergence", "Price rose but OBV fell — the advance lacks volume.", "bear"
        items.append(F("On-balance volume (OBV)", st, txt, b))
    vp = volume_profile(ch)
    if vp:
        c = ch.c[-1]
        where = ("above the value area" if c > vp["vah"] else "below the value area" if c < vp["val"] else "inside the value area")
        items.append(F("Volume profile", f"POC {_p(vp['poc'])}",
                       f"Most trading in the last 6 months happened near {_p(vp['poc'])} (point of control). "
                       f"70% of volume traded between {_p(vp['val'])} and {_p(vp['vah'])}; price is {where}.",
                       "bull" if c > vp["vah"] else "bear" if c < vp["val"] else "neutral", level=vp["poc"]))
    return items, vp


# ---------------------------------------------------------------------------
# 7. Indicators
# ---------------------------------------------------------------------------
def indicators_section(ch: Chart, intraday: list[dict] | None) -> tuple[list[dict], dict]:
    c = ch.c[-1]
    h, l, cl = ch.h, ch.l, ch.c
    items = []
    extra: dict = {}
    s20, s50, s200 = ch.sma20[-1], ch.sma50[-1], ch.sma200[-1]
    if s50:
        items.append(F("SMA (20 / 50 / 200)", f"{'Above' if c > s50 else 'Below'} 50-day",
                       f"20-day {_p(s20)}, 50-day {_p(s50)}, 200-day {_p(s200)}. Price is "
                       f"{_pct(c, s50):+.1f}% from the 50-day"
                       + (f" and {_pct(c, s200):+.1f}% from the 200-day." if s200 else "."),
                       "bull" if c > s50 else "bear"))
    e9, e21 = ch.ema9[-1], ch.ema21[-1]
    if e9 and e21:
        items.append(F("EMA (9 / 21)", "9 above 21" if e9 > e21 else "9 below 21",
                       f"Fast averages that react quicker to recent prices: 9-day {_p(e9)}, 21-day {_p(e21)}. "
                       + ("Short-term momentum is up." if e9 > e21 else "Short-term momentum is down."),
                       "bull" if e9 > e21 else "bear"))
    r = ch.rsi[-1]
    if r is not None:
        items.append(F("RSI (14)", f"{r:.1f}",
                       "Momentum gauge from 0 to 100. "
                       + ("Below 30 — oversold; sellers may be exhausted." if r < 30 else
                          "Above 70 — overbought; buyers may be stretched." if r > 70 else
                          "Between 30 and 70 — neutral."),
                       "bull" if r < 30 else "bear" if r > 70 else "neutral", weight=2 if r < 30 or r > 70 else 1))
    ml, sg, hs = macd_s(cl)
    if hs[-1] is not None and hs[-2] is not None:
        cross = ("crossed above its signal line" if hs[-2] <= 0 < hs[-1] else
                 "crossed below its signal line" if hs[-2] >= 0 > hs[-1] else None)
        items.append(F("MACD (12, 26, 9)", "Bullish" if hs[-1] > 0 else "Bearish",
                       f"MACD {ml[-1]:.2f} vs signal {sg[-1]:.2f}. "
                       + (f"It just {cross}." if cross else
                          f"Histogram {'rising' if hs[-1] > hs[-2] else 'falling'} — momentum {'building' if (hs[-1] > hs[-2]) == (hs[-1] > 0) else 'fading'}."),
                       "bull" if hs[-1] > 0 else "bear", weight=2 if cross else 1))
    extra["macd"] = (ml, sg, hs)
    if ch.n >= 21:
        w = cl[-20:]
        mid = sum(w) / 20
        sd = (sum((x - mid) ** 2 for x in w) / 20) ** 0.5
        up, lo = mid + 2 * sd, mid - 2 * sd
        pb = (c - lo) / (up - lo) if up > lo else 0.5
        items.append(F("Bollinger Bands (20, 2)", f"%B {pb:.2f}",
                       f"Upper {_p(up)}, middle {_p(mid)}, lower {_p(lo)}. "
                       + ("Above the upper band — stretched up." if pb > 1 else
                          "Below the lower band — stretched down." if pb < 0 else
                          "Hugging the lower band." if pb < 0.2 else "Pressing the upper band." if pb > 0.8 else
                          "Inside the bands."),
                       "bull" if pb < 0 else "bear" if pb > 1 else "neutral"))
    items.append(F("ATR (14)", f"{_p(ch.A)} ({ch.A / c * 100:.1f}%)",
                   f"The stock's normal daily range is about {_p(ch.A)}. Use it to size stops: a stop inside one ATR "
                   "is likely to get hit by normal noise.", "neutral"))
    k, d = stoch_s(h, l, cl)
    if k[-1] is not None and d[-1] is not None:
        items.append(F("Stochastic (14, 3)", f"%K {k[-1]:.0f} / %D {d[-1]:.0f}",
                       "Where the close sits within the last 14 days' range. "
                       + ("Under 20 — oversold." if k[-1] < 20 else "Over 80 — overbought." if k[-1] > 80 else "Mid-range.")
                       + (" %K just crossed above %D." if k[-2] is not None and d[-2] is not None and k[-2] <= d[-2] and k[-1] > d[-1]
                          else " %K just crossed below %D." if k[-2] is not None and d[-2] is not None and k[-2] >= d[-2] and k[-1] < d[-1] else ""),
                       "bull" if k[-1] < 20 else "bear" if k[-1] > 80 else "neutral"))
    adx, pdi, mdi = adx_s(h, l, cl)
    if adx[-1] is not None:
        a = adx[-1]
        items.append(F("ADX (14)", f"{a:.0f} — {'strong' if a >= 25 else 'weak' if a < 20 else 'developing'} trend",
                       f"Trend strength, not direction. +DI {pdi[-1]:.0f} vs −DI {mdi[-1]:.0f}: "
                       f"{'buyers' if pdi[-1] > mdi[-1] else 'sellers'} have the edge"
                       + (" in a strong trend." if a >= 25 else " but the trend is weak — range tactics work better." if a < 20 else "."),
                       ("bull" if pdi[-1] > mdi[-1] else "bear") if a >= 20 else "neutral"))
    cci = cci_s(h, l, cl)
    if cci[-1] is not None:
        items.append(F("CCI (20)", f"{cci[-1]:.0f}",
                       "How far price is from its statistical average. "
                       + ("Above +100 — unusually strong." if cci[-1] > 100 else
                          "Below −100 — unusually weak." if cci[-1] < -100 else "Within ±100 — normal."),
                       "bear" if cci[-1] > 200 else "bull" if cci[-1] < -200 else "neutral"))
    if ch.n >= 22:
        dh, dl = max(h[-21:-1]), min(l[-21:-1])
        items.append(F("Donchian channel (20)", f"{_p(dl)} – {_p(dh)}",
                       "Highest high and lowest low of the prior 20 sessions. "
                       + ("Closed above it — a 20-day breakout." if c > dh else
                          "Closed below it — a 20-day breakdown." if c < dl else
                          f"Price is {((c - dl) / (dh - dl) * 100 if dh > dl else 50):.0f}% of the way up the channel."),
                       "bull" if c > dh else "bear" if c < dl else "neutral"))
    sar, tr = psar_s(h, l)
    if sar[-1] is not None:
        items.append(F("Parabolic SAR", f"{_p(sar[-1])} ({'below' if tr[-1] else 'above'} price)",
                       ("Dots below price — the uptrend is intact; the SAR can serve as a trailing stop."
                        if tr[-1] else "Dots above price — the downtrend is intact.")
                       + (" It flipped in the last 3 sessions." if any(tr[-k] != tr[-k - 1] for k in range(1, 4) if tr[-k - 1] is not None) else ""),
                       "bull" if tr[-1] else "bear"))
    extra["psar"] = sar
    if ch.n >= 78:
        def mid_(a, b, n, i):
            return (max(a[i - n + 1:i + 1]) + min(b[i - n + 1:i + 1])) / 2
        i = ch.n - 1
        tenkan, kijun = mid_(h, l, 9, i), mid_(h, l, 26, i)
        j = i - 26
        span_a = (mid_(h, l, 9, j) + mid_(h, l, 26, j)) / 2
        span_b = mid_(h, l, 52, j)
        top, bot = max(span_a, span_b), min(span_a, span_b)
        pos = "above the cloud" if c > top else "below the cloud" if c < bot else "inside the cloud"
        items.append(F("Ichimoku cloud", pos.capitalize(),
                       f"Cloud {_p(bot)} – {_p(top)}; conversion {_p(tenkan)} {'>' if tenkan > kijun else '<'} base {_p(kijun)}. "
                       + ("Above a cloud with conversion over base is the full bullish picture." if c > top and tenkan > kijun else
                          "Below the cloud with conversion under base is the full bearish picture." if c < bot and tenkan < kijun else
                          "Signals are mixed."),
                       "bull" if c > top else "bear" if c < bot else "neutral"))
    if ch.n >= 60:
        s = max(0, ch.n - 120)
        hi_i = max(range(s, ch.n), key=lambda q: h[q])
        lo_i = min(range(s, ch.n), key=lambda q: l[q])
        H, Lw = h[hi_i], l[lo_i]
        upmove = lo_i < hi_i
        lv = {r_: (H - (H - Lw) * r_) if upmove else (Lw + (H - Lw) * r_) for r_ in (0.236, 0.382, 0.5, 0.618, 0.786)}
        near = min(lv.items(), key=lambda kv: abs(kv[1] - c))
        extra["fib"] = lv
        items.append(F("Fibonacci retracement", f"Near {near[0] * 100:.1f}% ({_p(near[1])})",
                       f"Measured on the {'rally' if upmove else 'decline'} from {_p(Lw if upmove else H)} to {_p(H if upmove else Lw)}. "
                       f"Key levels: 38.2% {_p(lv[0.382])}, 50% {_p(lv[0.5])}, 61.8% {_p(lv[0.618])}.",
                       "neutral", level=near[1]))
    # VWAP: today's session from 1-minute bars
    if intraday:
        pv = sum((b["h"] + b["l"] + b["c"]) / 3 * (b.get("v") or 0) for b in intraday)
        vv = sum((b.get("v") or 0) for b in intraday)
        if vv:
            vw = pv / vv
            last = intraday[-1]["c"]
            items.append(F("VWAP (today)", f"{_p(vw)} ({'above' if last > vw else 'below'})",
                           "Average price paid today, weighted by volume. Day traders treat price above VWAP as buyers in "
                           "control and below as sellers in control.", "bull" if last > vw else "bear"))
    else:
        items.append(_absent("VWAP (today)", "Needs today's 1-minute bars — only for watchlist stocks during the session."))
    return items, extra


# ---------------------------------------------------------------------------
# 8. Liquidity / smart-money concepts (frameworks, not rules)
# ---------------------------------------------------------------------------
def liquidity_section(ch: Chart) -> tuple[list[dict], list[dict], list[dict]]:
    items, zones_out, markers = [], [], []
    c = ch.c[-1]
    tol = 0.25 * ch.A
    # equal highs / lows
    for kind, piv, arr in (("highs", ch.ph, ch.h), ("lows", ch.pl, ch.l)):
        rec = [i for i in piv if i >= ch.n - 40][-4:]
        pair = None
        for a, b in zip(rec, rec[1:]):
            if abs(arr[a] - arr[b]) <= tol:
                pair = (a, b)
        nm = f"Equal {kind}"
        if pair:
            lvl = (arr[pair[0]] + arr[pair[1]]) / 2
            taken = any((ch.h[k] > lvl) if kind == "highs" else (ch.l[k] < lvl) for k in range(pair[1] + 1, ch.n))
            items.append(F(nm, _p(lvl) + (" · already taken" if taken else ""),
                           f"Two swing {kind} at almost the same price ({_day(ch.t[pair[0]])}, {_day(ch.t[pair[1]])}). "
                           f"Stops tend to cluster just {'above' if kind == 'highs' else 'below'} them"
                           + (" — and price has already run through them." if taken else " — a magnet for a sweep."),
                           "neutral", level=lvl, active=not taken))
        else:
            items.append(_absent(nm, f"No matching swing {kind} recently."))
    # sweep of the latest swing high / low within the last 5 bars
    sweep = None
    for i in range(max(1, ch.n - 5), ch.n):
        prior_h = [p for p in ch.ph if p < i - 1]
        prior_l = [p for p in ch.pl if p < i - 1]
        if prior_h:
            lv = ch.h[prior_h[-1]]
            if ch.h[i] > lv + 0.2 * ch.A and ch.c[i] < lv - 0.1 * ch.A:
                sweep = ("bear", i, lv, "above the last swing high")
        if prior_l:
            lv = ch.l[prior_l[-1]]
            if ch.l[i] < lv - 0.2 * ch.A and ch.c[i] > lv + 0.1 * ch.A:
                sweep = ("bull", i, lv, "below the last swing low")
    if sweep:
        b, i, lv, where = sweep
        items.append(F("Liquidity sweep", "Bullish" if b == "bull" else "Bearish",
                       f"{_day(ch.t[i])}: wicked {where} ({_p(lv)}) and closed back inside — a failed break that took out stops.",
                       b, ts=ch.t[i], weight=2, level=lv))
        markers.append({"ts": ch.t[i], "pos": "below" if b == "bull" else "above", "bias": b, "text": "Sweep"})
    else:
        items.append(_absent("Liquidity sweep", "No wick through a swing level that closed back inside this week."))
    # order blocks: last opposite candle before a >= 2 ATR displacement
    obs = []
    for i in range(max(3, ch.n - 40), ch.n - 3):
        a_ = ch.atr[i] or ch.A
        up = ch.c[i + 3] - ch.c[i] >= 3 * a_
        dn = ch.c[i] - ch.c[i + 3] >= 3 * a_
        if up and ch.red(i):
            obs.append({"bull": True, "lo": ch.l[i], "hi": ch.h[i], "i": i})
        if dn and ch.green(i):
            obs.append({"bull": False, "lo": ch.l[i], "hi": ch.h[i], "i": i})
    for ob in obs:
        later = range(ob["i"] + 4, ch.n)
        ob["mitigated"] = any((ch.l[k] <= ob["hi"]) if ob["bull"] else (ch.h[k] >= ob["lo"]) for k in later)
        ob["broken"] = any((ch.c[k] < ob["lo"]) if ob["bull"] else (ch.c[k] > ob["hi"]) for k in later)
    for bull in (True, False):
        nm = "Bullish order block" if bull else "Bearish order block"
        live = [o for o in obs if o["bull"] == bull and not o["broken"]]
        if live:
            o = live[-1]
            items.append(F(nm, f"{_p(o['lo'])} – {_p(o['hi'])}",
                           f"The last {'down' if bull else 'up'} candle ({_day(ch.t[o['i']])}) before a strong move "
                           f"{'up' if bull else 'down'}. {'Price has come back into it' if o['mitigated'] else 'Price has not returned to it yet'}; "
                           f"some traders watch it as {'support' if bull else 'resistance'}.",
                           "bull" if bull else "bear", ts=ch.t[o["i"]]))
            zones_out.append({"lo": o["lo"], "hi": o["hi"], "kind": "ob", "bull": bull, "ts": ch.t[o["i"]]})
        else:
            items.append(_absent(nm, "No intact zone before a strong move in the last two months."))
    brk = [o for o in obs if o["broken"]]
    if brk:
        o = brk[-1]
        items.append(F("Breaker block", f"{_p(o['lo'])} – {_p(o['hi'])}",
                       f"A former {'bullish' if o['bull'] else 'bearish'} order block ({_day(ch.t[o['i']])}) that price closed through. "
                       f"In this framework it flips role and may act as {'resistance' if o['bull'] else 'support'}.",
                       "bear" if o["bull"] else "bull", ts=ch.t[o["i"]]))
    else:
        items.append(_absent("Breaker block", "No order block has been broken through recently."))
    # premium / discount
    if ch.n >= 60:
        hi, lo = max(ch.h[-60:]), min(ch.l[-60:])
        eq = (hi + lo) / 2
        pos = (c - lo) / (hi - lo) if hi > lo else 0.5
        items.append(F("Premium / discount", "Discount" if pos < 0.5 else "Premium",
                       f"60-session range {_p(lo)} – {_p(hi)}, midpoint {_p(eq)}. Price sits at {pos * 100:.0f}% of the range — "
                       + ("the cheap half, where this framework prefers buying." if pos < 0.5 else
                          "the expensive half, where this framework prefers selling."),
                       "bull" if pos < 0.35 else "bear" if pos > 0.65 else "neutral", level=eq))
    # anchored VWAP from the 52-week low and high
    s = max(0, ch.n - 252)
    for nm, idx in (("Anchored VWAP (from 52-week low)", min(range(s, ch.n), key=lambda q: ch.l[q])),
                    ("Anchored VWAP (from 52-week high)", max(range(s, ch.n), key=lambda q: ch.h[q]))):
        pv = sum((ch.h[k] + ch.l[k] + ch.c[k]) / 3 * ch.v[k] for k in range(idx, ch.n))
        vv = sum(ch.v[k] for k in range(idx, ch.n))
        if vv and ch.n - idx >= 5:
            av = pv / vv
            items.append(F(nm, f"{_p(av)} ({'above' if c > av else 'below'})",
                           f"Average price paid by everyone who bought since {_day(ch.t[idx])}. "
                           + (f"Those buyers are in profit on average." if c > av else "Those buyers are underwater on average."),
                           "bull" if c > av else "bear", level=av))
        else:
            items.append(_absent(nm, "The anchor is too recent to measure."))
    return items, zones_out, markers


# ---------------------------------------------------------------------------
# 9. Market context
# ---------------------------------------------------------------------------
def weekly_trend(ch: Chart) -> str | None:
    weeks: dict = {}
    for t, c in zip(ch.t, ch.c):
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
        weeks[d.isocalendar()[:2]] = c
    wc = list(weeks.values())
    if len(wc) < 30:
        return None
    s10, s30 = sum(wc[-10:]) / 10, sum(wc[-30:]) / 30
    if wc[-1] > s10 > s30:
        return "up"
    if wc[-1] < s10 < s30:
        return "down"
    return "range"


def context_section(ch: Chart, trend: str, spy: list[float] | None,
                    intraday: list[dict] | None, partial: bool) -> list[dict]:
    items = []
    c = ch.c[-1]
    p = ch.n - 2 if partial else ch.n - 1
    if p >= 0:
        items.append(F("Previous day high / low", f"{_p(ch.l[p])} – {_p(ch.h[p])}",
                       f"Yesterday's range. Price is "
                       + ("above yesterday's high — buyers in control." if c > ch.h[p] else
                          "below yesterday's low — sellers in control." if c < ch.l[p] else "inside yesterday's range."),
                       "bull" if c > ch.h[p] else "bear" if c < ch.l[p] else "neutral", level=ch.h[p]))
    s = max(0, ch.n - 252)
    hi, lo = max(ch.h[s:]), min(ch.l[s:])
    items.append(F("52-week high / low", f"{_pct(c, hi):+.1f}% from high",
                   f"Year range {_p(lo)} – {_p(hi)}; price is {(c - lo) / (hi - lo) * 100 if hi > lo else 50:.0f}% of the way up.",
                   "bull" if c >= 0.97 * hi else "bear" if c <= 1.03 * lo else "neutral", weight=1))
    if spy and len(spy) > 64 and ch.n > 64:
        rs = (_pct(ch.c[-1], ch.c[-64]) - _pct(spy[-1], spy[-64]))
        items.append(F("Relative strength vs SPY", f"{rs:+.1f} pts (3 months)",
                       f"The stock {'beat' if rs > 0 else 'lagged'} the S&P 500 by {abs(rs):.1f} percentage points over 3 months"
                       + (" — a market leader; leaders tend to keep leading in strong markets." if rs > 10 else
                          " — a laggard; weak stocks tend to stay weak." if rs < -10 else " — moving roughly with the market."),
                       "bull" if rs > 5 else "bear" if rs < -5 else "neutral", weight=2 if abs(rs) > 10 else 1))
        rets = [_pct(ch.c[i], ch.c[i - 1]) for i in range(ch.n - 60, ch.n)]
        srets = [_pct(spy[i], spy[i - 1]) for i in range(len(spy) - 60, len(spy))]
        if len(rets) == len(srets) == 60:
            mr, ms = sum(rets) / 60, sum(srets) / 60
            cov = sum((a - mr) * (b - ms) for a, b in zip(rets, srets)) / 60
            vr_, vs_ = sum((a - mr) ** 2 for a in rets) / 60, sum((b - ms) ** 2 for b in srets) / 60
            if vr_ > 0 and vs_ > 0:
                corr, beta = cov / math.sqrt(vr_ * vs_), cov / vs_
                items.append(F("Correlation with the market", f"{corr:.2f} (beta {beta:.1f})",
                               f"Over 60 sessions the stock moved {'closely' if corr > 0.6 else 'loosely' if corr > 0.3 else 'mostly independently'} "
                               f"with SPY; a beta of {beta:.1f} means it typically moves {beta:.1f}× as much as the market.",
                               "neutral"))
    else:
        items.append(_absent("Relative strength vs SPY", "SPY history isn't cached yet."))
    wk = weekly_trend(ch)
    daily = {"up": "up", "down": "down", "range": "sideways"}[trend]
    if wk:
        wtxt = {"up": "up", "down": "down", "range": "sideways"}[wk]
        agree = wk == trend
        items.append(F("Multi-timeframe", f"Weekly {wtxt}, daily {daily}",
                       ("Both timeframes agree." if agree else
                        "The weekly and daily trends disagree — the weekly is the bigger picture; use the daily for timing.")
                       + (" " + ("Intraday price is above today's VWAP." if intraday and intraday[-1]["c"] > _vwap(intraday)
                                 else "Intraday price is below today's VWAP." if intraday else "")),
                       ("bull" if wk == "up" else "bear" if wk == "down" else "neutral") if agree else "neutral",
                       weight=2 if agree and wk != "range" else 1))
    if intraday:
        today = [b for b in intraday if _et_date(b["ts"]) == _et_date(intraday[-1]["ts"])]
        first = [b for b in today if _et_minutes(b["ts"]) < 30]
        if first and len(today) > len(first):
            orh, orl = max(b["h"] for b in first), min(b["l"] for b in first)
            last = today[-1]["c"]
            items.append(F("Opening range (first 30 min)", f"{_p(orl)} – {_p(orh)}",
                           "Breaking above the opening range is a common day-trade long trigger; below it, a short trigger. "
                           + ("Price is above it." if last > orh else "Price is below it." if last < orl else "Price is inside it."),
                           "bull" if last > orh else "bear" if last < orl else "neutral"))
        else:
            items.append(_absent("Opening range (first 30 min)", "Available after 10:00 AM ET for watchlist stocks."))
    else:
        items.append(_absent("Opening range (first 30 min)", "Needs today's 1-minute bars — watchlist stocks during the session."))
    return items


def _vwap(bars):
    vv = sum((b.get("v") or 0) for b in bars)
    return sum((b["h"] + b["l"] + b["c"]) / 3 * (b.get("v") or 0) for b in bars) / vv if vv else bars[-1]["c"]


def _et_date(ts):
    return dt.datetime.fromtimestamp(ts, ET).date()


def _et_minutes(ts):
    d = dt.datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute - 570


# ---------------------------------------------------------------------------
# 10. Risk plan
# ---------------------------------------------------------------------------
def risk_plan(ch: Chart, st: dict, key_levels: dict) -> dict:
    c, A = ch.c[-1], ch.A
    sup = key_levels.get("support") or []
    res = key_levels.get("resistance") or []
    stops = [{"name": "ATR stop (2× ATR)", "price": c - 2 * A,
              "why": "Outside the stock's normal daily noise."}]
    if st.get("last_pl") is not None and ch.l[st["last_pl"]] < c:
        stops.append({"name": "Below the last swing low", "price": ch.l[st["last_pl"]] - 0.25 * A,
                      "why": f"If price breaks {_p(ch.l[st['last_pl']])}, the higher-low structure is gone."})
    if sup:
        stops.append({"name": "Below nearest support", "price": sup[0]["level"] - 0.25 * A,
                      "why": f"Support at {_p(sup[0]['level'])} is the trade's line in the sand."})
    hh22 = max(ch.h[-22:])
    trail = hh22 - 3 * A
    targets = [{"name": "1R", "mult": 1}, {"name": "2R", "mult": 2}]
    if res:
        targets.append({"name": "Nearest resistance", "price": res[0]["level"]})
    return {"price": c, "atr": A, "stops": stops, "targets": targets,
            "trailing": {"name": "Chandelier trail (22-day high − 3× ATR)", "price": trail}}


# ---------------------------------------------------------------------------
# Summary + chart payload + entry point
# ---------------------------------------------------------------------------
SECTION_TITLES = {
    "trend": "Trend", "levels": "Levels", "gaps": "Gaps", "candles": "Candles",
    "patterns": "Patterns", "volume": "Volume", "indicators": "Indicators",
    "liquidity": "Liquidity", "context": "Context",
}


def summarize(sections: dict) -> dict:
    act = [f for items in sections.values() for f in items if f["active"]]
    bull = sum(f["weight"] for f in act if f["bias"] == "bull")
    bear = sum(f["weight"] for f in act if f["bias"] == "bear")
    tot = bull + bear
    if tot == 0:
        bias = "mixed"
    elif bull >= 1.5 * bear:
        bias = "bullish"
    elif bear >= 1.5 * bull:
        bias = "bearish"
    else:
        bias = "mixed"
    top = sorted([f for f in act if f["bias"] != "neutral"], key=lambda f: (-f["weight"], -(f["ts"] or 0)))
    lead = [f for f in top if f["bias"] == ("bull" if bias == "bullish" else "bear")][:3] if bias != "mixed" else top[:3]
    against = [f for f in top if bias != "mixed" and f["bias"] == ("bear" if bias == "bullish" else "bull")][:2]
    def name(f):
        return f"{f['name']}: {f['status'][0].lower() + f['status'][1:]}"
    head = {"bullish": "Mostly bullish", "bearish": "Mostly bearish", "mixed": "Mixed picture"}[bias]
    sent = head + (": " + ", ".join(name(f) for f in lead) if lead else "") + "."
    if against:
        sent += " Against it: " + ", ".join(name(f) for f in against) + "."
    return {"bias": bias, "bull": bull, "bear": bear, "headline": sent,
            "highlights": [{"name": f["name"], "status": f["status"], "bias": f["bias"],
                            "detail": f["detail"]} for f in top[:6]],
            "checked": sum(len(v) for v in sections.values()), "found": len(act)}


def _series(ch: Chart, xs: list, from_i: int = 0):
    return [{"time": ch.t[i], "value": round(v, 4)} for i, v in enumerate(xs)
            if v is not None and i >= from_i]


def analyze(bars: list[dict], spy_closes: list[float] | None = None,
            intraday: list[dict] | None = None,
            now: dt.datetime | None = None) -> dict | None:
    """Full chart read. bars: daily, oldest-first (>= 60 needed)."""
    if not bars or len(bars) < 60:
        return None
    ch = Chart(bars)
    last_day = dt.datetime.fromtimestamp(bars[-1]["ts"], dt.timezone.utc).date()
    now_et = (now or dt.datetime.now(ET)).astimezone(ET)
    minutes = session_minutes(now_et)
    partial = last_day == now_et.date() and minutes is not None
    st = structure(ch)
    zs = zones(ch)
    lv_items, key = levels_section(ch, zs)
    gap_items, gap_chart = gaps_section(ch, st["trend"])
    candle_items, candle_markers = candles_section(ch, key["support"], key["resistance"])
    pat_items, pat_lines, trendlines = patterns_section(ch)
    vol_items, vp = volume_section(ch, minutes, partial)
    ind_items, extra = indicators_section(ch, intraday)
    liq_items, liq_zones, liq_markers = liquidity_section(ch)
    ctx_items = context_section(ch, st["trend"], spy_closes, intraday, partial)
    sections = {"trend": trend_section(ch, st), "levels": lv_items, "gaps": gap_items,
                "candles": candle_items, "patterns": pat_items, "volume": vol_items,
                "indicators": ind_items, "liquidity": liq_items, "context": ctx_items}
    for k in sections:
        sections[k].sort(key=lambda f: not f["active"])
    # chart payload
    from_i = max(0, ch.n - 260)
    bb_mid = sma_s(ch.c, 20)
    sd = rolling(ch.c, 20, lambda w: (sum((x - sum(w) / 20) ** 2 for x in w) / 20) ** 0.5)
    bb_up = [m + 2 * s if m is not None and s is not None else None for m, s in zip(bb_mid, sd)]
    bb_lo = [m - 2 * s if m is not None and s is not None else None for m, s in zip(bb_mid, sd)]
    ml, sg, hs = extra["macd"]
    ev_markers = [{"ts": ch.t[i], "pos": "below" if b == "bull" else "above", "bias": b, "text": kind}
                  for b, kind, i, _ in st["events"][-2:]]
    lines = []
    for z in key["support"][:2]:
        lines.append({"price": z["level"], "title": f"Support ({z['touches']})", "bias": "bull"})
    for z in key["resistance"][:2]:
        lines.append({"price": z["level"], "title": f"Resistance ({z['touches']})", "bias": "bear"})
    lines += pat_lines
    if vp:
        lines.append({"price": vp["poc"], "title": "Volume POC", "bias": "neutral", "kind": "poc"})
    kept = []
    for ln in lines:
        if all(abs(ln["price"] - k["price"]) > 0.006 * ch.c[-1] for k in kept) and \
                abs(ln["price"] - ch.c[-1]) > 0.004 * ch.c[-1]:
            kept.append(ln)
    lines = kept
    chart = {
        "overlays": {"sma20": _series(ch, ch.sma20, from_i), "sma50": _series(ch, ch.sma50, from_i),
                     "sma200": _series(ch, ch.sma200, from_i), "ema21": _series(ch, ch.ema21, from_i),
                     "bb_upper": _series(ch, bb_up, from_i), "bb_lower": _series(ch, bb_lo, from_i)},
        "volume": [{"time": ch.t[i], "value": ch.v[i], "up": ch.c[i] >= ch.o[i]} for i in range(from_i, ch.n)],
        "rsi": _series(ch, ch.rsi, from_i),
        "macd": {"line": _series(ch, ml, from_i), "signal": _series(ch, sg, from_i), "hist": _series(ch, hs, from_i)},
        "markers": sorted(candle_markers + gap_chart["markers"] + liq_markers + ev_markers,
                          key=lambda m: m["ts"])[-10:],
        "lines": lines,
        "fib": [{"ratio": r, "price": p} for r, p in sorted(extra.get("fib", {}).items())],
        "zones": gap_chart["zones"] + liq_zones,
        "trendlines": trendlines,
    }
    return {"summary": summarize(sections),
            "sections": [{"key": k, "title": SECTION_TITLES[k], "items": sections[k]} for k in sections],
            "risk": risk_plan(ch, st, key), "chart": chart,
            "session": {"partial": partial, "minutes": minutes}}
