#!/usr/bin/env python3
"""Evidence study of the scanner's technical setups.

Walk-forward over the S&P 500 fallback universe: for every symbol and every
evaluation day (after a 250-bar warmup), determine which setups from
scanner.technical_screen fired, using precomputed indicator series that
replicate the screen's conditions EXACTLY, then measure forward outcomes
(+5/+10/+20 sessions, target-first within 20 sessions).

The series-based replication is verified against scanner.technical_screen
itself on a random sample of (symbol, day) pairs before any results are
trusted; the study aborts if the sample is not 100% clean.

This script is analysis-only: it never mutates scanner.py scoring.

Run:  .venv/bin/python study_setups.py
Out:  hidden_files/setup-evidence-study.md  (+ console summary)
"""
from __future__ import annotations

import datetime as dt
import random
import statistics
import sys
import time
from collections import defaultdict

import indicators
import scanner
from config import Settings

WARMUP = 250                    # bars of warmup before the first eval day
FWD_MAX = 20                    # longest forward window (sessions)
DEDUPE_GAP = 5                  # no-fire sessions required before a new event
BASELINE_N = 20_000             # random baseline sample size
VERIFY_N = 250                  # >= 200 required by the study method
SEED = 42
FETCH_DAYS = 800                # ~250 warmup + ~2y eval (calendar-bounded)
MIN_BARS = 300                  # skip symbols with fewer bars than this
REPORT_PATH = "hidden_files/setup-evidence-study.md"

BULL = ["oversold_bounce", "bollinger_squeeze", "volume_spike",
        "breakout_watch", "golden_cross", "trend_above_sma200",
        "trend_sma50_above_sma200"]
BEAR = ["overbought_breakdown", "bollinger_breakdown", "volume_spike_down",
        "low52_breakdown", "death_cross", "atr_selloff"]
ALL_SETUPS = BULL + BEAR
DIRECTION = {**{n: +1 for n in BULL}, **{n: -1 for n in BEAR}}
WEIGHT_OF = {**scanner.WEIGHTS, **scanner.BEAR_WEIGHTS}


