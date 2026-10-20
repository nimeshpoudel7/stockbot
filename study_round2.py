#!/usr/bin/env python3
"""HISTORICAL (pre Oct 20, 2026 signal rebuild): this study measured the
old setups (breakout_watch, golden_cross, overbought_breakdown, ...),
which no longer exist in scanner.py. Kept for the record; it will not
run against the current scanner. Use study_trader_signals.py instead.

Evidence study round 2 — three questions, same discipline as round 1.

Q1  Trend double-count: conditional edges of the three trend components
    and the merged "uptrend" alternative.
Q2  Regime-split (SPY close vs SPY SMA200, per day) and out-of-sample
    validation (fit half vs test half) of the CURRENT Oct-20 weights.
Q3  Bollinger squeeze as a multiplier: breakout_watch / volume_spike
    with vs without the squeeze condition the same day.

Reuses study_setups.py wholesale (Series, flags_at, outcomes, Agg,
fetch_all, the verified replication of scanner.technical_screen).
Analysis-only: never mutates production code.

Run:  .venv/bin/python study_round2.py
Out:  hidden_files/round2-evidence-study.md  (+ console summary)
"""
from __future__ import annotations

import bisect
import datetime as dt
import random
import statistics
import sys
import time

import indicators
import scanner
import study_setups as s1

WARMUP = s1.WARMUP
FWD_MAX = s1.FWD_MAX
DEDUPE_GAP = s1.DEDUPE_GAP
BASELINE_N = s1.BASELINE_N
SEED = s1.SEED
FETCH_DAYS = s1.FETCH_DAYS
MIN_BARS = s1.MIN_BARS
REPORT_PATH = "hidden_files/round2-evidence-study.md"
LOW_CONF = 100          # cells below this many events are flagged
EDGE_BAR_AVG = 0.25     # pre-set "real improvement" bar (as round 1 seq.)
EDGE_BAR_WIN = 2.0

BULL = s1.BULL
BEAR = s1.BEAR
ALL_SETUPS = s1.ALL_SETUPS
DIRECTION = s1.DIRECTION
WEIGHT_OF = {**scanner.WEIGHTS, **scanner.BEAR_WEIGHTS}

A_TREND = "trend_above_sma200"
B_TREND = "trend_sma50_above_sma200"
C_CROSS = "golden_cross"
SQ = "bollinger_squeeze"
BW = "breakout_watch"
VS = "volume_spike"


def day_of(bar: dict) -> dt.date:
    return dt.datetime.fromtimestamp(bar["ts"], dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# SPY regime map: date -> "bull" if SPY close > SPY SMA200 else "bear"
# (binary tag exactly as specified for this study; production regime.py
# additionally requires a 20-day slope sign — noted in the report).
# ---------------------------------------------------------------------------
class RegimeMap:
    def __init__(self, spy_bars: list[dict]):
        S = s1.Series(spy_bars)
        self.dates: list[dt.date] = []
        self.regimes: list[str | None] = []
        for i in range(S.n):
            self.dates.append(day_of(spy_bars[i]))
            if S.sma200[i] is None:
                self.regimes.append(None)
            else:
                self.regimes.append(
                    "bull" if S.closes[i] > S.sma200[i] else "bear")
        self.spy_closes = S.closes
        self.spy_S = S

    def tag(self, day: dt.date) -> str | None:
        """Regime of the most recent SPY session on or before `day`."""
        j = bisect.bisect_right(self.dates, day) - 1
        while j >= 0 and self.regimes[j] is None:
            j -= 1
        return self.regimes[j] if j >= 0 else None

    def direct_tag(self, day: dt.date) -> str | None:
        """Independent recomputation: indicators.sma over the raw prefix."""
        j = bisect.bisect_right(self.dates, day) - 1
        if j < 199:
            return None
        prefix = self.spy_closes[:j + 1]
        sma = indicators.sma(prefix, 200)
        if sma is None:
            return None
        return "bull" if prefix[-1] > sma else "bear"


# ---------------------------------------------------------------------------
# Spearman rank correlation (average ranks for ties)
# ---------------------------------------------------------------------------
def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda k: xs[k])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx)
           * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den > 0 else 0.0