# ---------------------------------------------------------------------------
# Precomputed series (all causal: value at index i uses bars[0..i] only,
# exactly like calling the scanner's functions on the prefix bars[:i+1])
# ---------------------------------------------------------------------------
class Series:
    def __init__(self, bars: list[dict]):
        self.bars = bars
        n = len(bars)
        self.n = n
        self.opens = [b["o"] for b in bars]
        self.highs = [b["h"] for b in bars]
        self.lows = [b["l"] for b in bars]
        self.closes = [b["c"] for b in bars]
        self.vols = [b["v"] for b in bars]
        c = self.closes

        # RSI: indicators.rsi_wilder is a causal Wilder recursion seeded on
        # the first 14 changes of the series -> one pass over the full list
        # reproduces rsi_wilder(closes[:i+1])[-1] at every i.
        self.rsi = indicators.rsi_wilder(c)

        # SMAs via rolling sums (value at i = mean of closes[i-n+1..i]).
        self.sma50 = self._sma_series(c, 50)
        self.sma200 = self._sma_series(c, 200)

        # Bollinger (20, 2 sigma, population variance) + bandwidth + lower.
        self.bw: list[float | None] = [None] * n
        self.bb_lower: list[float | None] = [None] * n
        s = 0.0
        sq = 0.0
        for i, v in enumerate(c):
            s += v
            sq += v * v
            if i >= 20:
                s -= c[i - 20]
                sq -= c[i - 20] * c[i - 20]
            if i >= 19:
                mid = s / 20.0
                var = sq / 20.0 - mid * mid
                sd = var ** 0.5 if var > 0 else 0.0
                self.bb_lower[i] = mid - 2.0 * sd
                self.bw[i] = (4.0 * sd / mid) if mid != 0 else None

        # Median of the prior 20 volumes (vols[i-20:i]) per index.
        self.vol_med: list[float | None] = [None] * n
        for i in range(20, n):
            self.vol_med[i] = statistics.median(self.vols[i - 20:i])

        # Rolling 52-week extremes over the last min(252, i+1) bars.
        self.high52 = self._rolling_extreme(self.highs, 252, max)
        self.low52 = self._rolling_extreme(self.lows, 252, min)

        # ATR(14) Wilder series: seeded with mean TR of bars 1..14, the same
        # seeding indicators.atr applies to any prefix (causal recursion).
        self.atr_at: list[float | None] = [None] * n
        trs: list[float] = [0.0] * n
        for i in range(1, n):
            h, l, pc = self.highs[i], self.lows[i], c[i - 1]
            trs[i] = max(h - l, abs(h - pc), abs(l - pc))
        if n > 14:
            a = sum(trs[1:15]) / 14.0
            self.atr_at[14] = a
            for i in range(15, n):
                a = (a * 13.0 + trs[i]) / 14.0
                self.atr_at[i] = a

        # SMA cross events at bar i (indicators.crossed_above/below sense).
        self.cross_up: list[bool] = [False] * n
        self.cross_dn: list[bool] = [False] * n
        for i in range(1, n):
            f0, f1 = self.sma50[i - 1], self.sma50[i]
            s0, s1 = self.sma200[i - 1], self.sma200[i]
            if None in (f0, f1, s0, s1):
                continue
            if f0 <= s0 and f1 > s1:
                self.cross_up[i] = True
            if f0 >= s0 and f1 < s1:
                self.cross_dn[i] = True

    @staticmethod
    def _sma_series(values: list[float], n: int) -> list[float | None]:
        out: list[float | None] = [None] * len(values)
        s = 0.0
        for i, v in enumerate(values):
            s += v
            if i >= n:
                s -= values[i - n]
            if i >= n - 1:
                out[i] = s / n
        return out

    @staticmethod
    def _rolling_extreme(values: list[float], window: int,
                         fn) -> list[float]:
        """Rolling max/min over the last min(window, i+1) values (deque)."""
        from collections import deque
        out = [0.0] * len(values)
        dq: deque[int] = deque()
        better = (lambda a, b: a >= b) if fn is max else (lambda a, b: a <= b)
        for i, v in enumerate(values):
            while dq and better(v, values[dq[-1]]):
                dq.pop()
            dq.append(i)
            while dq[0] <= i - window:
                dq.popleft()
            out[i] = values[dq[0]]
        return out


def _crossed_recently(S: Series, i: int, up: bool) -> bool:
    """scanner._crossed_*_recently(closes[:i+1], 50, 200, window=5): the SMA
    lists span as-of indices i-6..i (any None -> False); crossed_above on
    each prefix pair means a cross event at some bar in i-5..i."""
    for t in range(i - 6, i + 1):
        if S.sma50[t] is None or S.sma200[t] is None:
            return False
    events = S.cross_up if up else S.cross_dn
    return any(events[t] for t in range(i - 5, i + 1))


def flags_at(S: Series, i: int) -> set[str]:
    """Setup names firing at bar i — a line-by-line replication of
    scanner.technical_screen on the prefix bars[:i+1]."""
    out: set[str] = set()
    closes, price = S.closes, S.closes[i]

    # RSI setups
    rsi_now = S.rsi[i]
    rsi_prev = S.rsi[i - 1] if i >= 1 else None
    if rsi_now is not None and rsi_prev is not None:
        if rsi_now < 30 and rsi_now > rsi_prev:
            out.add("oversold_bounce")
        if rsi_now > 70 and rsi_now < rsi_prev:
            out.add("overbought_breakdown")

    # Bollinger squeeze: bandwidth at 126-day low (uses hist over prefix-end
    # bandwidths at indices i-126..i), gated on prefix length >= 146.
    bw = S.bw[i]
    if bw is not None and (i + 1) >= 126 + 20:
        hist = [h for h in S.bw[i - 126:i + 1] if h is not None]
        if hist and bw <= min(hist) * 1.02:
            out.add("bollinger_squeeze")

    # Bollinger breakdown
    bb_lower = S.bb_lower[i]
    if bb_lower is not None and price < bb_lower:
        out.add("bollinger_breakdown")

    # Volume spike (prior-20 median), direction by green/red day
    if i >= 20:
        med = S.vol_med[i]
        if med is not None and med > 0 and S.vols[i] > 2 * med:
            if closes[i] >= S.opens[i]:
                out.add("volume_spike")
            elif closes[i] < S.opens[i]:
                out.add("volume_spike_down")

    # 52-week proximity setups (lookback = min(252, prefix length); the
    # rolling extremes already implement exactly that window)
    high52, low52 = S.high52[i], S.low52[i]
    if high52 > 0:
        dist = (high52 - price) / high52
        if 0 <= dist <= 0.02:
            out.add("breakout_watch")
    if low52 > 0:
        dist_lo = (price - low52) / low52
        if 0 <= dist_lo <= 0.02:
            out.add("low52_breakdown")

    # SMA-dependent setups (all gated on both SMAs existing, as in the screen)
    sma50, sma200 = S.sma50[i], S.sma200[i]
    if sma50 is not None and sma200 is not None:
        if _crossed_recently(S, i, up=True):
            out.add("golden_cross")
        if _crossed_recently(S, i, up=False):
            out.add("death_cross")
        if price > sma200:
            out.add("trend_above_sma200")
        if sma50 > sma200:
            out.add("trend_sma50_above_sma200")

    # ATR selloff
    atr14 = S.atr_at[i]
    if atr14 and atr14 > 0 and price > 0 and i >= 1:
        drop_pct = (closes[i - 1] - price) / price
        if drop_pct > 2 * (atr14 / price):
            out.add("atr_selloff")

    return out


# ---------------------------------------------------------------------------
# Forward outcomes
# ---------------------------------------------------------------------------
def outcomes(S: Series, i: int, direction: int) -> tuple[dict, int]:
    """Forward stats from bar i's close. Returns ({5: r, 10: r, 20: r}, tf)
    with returns direction-adjusted (%), tf=1 if +5% (direction-adjusted)
    is reached before -3% within the next 20 sessions using daily
    highs/lows; a day hitting both counts stop-first (conservative)."""
    entry = S.closes[i]
    rets = {k: direction * (S.closes[i + k] / entry - 1.0) * 100.0
            for k in (5, 10, 20)}
    if direction > 0:
        tgt, stp = entry * 1.05, entry * 0.97
    else:
        tgt, stp = entry * 0.95, entry * 1.03
    tf = 0
    for j in range(i + 1, i + 21):
        if direction > 0:
            hit_t, hit_s = S.highs[j] >= tgt, S.lows[j] <= stp
        else:
            hit_t, hit_s = S.lows[j] <= tgt, S.highs[j] >= stp
        if hit_t and hit_s:
            break
        if hit_t:
            tf = 1
            break
        if hit_s:
            break
    return rets, tf


class Agg:
    """Running aggregate for one bucket (a setup, or the baseline)."""

    def __init__(self):
        self.events = 0
        self.symbols: set[str] = set()
        self.r5: list[float] = []
        self.r10: list[float] = []
        self.r20: list[float] = []
        self.tf = 0

    def add(self, symbol: str, rets: dict, tf: int):
        self.events += 1
        self.symbols.add(symbol)
        self.r5.append(rets[5])
        self.r10.append(rets[10])
        self.r20.append(rets[20])
        self.tf += tf

    def stats(self) -> dict:
        if not self.events:
            return {"events": 0}
        win = lambda rs: sum(1 for r in rs if r > 0) / len(rs) * 100.0
        return {
            "events": self.events,
            "symbols": len(self.symbols),
            "win5": win(self.r5), "win10": win(self.r10),
            "win20": win(self.r20),
            "avg5": statistics.fmean(self.r5),
            "avg10": statistics.fmean(self.r10),
            "avg20": statistics.fmean(self.r20),
            "med5": statistics.median(self.r5),
            "med10": statistics.median(self.r10),
            "med20": statistics.median(self.r20),
            "tf_rate": self.tf / self.events * 100.0,
            "worst20": min(self.r20),
        }