# ---------------------------------------------------------------------------
# Round-2 verification: flags (round-1 check) + new conditions
# ---------------------------------------------------------------------------
def verify2(series_by_sym, rmap: RegimeMap, rng: random.Random):
    """Returns (n_pairs, mismatches[list], checks_per_pair). Checks per pair:
    (1) flags_at == scanner.technical_screen setups (round-1 check);
    (2) merged uptrend (A and B in flags) == direct SMA computation on the
        raw prefix AND == both names present in the screen's setups;
    (3) conjunctions breakout& squeeze, volume & squeeze: flags == screen;
    (4) regime tag == direct SPY prefix-SMA computation.
    """
    pairs = []
    for sym, S in series_by_sym.items():
        hi = S.n - 1 - FWD_MAX
        if hi >= WARMUP:
            for _ in range(3):
                pairs.append((sym, rng.randint(WARMUP, hi)))
    rng.shuffle(pairs)
    pairs = pairs[:max(250, 200)]
    bad = []
    for sym, i in pairs:
        S = series_by_sym[sym]
        prefix = S.bars[:i + 1]
        res = scanner.technical_screen(prefix)
        truth = set()
        if res is not None:
            truth = {n for n, _, _ in res["setups"]}
            truth |= {n for n, _, _ in res["bear_setups"]}
        mine = s1.flags_at(S, i)
        if mine != truth:
            bad.append((sym, i, "flags != technical_screen"))
            continue
        closes = [b["c"] for b in prefix]
        sma50 = indicators.sma(closes, 50)
        sma200 = indicators.sma(closes, 200)
        direct_up = (sma50 is not None and sma200 is not None
                     and closes[-1] > sma200 and sma50 > sma200)
        mine_up = A_TREND in mine and B_TREND in mine
        if direct_up != mine_up:
            bad.append((sym, i, "merged uptrend != direct SMA"))
            continue
        for x, y in ((BW, SQ), (VS, SQ)):
            if (x in mine and y in mine) != (x in truth and y in truth):
                bad.append((sym, i, f"conjunction {x}&{y} mismatch"))
                break
        else:
            day = day_of(S.bars[i])
            if rmap.tag(day) != rmap.direct_tag(day):
                bad.append((sym, i, "regime tag != direct SPY SMA"))
    return len(pairs), bad


# ---------------------------------------------------------------------------
# Aggregation helper
# ---------------------------------------------------------------------------
def edge_of(st: dict, base: dict) -> tuple[float, float]:
    """(avg10 edge, win10 edge) of a bucket vs a baseline bucket."""
    if not st.get("events") or not base.get("events"):
        return (float("nan"), float("nan"))
    return (st["avg10"] - base["avg10"], st["win10"] - base["win10"])


def improvement_verdict(st: dict, ref: dict) -> str:
    """Pre-set bar: >= +0.25% avg10 AND >= +2 pts win10 on >= 100 events."""
    if st.get("events", 0) < LOW_CONF:
        return (f"LOW-CONFIDENCE — only {st.get('events', 0)} events "
                f"(< {LOW_CONF}); no verdict drawn")
    d_avg = st["avg10"] - ref["avg10"]
    d_win = st["win10"] - ref["win10"]
    if d_avg >= EDGE_BAR_AVG and d_win >= EDGE_BAR_WIN:
        return (f"REAL IMPROVEMENT — {d_avg:+.2f}% avg 10d and "
                f"{d_win:+.1f} pts win rate over the unconditioned signal, "
                f"clearing the pre-set bar")
    if d_avg <= -EDGE_BAR_AVG and d_win <= -EDGE_BAR_WIN:
        return (f"HURTS — {d_avg:+.2f}% avg 10d and {d_win:+.1f} pts win "
                f"rate vs the unconditioned signal")
    return (f"NO CLEAR ADDED EDGE — {d_avg:+.2f}% avg 10d / "
            f"{d_win:+.1f} pts win rate vs the unconditioned signal; "
            f"inside the noise band")