# ---------------------------------------------------------------------------
# Main study
# ---------------------------------------------------------------------------
def fetch_all(symbols: list[str]) -> dict[str, list[dict]]:
    settings = Settings.from_env()
    rest = scanner.AlpacaREST(settings.alpaca_key, settings.alpaca_secret)
    out: dict[str, list[dict]] = {}
    for ci in range(0, len(symbols), 100):
        chunk = symbols[ci:ci + 100]
        t0 = time.time()
        got = rest.daily_bars_multi(chunk, days=FETCH_DAYS)
        out.update(got)
        print(f"  fetch {ci + len(chunk)}/{len(symbols)} "
              f"({len(got)} with bars, {time.time() - t0:.1f}s)", flush=True)
    return out


def verify(series_by_sym: dict[str, Series], rng: random.Random) -> tuple[int, int, list]:
    """Compare flags_at vs scanner.technical_screen on random pairs."""
    pairs = []
    for sym, S in series_by_sym.items():
        hi = S.n - 1 - FWD_MAX
        if hi >= WARMUP:
            for _ in range(3):
                pairs.append((sym, rng.randint(WARMUP, hi)))
    rng.shuffle(pairs)
    pairs = pairs[:max(VERIFY_N, 200)]
    bad = []
    for sym, i in pairs:
        S = series_by_sym[sym]
        res = scanner.technical_screen(S.bars[:i + 1])
        truth = set()
        if res is not None:
            truth = {n for n, _, _ in res["setups"]}
            truth |= {n for n, _, _ in res["bear_setups"]}
        mine = flags_at(S, i)
        if mine != truth:
            bad.append((sym, i, sorted(mine - truth), sorted(truth - mine)))
    return len(pairs), len(bad), bad


def verdict_and_proposal(st: dict, base: dict) -> tuple[str, str, float]:
    """(verdict, reason, edge_score) from a setup's stats vs its baseline."""
    if st.get("events", 0) < 30:
        return ("KEEP", f"only {st.get('events', 0)} events — sample too "
                "small to judge; kept at current weight pending more data",
                0.0)
    edge_avg = st["avg10"] - base["avg10"]
    edge_win = st["win10"] - base["win10"]
    edge = edge_avg  # primary edge metric: avg 10d excess return (pts)
    if edge_avg >= 0.75 and edge_win >= 3.0:
        v = "STRENGTHEN"
        r = (f"beats baseline by {edge_avg:+.2f}% avg 10d return and "
             f"{edge_win:+.1f} pts win rate")
    elif edge_avg >= 0.25 and edge_win >= 0:
        v = "KEEP"
        r = (f"positive edge vs baseline ({edge_avg:+.2f}% avg 10d, "
             f"{edge_win:+.1f} pts win rate)")
    elif edge_avg <= -0.25 and edge_win <= 0:
        v = "KILL"
        r = (f"negative edge vs baseline ({edge_avg:+.2f}% avg 10d, "
             f"{edge_win:+.1f} pts win rate) — worse than doing nothing")
    else:
        v = "WEAKEN"
        r = (f"edge vs baseline is ~zero/mixed ({edge_avg:+.2f}% avg 10d, "
             f"{edge_win:+.1f} pts win rate)")
    return v, r, edge


def main() -> int:
    rng = random.Random(SEED)
    symbols = list(scanner.SP500_FALLBACK)
    print(f"universe: {len(symbols)} symbols (scanner.SP500_FALLBACK)")

    print("fetching daily bars (days=800)...", flush=True)
    bars_by_sym = fetch_all(symbols)

    series_by_sym: dict[str, Series] = {}
    skipped = 0
    for sym in symbols:
        bars = bars_by_sym.get(sym) or []
        if len(bars) < MIN_BARS:
            skipped += 1
            continue
        series_by_sym[sym] = Series(bars)
    print(f"usable symbols: {len(series_by_sym)} (skipped {skipped} "
          f"with < {MIN_BARS} bars)", flush=True)

    # --- verification gate -------------------------------------------------
    print("verifying replication vs scanner.technical_screen...", flush=True)
    n_checked, n_bad, bad = verify(series_by_sym, rng)
    print(f"verification: {n_checked} (symbol, day) pairs, "
          f"{n_bad} mismatches")
    if n_bad:
        for sym, i, extra, missing in bad[:10]:
            print(f"  MISMATCH {sym}@{i}: mine-only={extra} "
                  f"screen-only={missing}")
        print("ABORT: replication is not exact; no report written.")
        return 1
    print("verification: 100% clean — proceeding.", flush=True)

    # --- walk-forward ------------------------------------------------------
    aggs: dict[str, Agg] = {name: Agg() for name in ALL_SETUPS}
    total_events = 0
    eval_days = 0
    first_eval: dt.date | None = None
    last_eval: dt.date | None = None
    # baseline reservoir of (symbol, i) with no setup firing
    reservoir: list[tuple[str, int]] = []
    seen_candidates = 0

    for sym, S in series_by_sym.items():
        hi = S.n - 1 - FWD_MAX
        if hi < WARMUP:
            continue
        last_fire: dict[str, int] = {}
        for i in range(WARMUP, hi + 1):
            eval_days += 1
            day = dt.datetime.fromtimestamp(S.bars[i]["ts"],
                                            dt.timezone.utc).date()
            if first_eval is None or day < first_eval:
                first_eval = day
            if last_eval is None or day > last_eval:
                last_eval = day
            flags = flags_at(S, i)
            if not flags:
                seen_candidates += 1
                item = (sym, i)
                if len(reservoir) < BASELINE_N:
                    reservoir.append(item)
                else:
                    j = rng.randint(0, seen_candidates - 1)
                    if j < BASELINE_N:
                        reservoir[j] = item
                continue
            for name in flags:
                lf = last_fire.get(name)
                if lf is None or (i - lf) > DEDUPE_GAP:
                    rets, tf = outcomes(S, i, DIRECTION[name])
                    aggs[name].add(sym, rets, tf)
                    total_events += 1
                last_fire[name] = i

    base_long, base_short = Agg(), Agg()
    for sym, i in reservoir:
        S = series_by_sym[sym]
        rets, tf = outcomes(S, i, +1)
        base_long.add(sym, rets, tf)
        rets, tf = outcomes(S, i, -1)
        base_short.add(sym, rets, tf)
    bases = {+1: base_long.stats(), -1: base_short.stats()}

    stats = {name: aggs[name].stats() for name in ALL_SETUPS}
    verdicts = {}
    for name in ALL_SETUPS:
        verdicts[name] = verdict_and_proposal(stats[name],
                                              bases[DIRECTION[name]])

    # --- proposed weights: proportional to demonstrated avg-10d edge -----
    proposed: dict[str, int] = {}
    for side in (BULL, BEAR):
        edges = {n: max(0.0, verdicts[n][2]) for n in side}
        top = max(edges.values()) if edges else 0.0
        raw = {}
        for n in side:
            if verdicts[n][0] == "KILL" or top <= 0:
                raw[n] = 0.0
            else:
                raw[n] = 25.0 * edges[n] / top
                if n.startswith("trend_"):
                    raw[n] = min(raw[n], 10.0)  # bonuses stay bonuses
        total = sum(raw.values())
        if total > 100.0:  # keep the side on the same 0-100 scale
            raw = {n: v * 100.0 / total for n, v in raw.items()}
        for n in side:
            w = int(round(raw[n]))
            if verdicts[n][0] in ("KEEP", "STRENGTHEN") and edges[n] > 0:
                w = max(w, 3)
            proposed[n] = w

    # --- report ------------------------------------------------------------
    def fmt(x, nd=2):
        return f"{x:+.{nd}f}" if isinstance(x, float) else str(x)

    def table(side_names, base):
        lines = ["| Setup | Wt now | Events | Win% 10d | Avg 5d | Avg 10d "
                 "| Avg 20d | Med 10d | Target-first % | Worst 20d "
                 "| Edge vs baseline |",
                 "|---|---|---|---|---|---|---|---|---|---|---|"]
        for n in side_names:
            st = stats[n]
            if not st.get("events"):
                lines.append(f"| {n} | {WEIGHT_OF[n]} | 0 | — | — | — | — "
                             "| — | — | — | — |")
                continue
            edge_avg = st["avg10"] - base["avg10"]
            edge_win = st["win10"] - base["win10"]
            lines.append(
                f"| {n} | {WEIGHT_OF[n]} | {st['events']} "
                f"| {st['win10']:.1f} | {fmt(st['avg5'])}% "
                f"| {fmt(st['avg10'])}% | {fmt(st['avg20'])}% "
                f"| {fmt(st['med10'])}% | {st['tf_rate']:.1f} "
                f"| {fmt(st['worst20'], 1)}% "
                f"| {edge_avg:+.2f}% avg10 / {edge_win:+.1f} pts win |")
        return lines

    bl, bs = bases[+1], bases[-1]
    md = []
    md.append("# Setup Evidence Study — stockbot scanner")
    md.append("")
    md.append(f"Generated {dt.datetime.now(dt.timezone.utc).date()} by "
              "`study_setups.py` (analysis only; production scoring "
              "unchanged).")
    md.append("")
    md.append("## Method")
    md.append("")
    md.append(f"- **Universe:** scanner.SP500_FALLBACK — {len(symbols)} "
              f"symbols requested; {len(series_by_sym)} had >= {MIN_BARS} "
              f"daily bars and were studied ({skipped} skipped).")
    md.append(f"- **History:** Alpaca daily bars fetched with days=800 "
              f"(IEX feed, split-adjusted). First {WARMUP} bars per symbol "
              "are warmup; evaluation days run "
              f"{first_eval} → {last_eval} ({eval_days:,} symbol-days).")
    md.append("- **Setup detection:** precomputed causal indicator series "
              "replicating scanner.technical_screen line-by-line "
              "(RSI/Bollinger/SMA/volume-median/52-week extremes/ATR).")
    md.append(f"- **Verification:** {n_checked} random (symbol, day) pairs "
              f"checked against scanner.technical_screen(bars[:i+1]) — "
              f"**{n_checked - n_bad}/{n_checked} exact matches, "
              f"{n_bad} mismatches** across all 13 setups.")
    md.append("- **Setups:** technical_screen defines 13 named setups — "
              "7 bullish (incl. two +5 trend bonuses) and 6 bearish; all "
              "13 are studied.")
    md.append(f"- **Events:** a setup firing on consecutive days counts "
              f"once; a new event requires >= {DEDUPE_GAP} no-fire "
              f"sessions. Total deduped events: **{total_events:,}**.")
    md.append("- **Outcomes (from the fire day's close):** forward returns "
              "at +5/+10/+20 sessions (direction-adjusted for bearish); "
              "target-first = +5% reached before −3% (direction-adjusted) "
              "within 20 sessions on daily highs/lows (a day hitting both "
              "counts as stop-first).")
    md.append(f"- **Baseline:** {len(reservoir):,} random (symbol, day) "
              "pairs on which NO setup fired, scored long (for bullish "
              "comparisons) and short (for bearish). Baseline long: "
              f"win%10 {bl['win10']:.1f}, avg10 {fmt(bl['avg10'])}%; "
              f"baseline short: win%10 {bs['win10']:.1f}, "
              f"avg10 {fmt(bs['avg10'])}%.")
    md.append("")
    md.append("## Bullish setups")
    md.append("")
    md += table(BULL, bl)
    md.append("")
    md.append("## Bearish setups")
    md.append("")
    md += table(BEAR, bs)
    md.append("")
    md.append("## Verdicts")
    md.append("")
    for n in ALL_SETUPS:
        v, reason, _ = verdicts[n]
        md.append(f"- **{n}** ({'bullish' if DIRECTION[n] > 0 else 'bearish'}"
                  f", weight {WEIGHT_OF[n]}): **{v}** — {reason}.")
    md.append("")
    md.append("Verdict rule: STRENGTHEN = avg-10d edge >= +0.75% and "
              "win-rate edge >= +3 pts; KEEP = edge >= +0.25% with win "
              "edge >= 0; KILL = edge <= −0.25% with win edge <= 0; "
              "WEAKEN otherwise; < 30 events = KEEP (insufficient sample).")
    md.append("")
    md.append("## Proposed weights (0–100 scale, same as today)")
    md.append("")
    md.append("| Setup | Current | Proposed |")
    md.append("|---|---|---|")
    for n in ALL_SETUPS:
        md.append(f"| {n} | {WEIGHT_OF[n]} | {proposed[n]} |")
    md.append("")
    md.append("Derivation: within each side, proposed weight ∝ max(0, "
              "avg-10d edge vs baseline), scaled so the strongest setup "
              "gets 25; KILL verdicts get 0; trend bonuses capped at 10; "
              "each side normalised to sum <= 100. These are proposals "
              "from this study only — not applied to scanner.py.")
    md.append("")
    md.append("## Limitations")
    md.append("")
    md.append("- **IEX-only volume:** Alpaca's free IEX feed carries only "
              "IEX-exchange volume (~a few % of consolidated tape), so "
              "the volume-median spike conditions — and any volume-based "
              "result — are measured on a partial tape; on the "
              "consolidated tape the same setups may fire differently.")
    md.append("- **No transaction costs:** returns are gross — no "
              "commissions, spreads, slippage, borrow costs (for the "
              "bearish side) or taxes; real-world results would be lower, "
              "especially for the 5-day horizon.")
    md.append("- **Survivorship bias:** the universe is the built-in "
              "SP500_FALLBACK snapshot (compiled 2026-10-19) applied "
              "retroactively — companies that left the index (or failed) "
              "during the window are missing, which flatters results.")
    md.append("- **Window/regime:** the ~2-year window (fetch-bounded by "
              "calendar days, ≈575 trading days of bars, ~1.2 years of "
              "post-warmup evaluation) covers a mostly-rising market; "
              "setup edges — especially trend bonuses and bounce setups — "
              "may not transfer to a sustained bear regime. Edges are "
              "also measured independently per setup: real scanner "
              "scores stack setups, and stacked behaviour is not "
              "validated here.")
    md.append("- Split-adjusted prices only (no dividend adjustment), "
              "and no liquidity gate was applied in the study (the "
              "production scanner additionally requires 20-day avg "
              "dollar volume >= $250k — immaterial for this universe).")
    md.append("")

    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(md))

    # --- console summary ---------------------------------------------------
    ranked = sorted(ALL_SETUPS, key=lambda n: stats[n].get("win10", -1),
                    reverse=True)
    print(f"\nreport written: {REPORT_PATH}")
    print(f"total events: {total_events:,}")
    print(f"baseline long : win10 {bl['win10']:.1f}% avg10 {bl['avg10']:+.2f}%")
    print(f"baseline short: win10 {bs['win10']:.1f}% avg10 {bs['avg10']:+.2f}%")
    print("\nby 10d win rate:")
    for n in ranked:
        st = stats[n]
        if st.get("events"):
            print(f"  {n:26s} w={WEIGHT_OF[n]:>2} -> {proposed[n]:>2}  "
                  f"ev={st['events']:>6,} win10={st['win10']:5.1f}% "
                  f"avg10={st['avg10']:+.2f}% tf={st['tf_rate']:4.1f}% "
                  f"[{verdicts[n][0]}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