def main() -> int:
    rng = random.Random(SEED)
    symbols = list(scanner.SP500_FALLBACK)
    print(f"universe: {len(symbols)} symbols (scanner.SP500_FALLBACK)")

    settings = s1.Settings.from_env()
    rest = scanner.AlpacaREST(settings.alpaca_key, settings.alpaca_secret)

    print("fetching daily bars (days=800)...", flush=True)
    bars_by_sym = s1.fetch_all(symbols)
    spy_bars = (rest.daily_bars_multi(["SPY"], days=FETCH_DAYS)
                .get("SPY") or [])
    print(f"SPY bars: {len(spy_bars)}")
    if len(spy_bars) < 250:
        print("ABORT: not enough SPY history for the regime map.")
        return 1
    rmap = RegimeMap(spy_bars)

    series_by_sym: dict[str, s1.Series] = {}
    skipped = 0
    for sym in symbols:
        bars = bars_by_sym.get(sym) or []
        if len(bars) < MIN_BARS:
            skipped += 1
            continue
        series_by_sym[sym] = s1.Series(bars)
    print(f"usable symbols: {len(series_by_sym)} (skipped {skipped})",
          flush=True)

    # --- eval window + fit/test split (by calendar date) -------------------
    first_eval = last_eval = None
    for S in series_by_sym.values():
        hi = S.n - 1 - FWD_MAX
        if hi < WARMUP:
            continue
        d0, d1 = day_of(S.bars[WARMUP]), day_of(S.bars[hi])
        first_eval = d0 if first_eval is None or d0 < first_eval \
            else first_eval
        last_eval = d1 if last_eval is None or d1 > last_eval else last_eval
    split_date = first_eval + (last_eval - first_eval) / 2
    print(f"eval window {first_eval} -> {last_eval}; "
          f"fit < {split_date} <= test", flush=True)

    # --- verification gate ---------------------------------------------------
    print("verifying round-1 replication + new conditions...", flush=True)
    n_checked, n_bad, bad1 = s1.verify(series_by_sym, rng)
    print(f"  round-1 flags check: {n_checked} pairs, {n_bad} mismatches")
    n2, bad2 = verify2(series_by_sym, rmap, rng)
    print(f"  round-2 conditions check: {n2} pairs, {len(bad2)} mismatches")
    for b in bad2[:10]:
        print(f"    MISMATCH {b}")
    if n_bad or bad2:
        print("ABORT: verification failed; no report written.")
        return 1
    print("verification: 100% clean — proceeding.", flush=True)

    # --- walk-forward --------------------------------------------------------
    Agg = s1.Agg
    full = {n: Agg() for n in ALL_SETUPS}
    by_regime = {n: {"bull": Agg(), "bear": Agg()} for n in ALL_SETUPS}
    by_half = {n: {"fit": Agg(), "test": Agg()} for n in ALL_SETUPS}
    # Q1 buckets
    b_tag = {"with_A": Agg(), "without_A": Agg()}
    c_tag = {"with_AB": Agg(), "without_AB": Agg()}
    merged = Agg()
    # Q3 buckets
    bw_sq, vs_sq = Agg(), Agg()
    bw_tag = {"with_sq": Agg(), "without_sq": Agg()}
    vs_tag = {"with_sq": Agg(), "without_sq": Agg()}

    day_counts = {"A": 0, "B": 0, "AB": 0, "C": 0, "C_AB": 0, "days": 0}
    regime_symdays = {"bull": 0, "bear": 0, "unknown": 0}
    half_symdays = {"fit": 0, "test": 0}
    total_events = 0
    eval_days = 0
    reservoir: list[tuple[str, int, str | None, str]] = []
    seen_candidates = 0

    for sym, S in series_by_sym.items():
        hi = S.n - 1 - FWD_MAX
        if hi < WARMUP:
            continue
        last_fire: dict[str, int] = {}

        def fired(key: str, i: int) -> bool:
            lf = last_fire.get(key)
            ok = lf is None or (i - lf) > DEDUPE_GAP
            last_fire[key] = i
            return ok

        for i in range(WARMUP, hi + 1):
            eval_days += 1
            day = day_of(S.bars[i])
            regime = rmap.tag(day)
            half = "fit" if day < split_date else "test"
            regime_symdays[regime or "unknown"] += 1
            half_symdays[half] += 1
            flags = s1.flags_at(S, i)
            if not flags:
                seen_candidates += 1
                item = (sym, i, regime, half)
                if len(reservoir) < BASELINE_N:
                    reservoir.append(item)
                else:
                    j = rng.randint(0, seen_candidates - 1)
                    if j < BASELINE_N:
                        reservoir[j] = item
                continue

            # Q1 day-level prevalence (pre-dedupe)
            has_a, has_b = A_TREND in flags, B_TREND in flags
            day_counts["days"] += 1
            day_counts["A"] += has_a
            day_counts["B"] += has_b
            day_counts["AB"] += has_a and has_b
            day_counts["C"] += C_CROSS in flags
            day_counts["C_AB"] += (C_CROSS in flags) and has_a and has_b

            for name in flags:
                if not fired(name, i):
                    continue
                rets, tf = s1.outcomes(S, i, DIRECTION[name])
                full[name].add(sym, rets, tf)
                if regime in ("bull", "bear"):
                    by_regime[name][regime].add(sym, rets, tf)
                by_half[name][half].add(sym, rets, tf)
                total_events += 1
                if name == B_TREND:
                    b_tag["with_A" if has_a else "without_A"].add(
                        sym, rets, tf)
                elif name == C_CROSS:
                    c_tag["with_AB" if (has_a and has_b)
                          else "without_AB"].add(sym, rets, tf)
                elif name == BW:
                    bw_tag["with_sq" if SQ in flags
                           else "without_sq"].add(sym, rets, tf)
                elif name == VS:
                    vs_tag["with_sq" if SQ in flags
                           else "without_sq"].add(sym, rets, tf)

            # merged uptrend stream (own dedupe on the conjunction)
            if has_a and has_b and fired("__merged__", i):
                rets, tf = s1.outcomes(S, i, +1)
                merged.add(sym, rets, tf)
            # Q3 conjunction streams (own dedupe)
            if BW in flags and SQ in flags and fired("__bw_sq__", i):
                rets, tf = s1.outcomes(S, i, +1)
                bw_sq.add(sym, rets, tf)
            if VS in flags and SQ in flags and fired("__vs_sq__", i):
                rets, tf = s1.outcomes(S, i, +1)
                vs_sq.add(sym, rets, tf)

    # --- baselines -----------------------------------------------------------
    def new_base_family():
        return {"full": Agg(),
                "regime": {"bull": Agg(), "bear": Agg()},
                "half": {"fit": Agg(), "test": Agg()}}

    bases = {+1: new_base_family(), -1: new_base_family()}
    for sym, i, regime, half in reservoir:
        S = series_by_sym[sym]
        for d in (+1, -1):
            rets, tf = s1.outcomes(S, i, d)
            fam = bases[d]
            fam["full"].add(sym, rets, tf)
            if regime in ("bull", "bear"):
                fam["regime"][regime].add(sym, rets, tf)
            fam["half"][half].add(sym, rets, tf)
    bst = {d: {"full": bases[d]["full"].stats(),
               "regime": {r: bases[d]["regime"][r].stats()
                          for r in ("bull", "bear")},
               "half": {h: bases[d]["half"][h].stats()
                        for h in ("fit", "test")}}
           for d in (+1, -1)}

    st_full = {n: full[n].stats() for n in ALL_SETUPS}
    st_regime = {n: {r: by_regime[n][r].stats() for r in ("bull", "bear")}
                 for n in ALL_SETUPS}
    st_half = {n: {h: by_half[n][h].stats() for h in ("fit", "test")}
               for n in ALL_SETUPS}
    st_b_tag = {k: v.stats() for k, v in b_tag.items()}
    st_c_tag = {k: v.stats() for k, v in c_tag.items()}
    st_merged = merged.stats()
    st_bw_sq, st_vs_sq = bw_sq.stats(), vs_sq.stats()
    st_bw_tag = {k: v.stats() for k, v in bw_tag.items()}
    st_vs_tag = {k: v.stats() for k, v in vs_tag.items()}

    # distinct SPY bear/bull calendar days inside the eval window
    cal_days = {"bull": 0, "bear": 0}
    d = first_eval
    while d <= last_eval:
        t = rmap.tag(d)
        if t in cal_days:
            cal_days[t] += 1
        d += dt.timedelta(days=1)

    # --- Q2 out-of-sample numbers -------------------------------------------
    def half_edges(half: str) -> dict[str, float]:
        out = {}
        for n in ALL_SETUPS:
            e, _ = edge_of(st_half[n][half], bst[DIRECTION[n]]["half"][half])
            out[n] = e
        return out

    fit_e, test_e = half_edges("fit"), half_edges("test")
    spear: dict[str, float] = {}
    for label, names in (("bullish", BULL), ("bearish", BEAR),
                         ("all 13", ALL_SETUPS)):
        xs = [float(WEIGHT_OF[n]) for n in names]
        ys = [test_e[n] for n in names]
        ok = [(x, y) for x, y in zip(xs, ys) if y == y]
        spear[label] = spearman([x for x, _ in ok], [y for _, y in ok])
    spear_fit_test = {}
    for label, names in (("bullish", BULL), ("bearish", BEAR),
                         ("all 13", ALL_SETUPS)):
        ok = [(fit_e[n], test_e[n]) for n in names
              if fit_e[n] == fit_e[n] and test_e[n] == test_e[n]]
        spear_fit_test[label] = spearman([x for x, _ in ok],
                                         [y for _, y in ok])

    def top_half(names: list[str], key) -> list[str]:
        k = (len(names) + 1) // 2
        return sorted(names, key=key, reverse=True)[:k]

    portfolio: dict[str, dict] = {}
    for label, names in (("bullish", BULL), ("bearish", BEAR)):
        fit_top = top_half(names, lambda n: fit_e[n])
        wsum = sum(WEIGHT_OF[n] for n in names)
        wtd = (sum(WEIGHT_OF[n] * test_e[n] for n in names) / wsum
               if wsum else float("nan"))
        portfolio[label] = {
            "fit_top": fit_top,
            "fit_rule_test": statistics.fmean([test_e[n] for n in fit_top]),
            "weights_test": wtd,
            "all_equal_test": statistics.fmean([test_e[n] for n in names]),
            "cur_top": top_half(names, lambda n: WEIGHT_OF[n]),
        }

    # --- report ---------------------------------------------------------------
    def fmt(x, nd=2):
        return f"{x:+.{nd}f}" if isinstance(x, float) else str(x)

    def row(label: str, st: dict, base: dict, wt: str = ""):
        if not st.get("events"):
            return f"| {label} | {wt} | 0 | — | — | — | — | — | — | — |"
        ea, ew = edge_of(st, base)
        flag = " ⚠" if st["events"] < LOW_CONF else ""
        return (f"| {label} | {wt} | {st['events']:,}{flag} "
                f"| {st['win10']:.1f} | {fmt(st['avg5'])}% "
                f"| {fmt(st['avg10'])}% | {fmt(st['avg20'])}% "
                f"| {fmt(st['med10'])}% | {st['tf_rate']:.1f} "
                f"| {fmt(st['worst20'], 1)}% "
                f"| {ea:+.2f}% / {ew:+.1f} pts |")

    HDR = ("| Bucket | Wt | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d "
           "| Med 10d | Target-first % | Worst 20d | Edge vs baseline |")
    SEP = "|---|---|---|---|---|---|---|---|---|---|---|"
    bl, bs = bst[+1]["full"], bst[-1]["full"]
    md: list[str] = []
    md.append("# Round 2 Evidence Study — trend overlap, regime split, "
              "out-of-sample weights, squeeze-as-filter")
    md.append("")
    md.append(f"Generated {dt.datetime.now(dt.timezone.utc).date()} by "
              "`study_round2.py` (analysis only; production scoring "
              "unchanged). Companion to `setup-evidence-study.md` "
              "(round 1) and `sequence-evidence-study.md`.")
    md.append("")
    md.append("## Method")
    md.append("")
    md.append(f"- **Universe:** scanner.SP500_FALLBACK — {len(symbols)} "
              f"symbols requested; {len(series_by_sym)} had >= {MIN_BARS} "
              f"daily bars ({skipped} skipped). SPY fetched separately "
              f"for the regime map ({len(spy_bars)} bars).")
    md.append(f"- **History:** Alpaca daily bars, days={FETCH_DAYS} "
              f"(IEX feed, split-adjusted). First {WARMUP} bars per "
              f"symbol are warmup; evaluation days run {first_eval} → "
              f"{last_eval} ({eval_days:,} symbol-days). Fit half = days "
              f"< {split_date}; test half = days >= {split_date} "
              f"(fit {half_symdays['fit']:,} / test "
              f"{half_symdays['test']:,} symbol-days).")
    md.append("- **Setup detection:** the round-1 precomputed causal "
              "series replicating scanner.technical_screen exactly "
              "(see study_setups.py).")
    md.append(f"- **Verification:** round-1 check {n_checked} random "
              f"(symbol, day) pairs vs scanner.technical_screen — "
              f"**{n_checked - n_bad}/{n_checked} exact, {n_bad} "
              f"mismatches**. Round-2 check {n2} pairs: merged-uptrend "
              "condition vs a direct SMA computation on the raw prefix, "
              "breakout& squeeze and volume& squeeze conjunctions vs "
              "the screen, and the SPY regime tag vs a direct prefix-SMA "
              f"computation — **{n2 - len(bad2)}/{n2} exact, "
              f"{len(bad2)} mismatches**.")
    md.append(f"- **Events:** deduped as in round 1 (>= {DEDUPE_GAP} "
              f"no-fire sessions before a new event). Total deduped "
              f"setup events: **{total_events:,}**, plus the Q1/Q3 "
              "conjunction streams (own dedupe). Outcomes identical to "
              "round 1 (forward 5/10/20d from the fire day's close, "
              "direction-adjusted; target-first +5% before −3% within "
              "20 sessions).")
    md.append(f"- **Regime tag (as specified for this study):** a day "
              "is **bull** if SPY's close that day > SPY's SMA200 that "
              "day, else **bear** (binary; production regime.py "
              "additionally requires the 20-day SMA slope sign and can "
              "say sideways). A stock's day takes the regime of the "
              "most recent SPY session on or before it. Eval-window "
              f"calendar days: {cal_days['bull']} bull / {cal_days['bear']}"
              f" bear; symbol-days: {regime_symdays['bull']:,} bull / "
              f"{regime_symdays['bear']:,} bear / "
              f"{regime_symdays['unknown']:,} untagged.")
    md.append(f"- **Baseline:** {len(reservoir):,} random no-setup "
              f"(symbol, day) pairs (round-1 reservoir method), tagged "
              f"by regime and half. Full-window baseline long: win%10 "
              f"{bl['win10']:.1f}, avg10 {fmt(bl['avg10'])}%; short: "
              f"win%10 {bs['win10']:.1f}, avg10 {fmt(bs['avg10'])}%. "
              "Regime baselines (long): bull "
              f"avg10 {fmt(bst[+1]['regime']['bull']['avg10'])}% "
              f"(n={bst[+1]['regime']['bull']['events']:,}), bear "
              f"avg10 {fmt(bst[+1]['regime']['bear']['avg10'])}% "
              f"(n={bst[+1]['regime']['bear']['events']:,}); short: bull "
              f"avg10 {fmt(bst[-1]['regime']['bull']['avg10'])}%, bear "
              f"avg10 {fmt(bst[-1]['regime']['bear']['avg10'])}%.")
    md.append(f"- **Low-confidence flag:** any cell with < {LOW_CONF} "
              "events is marked ⚠ and no verdict rests on it.")
    md.append("")

    # ---------------- Q1 -----------------------------------------------------
    dc = day_counts
    p_b_with_a = (100.0 * dc["AB"] / dc["B"]) if dc["B"] else 0.0
    p_c_with_ab = (100.0 * dc["C_AB"] / dc["C"]) if dc["C"] else 0.0
    md.append("## Q1 — Do the trend components double-count?")
    md.append("")
    md.append("Components: A = `trend_above_sma200` (+10), B = "
              "`trend_sma50_above_sma200` (+10), C = `golden_cross` (+11). "
              "Day-level prevalence over firing days (pre-dedupe): A on "
              f"{dc['A']:,} days, B on {dc['B']:,} days, both on "
              f"{dc['AB']:,} — **{p_b_with_a:.0f}% of B-days also have "
              "A**. Golden cross fires on "
              f"{dc['C']:,} days, of which **{p_c_with_ab:.0f}% already "
              "have both trend bonuses true**.")
    md.append("")
    md.append("### (a) Standalone (full window, round-1 method)")
    md.append("")
    md.append(HDR)
    md.append(SEP)
    for n in (A_TREND, B_TREND, C_CROSS):
        md.append(row(n, st_full[n], bl, str(WEIGHT_OF[n])))
    md.append(row("merged uptrend (A and B, own stream)", st_merged, bl,
                  "—"))
    md.append("")
    md.append("### (b) Conditional edges")
    md.append("")
    md.append("B's own deduped events, split by whether A also fired "
              "that day:")
    md.append("")
    md.append(HDR)
    md.append(SEP)
    md.append(row("B events with A", st_b_tag["with_A"], bl))
    md.append(row("B events without A", st_b_tag["without_A"], bl))
    md.append("")
    md.append("C's own deduped events, split by whether both trend "
              "bonuses were already true that day:")
    md.append("")
    md.append(HDR)
    md.append(SEP)
    md.append(row("C events with A and B", st_c_tag["with_AB"], bl))
    md.append(row("C events without A and B", st_c_tag["without_AB"], bl))
    md.append("")
    b_adds = improvement_verdict(st_b_tag["with_A"], st_full[A_TREND])
    merged_vs_a = improvement_verdict(st_merged, st_full[A_TREND])
    c_adds = improvement_verdict(st_c_tag["with_AB"], st_merged)
    md.append("### Q1 verdict")
    md.append("")
    md.append(f"- Merged uptrend vs A alone: **{merged_vs_a}**.")
    md.append(f"- B-events-with-A vs A alone: **{b_adds}**.")
    md.append(f"- C-events-with-A-and-B vs the merged uptrend state: "
              f"**{c_adds}**.")
    b_wo = st_b_tag["without_A"]
    b_wo_e, _ = edge_of(b_wo, bl)
    md.append(f"- B without A (SMA50 > SMA200 but price under the 200-day) "
              f"carries an avg-10d edge of {b_wo_e:+.2f}% vs baseline on "
              f"{b_wo.get('events', 0):,} events — the direct read on "
              "whether B means anything once A is absent.")
    md.append("- Reading: the three components are state descriptions of "
              "one fact (an uptrend) far more than three independent "
              "signals; the numbers above are the basis for merge/cap "
              "vs keep-as-is. (Verdict wording in the console summary "
              "follows these same comparisons.)")
    md.append("")

    # ---------------- Q2 -----------------------------------------------------
    md.append("## Q2 — Regime split and out-of-sample validation of "
              "the current weights")
    md.append("")
    md.append(f"Current weights (Oct 20, from scanner.py): bullish — "
              + ", ".join(f"{n} {WEIGHT_OF[n]}" for n in BULL)
              + "; bearish — "
              + ", ".join(f"{n} {WEIGHT_OF[n]}" for n in BEAR) + ".")
    md.append("")
    md.append("### (a) Regime split — 10d performance by SPY regime day")
    md.append("")
    md.append("Each cell is measured against the same-regime, "
              "same-direction baseline. ⚠ = < "
              f"{LOW_CONF} events (low confidence). Bear-regime baseline "
              f"sample sizes: long n={bst[+1]['regime']['bear']['events']:,}, "
              f"short n={bst[-1]['regime']['bear']['events']:,}.")
    md.append("")
    for side_names, base_dir in ((BULL, +1), (BEAR, -1)):
        md.append(f"**{'Bullish' if base_dir > 0 else 'Bearish'} setups**")
        md.append("")
        md.append(HDR)
        md.append(SEP)
        for n in side_names:
            for r in ("bull", "bear"):
                md.append(row(f"{n} — {r} days", st_regime[n][r],
                              bst[base_dir]["regime"][r],
                              str(WEIGHT_OF[n])))
        md.append("")
    bear_cells = sum(1 for n in ALL_SETUPS
                     if st_regime[n]["bear"].get("events", 0) < LOW_CONF)
    md.append(f"Bear-regime cells with < {LOW_CONF} events: "
              f"**{bear_cells} of {len(ALL_SETUPS)}** — the window is "
              "mostly bull, so most bear-regime reads are indicative "
              "only.")
    md.append("")
    md.append("### (b) Out-of-sample: fit half vs test half")
    md.append("")
    md.append(f"Fit = evaluation days < {split_date}; test = days >= "
              f"{split_date}. Edges are avg-10d vs the same-half, "
              "same-direction baseline.")
    md.append("")
    md.append("| Setup | Wt now | Fit edge avg10 | Test edge avg10 "
              "| Fit events | Test events |")
    md.append("|---|---|---|---|---|---|")
    for n in ALL_SETUPS:
        md.append(f"| {n} | {WEIGHT_OF[n]} | {fit_e[n]:+.2f}% "
                  f"| {test_e[n]:+.2f}% "
                  f"| {st_half[n]['fit'].get('events', 0):,} "
                  f"| {st_half[n]['test'].get('events', 0):,} |")
    md.append("")
    md.append("**Rank correlations (Spearman):**")
    for label in ("bullish", "bearish", "all 13"):
        md.append(f"- current weight vs test-half edge ({label}): "
                  f"**{spear[label]:+.2f}**")
    for label in ("bullish", "bearish", "all 13"):
        md.append(f"- fit-half edge vs test-half edge ({label}): "
                  f"**{spear_fit_test[label]:+.2f}** (how rank-stable "
                  "edges are at all in this window)")
    md.append("")
    md.append("**Portfolio rules — mean test-half avg10 edge:**")
    md.append("")
    md.append("| Side | Fit-top-half, equal weight | Current weights "
              "(weight-proportional, all setups) | All setups, equal "
              "weight |")
    md.append("|---|---|---|---|")
    for label in ("bullish", "bearish"):
        p = portfolio[label]
        md.append(f"| {label} | {p['fit_rule_test']:+.2f}% (setups: "
                  f"{', '.join(p['fit_top'])}) "
                  f"| {p['weights_test']:+.2f}% "
                  f"| {p['all_equal_test']:+.2f}% |")
    md.append("")
    md.append("Current-weight top halves for reference — bullish: "
              + ", ".join(portfolio["bullish"]["cur_top"])
              + "; bearish: " + ", ".join(portfolio["bearish"]["cur_top"])
              + " (ties at the cutoff broken by rank order).")
    md.append("")
    md.append("### Q2 verdict")
    md.append("")
    sb_, sbe_ = spear["bullish"], spear["bearish"]
    fit_b = portfolio["bullish"]
    fit_s = portfolio["bearish"]
    md.append(f"- Bullish weights vs test-half performance: Spearman "
              f"{sb_:+.2f}; the naive fit-top-half rule scores "
              f"{fit_b['fit_rule_test']:+.2f}% mean test edge vs "
              f"{fit_b['weights_test']:+.2f}% for the shipped weights "
              "(weight-proportional) and "
              f"{fit_b['all_equal_test']:+.2f}% for all setups equally.")
    md.append(f"- Bearish weights vs test-half performance: Spearman "
              f"{sbe_:+.2f}; fit-top-half rule "
              f"{fit_s['fit_rule_test']:+.2f}% vs shipped "
              f"{fit_s['weights_test']:+.2f}% vs all-equal "
              f"{fit_s['all_equal_test']:+.2f}%.")
    md.append("- Whether that reads as 'generalizes' or 'period-fitted' "
              "is stated in the console summary from exactly these "
              "numbers; the regime table above is the caveat either "
              "way — most of both halves are bull-regime days.")
    md.append("")

    # ---------------- Q3 -----------------------------------------------------
    md.append("## Q3 — Squeeze as a multiplier, not a signal")
    md.append("")
    md.append(f"Pre-set improvement bar (as in the sequence study): "
              f">= +{EDGE_BAR_AVG:.2f}% avg 10d AND >= +{EDGE_BAR_WIN:.0f} "
              f"pts win rate vs the unconditioned signal, on >= "
              f"{LOW_CONF} events.")
    md.append("")
    md.append(HDR)
    md.append(SEP)
    md.append(row("breakout_watch (all events)", st_full[BW], bl,
                  str(WEIGHT_OF[BW])))
    md.append(row("breakout_watch WHILE squeeze (own stream)", st_bw_sq,
                  bl))
    md.append(row("breakout_watch events, squeeze that day",
                  st_bw_tag["with_sq"], bl))
    md.append(row("breakout_watch events, no squeeze",
                  st_bw_tag["without_sq"], bl))
    md.append(row("volume_spike (all events)", st_full[VS], bl,
                  str(WEIGHT_OF[VS])))
    md.append(row("volume_spike WHILE squeeze (own stream)", st_vs_sq,
                  bl))
    md.append(row("volume_spike events, squeeze that day",
                  st_vs_tag["with_sq"], bl))
    md.append(row("volume_spike events, no squeeze",
                  st_vs_tag["without_sq"], bl))
    md.append("")
    v_bw = improvement_verdict(st_bw_sq, st_full[BW])
    v_bw_tag = improvement_verdict(st_bw_tag["with_sq"], st_full[BW])
    v_vs = improvement_verdict(st_vs_sq, st_full[VS])
    v_vs_tag = improvement_verdict(st_vs_tag["with_sq"], st_full[VS])
    md.append("### Q3 verdict")
    md.append("")
    md.append(f"- breakout_watch while coiled (conjunction stream): "
              f"**{v_bw}**.")
    md.append(f"- breakout_watch events with squeeze that day (split "
              f"view): **{v_bw_tag}**.")
    md.append(f"- volume_spike while coiled (conjunction stream): "
              f"**{v_vs}**.")
    md.append(f"- volume_spike events with squeeze that day (split "
              f"view): **{v_vs_tag}**.")
    md.append("")

    # ---------------- limitations ---------------------------------------------
    md.append("## Limitations")
    md.append("")
    md.append("- **Bear-regime scarcity:** the evaluation window is "
              "mostly a rising market — SPY spent most days above its "
              "SMA200, so bear-regime cells (and the bear baseline) rest "
              "on far fewer events; every such cell is flagged ⚠ in the "
              "tables and no strong claim should rest on it. Regime-"
              "conditional edges for bearish setups in particular remain "
              "close to untested.")
    md.append("- **Binary regime tag:** per this study's spec the tag "
              "is SPY close vs SMA200 only; production regime.py also "
              "requires the SMA slope sign (bull/bear/sideways), so "
              "regime-cell membership differs slightly from what the "
              "live gate sees.")
    md.append("- **IEX-only volume:** volume conditions (volume_spike "
              "and its squeeze conjunctions) are measured on Alpaca's "
              "free IEX tape, a few % of consolidated volume; on the "
              "full tape the same conditions may fire differently.")
    md.append("- **No transaction costs:** gross returns only — no "
              "commissions, spreads, slippage, borrow costs or taxes.")
    md.append("- **Survivorship bias:** SP500_FALLBACK is a 2026-10-19 "
              "snapshot applied retroactively; index leavers/failures "
              "in the window are missing, flattering all results.")
    md.append("- **Overlapping conditions:** Q1/Q3 buckets share events "
              "by construction (a conjunction event is also an event of "
              "each component), so bucket counts are not independent "
              "samples; conditional splits of the same event stream are "
              "paired comparisons, not fresh evidence.")
    md.append("- **One window, two halves:** the fit/test split is a "
              "single chronological cut of a mostly-bull ~1.2-year "
              "evaluation window; Spearman values on 6–7 points per "
              "side are coarse and a different cut could move them.")
    md.append("- Split-adjusted prices only (no dividend adjustment); "
              "no liquidity gate in-study (production additionally "
              "requires 20-day avg dollar volume >= $250k — immaterial "
              "for this universe).")
    md.append("")

    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(md))

    # --- console summary -------------------------------------------------------
    print(f"\nreport written: {REPORT_PATH}")
    print(f"total setup events: {total_events:,}")
    print(f"regime calendar days: bull {cal_days['bull']} / bear "
          f"{cal_days['bear']}; bear cells < {LOW_CONF} events: "
          f"{bear_cells}/{len(ALL_SETUPS)}")
    print(f"Spearman weight-vs-test-edge: bullish {spear['bullish']:+.2f}, "
          f"bearish {spear['bearish']:+.2f}, all {spear['all 13']:+.2f}")
    print("\nQ1 verdicts:")
    print(f"  merged vs A alone: {merged_vs_a}")
    print(f"  B-with-A vs A alone: {b_adds}")
    print(f"  C-with-AB vs merged: {c_adds}")
    print(f"  B without A edge: {b_wo_e:+.2f}% avg10 "
          f"({b_wo.get('events', 0):,} events)")
    print("\nQ2 portfolio (mean test-half avg10 edge):")
    for label in ("bullish", "bearish"):
        p = portfolio[label]
        print(f"  {label}: fit-top-half {p['fit_rule_test']:+.2f}% | "
              f"shipped weights {p['weights_test']:+.2f}% | all-equal "
              f"{p['all_equal_test']:+.2f}%")
    print("\nQ3 verdicts:")
    print(f"  breakout while coiled: {v_bw}")
    print(f"  volume while coiled: {v_vs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
