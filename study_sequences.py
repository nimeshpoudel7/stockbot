#!/usr/bin/env python3
"""Sequence evidence study — do CONFIRMED breakouts beat raw breakouts?

Extends study_setups.py (same universe, fetch, causal series replication,
verification discipline, forward-outcome machinery, and dedupe rule) to
 Bollinger upper-band breakout EVENTS and their confirmed variants:

  A  raw breakout            close crosses above the Bollinger(20, 2σ)
                             upper band today (yesterday's close <= band)
  B  breakout + volume       A and today's volume >= 1.5x the median of
                             the prior 20 sessions
  C  squeeze -> breakout     A and the scanner's bollinger_squeeze
                             condition held on >= 1 of the prior 10 sessions
  D  squeeze -> breakout     A + B + C
       + volume
  E  confirmed breakout      second consecutive close above the band
                             (event day = the second close); also split
                             as E+vol when the volume condition holds
  F  walking the band        close above the band on >= 3 of the last 5
                             sessions (event day = first day count hits 3)

All bullish, direction +1, scored against the same no-setup baseline as
study 1 (random symbol-days on which none of the 13 scanner setups fired).

Analysis-only: no production file is touched.

Run:  .venv/bin/python study_sequences.py
Out:  hidden_files/sequence-evidence-study.md  (+ console summary)
"""
from __future__ import annotations

import datetime as dt
import random
import statistics
import sys
import time

import indicators
import scanner
import study_setups as base

WARMUP = base.WARMUP
FWD_MAX = base.FWD_MAX
DEDUPE_GAP = base.DEDUPE_GAP
BASELINE_N = base.BASELINE_N
SEED = base.SEED
MIN_BARS = base.MIN_BARS
REPORT_PATH = "hidden_files/sequence-evidence-study.md"
VERIFY_N = 250
LOW_CONF = 100                  # variants below this are flagged, not hidden

VARIANTS = ["A_raw", "B_vol", "C_sqz", "D_sqz_vol", "E_second",
            "E_second_vol", "F_walk"]
LABELS = {
    "A_raw": "A — raw breakout",
    "B_vol": "B — breakout + volume",
    "C_sqz": "C — squeeze → breakout",
    "D_sqz_vol": "D — squeeze → breakout + volume",
    "E_second": "E — confirmed (2nd close)",
    "E_second_vol": "E+vol — confirmed + volume",
    "F_walk": "F — walking the band",
}


# ---------------------------------------------------------------------------
# Series extension: Bollinger mid/upper (two-pass, indicators' own formula),
# above-band booleans, the scanner's squeeze-day condition, volume condition,
# and prefix sums for the windowed variants.
# ---------------------------------------------------------------------------
class SeqSeries(base.Series):
    def __init__(self, bars: list[dict]):
        super().__init__(bars)
        n = self.n
        c = self.closes

        # Bollinger mid/upper with indicators.bollinger_bands' exact
        # arithmetic (SMA20, population variance, two-pass).
        self.bb_mid: list[float | None] = [None] * n
        self.bb_upper: list[float | None] = [None] * n
        for i in range(19, n):
            w = c[i - 19:i + 1]
            mid = sum(w) / 20.0
            var = sum((v - mid) ** 2 for v in w) / 20.0
            sd = var ** 0.5
            self.bb_mid[i] = mid
            self.bb_upper[i] = mid + 2.0 * sd

        # Close above the upper band?
        self.above: list[bool] = [
            self.bb_upper[i] is not None and c[i] > self.bb_upper[i]
            for i in range(n)]

        # Scanner bollinger_squeeze condition per day j, using the base
        # Series bandwidth array exactly as study_setups.flags_at does:
        # bw[j] not None, prefix >= 146 bars, bw[j] <= min(bw[j-126..j])*1.02.
        self.squeeze: list[bool] = [False] * n
        for j in range(n):
            bw = self.bw[j]
            if bw is not None and (j + 1) >= 126 + 20:
                hist = [h for h in self.bw[j - 126:j + 1] if h is not None]
                if hist and bw <= min(hist) * 1.02:
                    self.squeeze[j] = True

        # Volume condition: today's volume >= 1.5x median of prior 20.
        self.vol_ok: list[bool] = [False] * n
        for i in range(20, n):
            med = self.vol_med[i]
            if med is not None and med > 0 and self.vols[i] >= 1.5 * med:
                self.vol_ok[i] = True

        # Prefix sums: above-band counts and squeeze-any windows.
        self._above_ps = [0] * (n + 1)
        self._sqz_ps = [0] * (n + 1)
        for i in range(n):
            self._above_ps[i + 1] = self._above_ps[i] + (1 if self.above[i]
                                                         else 0)
            self._sqz_ps[i + 1] = self._sqz_ps[i] + (1 if self.squeeze[i]
                                                     else 0)

    def above_count(self, i: int, window: int = 5) -> int:
        """Closes above the band among bars[i-window+1 .. i]."""
        lo = max(0, i - window + 1)
        return self._above_ps[i + 1] - self._above_ps[lo]

    def squeeze_recent(self, i: int, window: int = 10) -> bool:
        """Scanner squeeze condition held on >= 1 of the PRIOR `window`
        sessions (bars[i-window .. i-1]; the breakout day itself excluded)."""
        lo = max(0, i - window)
        return (self._sqz_ps[i] - self._sqz_ps[lo]) > 0


def variant_flags(S: SeqSeries, i: int) -> set[str]:
    """Sequence-variant names firing at bar i (all bullish)."""
    out: set[str] = set()
    if i < 1:
        return out
    cross = S.above[i] and not S.above[i - 1]          # variant A event
    if cross:
        out.add("A_raw")
        if S.vol_ok[i]:
            out.add("B_vol")
        sq = S.squeeze_recent(i)
        if sq:
            out.add("C_sqz")
        if sq and S.vol_ok[i]:
            out.add("D_sqz_vol")
    if S.above[i] and S.above[i - 1]:                  # variant E event
        out.add("E_second")
        if S.vol_ok[i]:
            out.add("E_second_vol")
    cnt = S.above_count(i, 5)                          # variant F event
    if cnt >= 3 and S.above_count(i - 1, 5) < 3:
        out.add("F_walk")
    return out


# ---------------------------------------------------------------------------
# Verification: band values vs indicators.*, squeeze vs the real screen.
# ---------------------------------------------------------------------------
def verify(series_by_sym: dict[str, SeqSeries],
           rng: random.Random) -> tuple[int, int, list]:
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
        prefix = S.bars[:i + 1]
        closes = S.closes[:i + 1]
        mid, upper, lower = indicators.bollinger_bands(closes)
        bw_true = indicators.bollinger_bandwidth(closes)
        res = scanner.technical_screen(prefix)
        screen_sqz = bool(res) and any(
            nm == "bollinger_squeeze" for nm, _, _ in res["setups"])
        problems = []
        if upper is None or S.bb_upper[i] is None or \
                abs(S.bb_upper[i] - upper) > 1e-6 * max(1.0, abs(upper)):
            problems.append(f"upper mine={S.bb_upper[i]} true={upper}")
        if lower is None or S.bb_lower[i] is None or \
                abs(S.bb_lower[i] - lower) > 1e-6 * max(1.0, abs(lower)):
            problems.append(f"lower mine={S.bb_lower[i]} true={lower}")
        if bw_true is None or S.bw[i] is None or \
                abs(S.bw[i] - bw_true) > 1e-9 * max(1.0, abs(bw_true)):
            problems.append(f"bw mine={S.bw[i]} true={bw_true}")
        if S.squeeze[i] != screen_sqz:
            problems.append(f"squeeze mine={S.squeeze[i]} "
                            f"screen={screen_sqz}")
        med_true = statistics.median(S.vols[i - 20:i]) if i >= 20 else None
        if med_true != S.vol_med[i]:
            problems.append(f"vol_med mine={S.vol_med[i]} true={med_true}")
        above_true = upper is not None and closes[-1] > upper
        if S.above[i] != above_true:
            problems.append(f"above mine={S.above[i]} true={above_true}")
        if problems:
            bad.append((sym, i, problems))
    return len(pairs), len(bad), bad


# ---------------------------------------------------------------------------
# Main study
# ---------------------------------------------------------------------------
def main() -> int:
    rng = random.Random(SEED)
    symbols = list(scanner.SP500_FALLBACK)
    print(f"universe: {len(symbols)} symbols (scanner.SP500_FALLBACK)")

    print("fetching daily bars (days=800)...", flush=True)
    bars_by_sym = base.fetch_all(symbols)

    series_by_sym: dict[str, SeqSeries] = {}
    skipped = 0
    for sym in symbols:
        bars = bars_by_sym.get(sym) or []
        if len(bars) < MIN_BARS:
            skipped += 1
            continue
        series_by_sym[sym] = SeqSeries(bars)
    print(f"usable symbols: {len(series_by_sym)} (skipped {skipped} "
          f"with < {MIN_BARS} bars)", flush=True)

    # --- verification gate -------------------------------------------------
    print("verifying replication (bands vs indicators.*, squeeze vs "
          "scanner.technical_screen)...", flush=True)
    n_checked, n_bad, bad = verify(series_by_sym, rng)
    print(f"verification: {n_checked} (symbol, day) pairs, "
          f"{n_bad} mismatches")
    if n_bad:
        for sym, i, problems in bad[:10]:
            print(f"  MISMATCH {sym}@{i}: {problems}")
        print("ABORT: replication is not exact; no report written.")
        return 1
    print("verification: 100% clean — proceeding.", flush=True)

    # --- walk-forward ------------------------------------------------------
    aggs: dict[str, base.Agg] = {v: base.Agg() for v in VARIANTS}
    total_events = 0
    eval_days = 0
    first_eval: dt.date | None = None
    last_eval: dt.date | None = None
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
            # baseline reservoir: days on which NO scanner setup fired
            # (identical construction to study 1)
            if not base.flags_at(S, i):
                seen_candidates += 1
                item = (sym, i)
                if len(reservoir) < BASELINE_N:
                    reservoir.append(item)
                else:
                    j = rng.randint(0, seen_candidates - 1)
                    if j < BASELINE_N:
                        reservoir[j] = item
            for name in variant_flags(S, i):
                lf = last_fire.get(name)
                if lf is None or (i - lf) > DEDUPE_GAP:
                    rets, tf = base.outcomes(S, i, +1)
                    aggs[name].add(sym, rets, tf)
                    total_events += 1
                last_fire[name] = i

    base_long = base.Agg()
    for sym, i in reservoir:
        S = series_by_sym[sym]
        rets, tf = base.outcomes(S, i, +1)
        base_long.add(sym, rets, tf)
    bl = base_long.stats()
    stats = {v: aggs[v].stats() for v in VARIANTS}

    # --- verdicts ------------------------------------------------------------
    # "Confirmation adds real edge" is judged vs variant A with study 1's
    # KEEP-threshold spirit: Δavg10 >= +0.25% AND Δwin10 >= +2 pts, on a
    # sample of >= 100 events.
    def rel(v: str) -> tuple[float, float]:
        st, a = stats[v], stats["A_raw"]
        if not st.get("events") or not a.get("events"):
            return 0.0, 0.0
        return st["avg10"] - a["avg10"], st["win10"] - a["win10"]

    verdicts: dict[str, str] = {}
    a = stats["A_raw"]
    verdicts["A_raw"] = (
        f"CONTROL. Raw upper-band crosses: {a.get('events', 0)} events, "
        f"win%10 {a.get('win10', 0):.1f}, avg10 {a.get('avg10', 0):+.2f}% vs "
        f"baseline {bl['avg10']:+.2f}% — the yardstick every confirmation "
        "below is measured against.")
    for v in VARIANTS[1:]:
        st = stats[v]
        ev = st.get("events", 0)
        davg, dwin = rel(v)
        eavg = st.get("avg10", 0) - bl["avg10"]
        if ev < LOW_CONF:
            verdicts[v] = (f"LOW CONFIDENCE — only {ev} events (< "
                           f"{LOW_CONF}); no conclusion drawn.")
        elif davg >= 0.25 and dwin >= 2.0:
            verdicts[v] = (f"CONFIRMATION ADDS EDGE — beats raw breakouts "
                           f"by {davg:+.2f}% avg 10d and {dwin:+.1f} pts "
                           f"win rate on {ev} events.")
        elif davg <= -0.25 and dwin <= -2.0:
            verdicts[v] = (f"CONFIRMATION HURTS — trails raw breakouts by "
                           f"{davg:+.2f}% avg 10d and {dwin:+.1f} pts win "
                           f"rate on {ev} events.")
        elif eavg < 0 and (st["win10"] - bl["win10"]) < 0:
            verdicts[v] = (f"NO EDGE — worse than the no-setup baseline "
                           f"({eavg:+.2f}% avg 10d) and no better than raw "
                           f"breakouts ({davg:+.2f}% / {dwin:+.1f} pts).")
        else:
            verdicts[v] = (f"NO CLEAR ADDED EDGE — vs raw breakouts "
                           f"{davg:+.2f}% avg 10d / {dwin:+.1f} pts win "
                           f"rate on {ev} events; inside the noise band.")

    # --- report --------------------------------------------------------------
    def fmt(x, nd=2):
        return f"{x:+.{nd}f}" if isinstance(x, float) else str(x)

    def edge_cell(st: dict, ref: dict) -> str:
        if not st.get("events") or not ref.get("events"):
            return "—"
        return (f"{st['avg10'] - ref['avg10']:+.2f}% avg10 / "
                f"{st['win10'] - ref['win10']:+.1f} pts win")

    md = []
    md.append("# Sequence Evidence Study — confirmed vs raw breakouts")
    md.append("")
    md.append(f"Generated {dt.datetime.now(dt.timezone.utc).date()} by "
              "`study_sequences.py` (analysis only; production scoring "
              "unchanged). Companion to `setup-evidence-study.md`.")
    md.append("")
    md.append("## Method")
    md.append("")
    md.append(f"- **Universe:** scanner.SP500_FALLBACK — {len(symbols)} "
              f"symbols requested; {len(series_by_sym)} had >= {MIN_BARS} "
              f"daily bars and were studied ({skipped} skipped).")
    md.append(f"- **History:** Alpaca daily bars fetched with days=800 "
              f"(IEX feed, split-adjusted) — identical fetch to study 1. "
              f"First {WARMUP} bars per symbol are warmup; evaluation days "
              f"run {first_eval} → {last_eval} ({eval_days:,} symbol-days).")
    md.append("- **Events (all bullish):** A = close crosses above the "
              "Bollinger(20, 2σ) upper band (yesterday's close <= "
              "yesterday's band). B = A + volume >= 1.5x median of the "
              "prior 20 sessions. C = A + the scanner's bollinger_squeeze "
              "condition (bandwidth at its 126-day low, within 2%) held "
              "on >= 1 of the prior 10 sessions (breakout day excluded). "
              "D = A + B + C. E = second consecutive close above the band "
              "(event day = second close), plus an E+vol split with the "
              "volume condition. F = close above the band on >= 3 of the "
              "last 5 sessions (event day = first day the count reaches "
              "3) — a momentum-continuation event, not an entry-from-flat.")
    md.append(f"- **Dedupe:** identical to study 1 — an event counts once "
              f"per variant per symbol; a new event requires >= "
              f"{DEDUPE_GAP} no-fire sessions. Total deduped events: "
              f"**{total_events:,}**.")
    md.append("- **Outcomes (from the event day's close):** forward "
              "returns at +5/+10/+20 sessions; target-first = +5% "
              "reached before −3% within 20 sessions on daily highs/lows "
              "(a day hitting both counts as stop-first) — identical "
              "machinery to study 1.")
    md.append(f"- **Baseline:** {len(reservoir):,} random (symbol, day) "
              "pairs on which NO scanner setup fired (identical "
              "construction to study 1), scored long: "
              f"win%10 {bl['win10']:.1f}, avg10 {fmt(bl['avg10'])}%.")
    md.append(f"- **Verification:** {n_checked} random (symbol, day) "
              f"pairs — Bollinger mid/upper/lower and bandwidth from the "
              f"study's series vs indicators.bollinger_bands / "
              f"bollinger_bandwidth on the prefix, the per-day squeeze "
              f"condition vs scanner.technical_screen's bollinger_squeeze "
              f"flag, and the prior-20 volume median vs a direct "
              f"computation — **{n_checked - n_bad}/{n_checked} exact "
              f"matches, {n_bad} mismatches**. (The raw cross itself is a "
              "new definition; its components — band level and close vs "
              "band — are what is verified.)")
    md.append("- **Low-confidence flag:** variants with < "
              f"{LOW_CONF} events are flagged ⚠ and no verdict is drawn "
              "from them.")
    md.append("")
    md.append("## Results")
    md.append("")
    md.append("| Variant | Events | Win% 10d | Avg 5d | Avg 10d | Avg 20d "
              "| Med 10d | Target-first % | Worst 20d | Edge vs baseline "
              "| Edge vs A |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for v in VARIANTS:
        st = stats[v]
        if not st.get("events"):
            md.append(f"| {LABELS[v]} | 0 | — | — | — | — | — | — | — | — "
                      "| — |")
            continue
        flag = " ⚠" if st["events"] < LOW_CONF else ""
        vs_a = "— (control)" if v == "A_raw" else edge_cell(st, a)
        md.append(
            f"| {LABELS[v]} | {st['events']}{flag} "
            f"| {st['win10']:.1f} | {fmt(st['avg5'])}% "
            f"| {fmt(st['avg10'])}% | {fmt(st['avg20'])}% "
            f"| {fmt(st['med10'])}% | {st['tf_rate']:.1f} "
            f"| {fmt(st['worst20'], 1)}% "
            f"| {edge_cell(st, bl)} | {vs_a} |")
    md.append("")
    md.append(f"Baseline (no setup, long): {bl['events']:,} days, "
              f"win%10 {bl['win10']:.1f}, avg10 {fmt(bl['avg10'])}%, "
              f"target-first {bl['tf_rate']:.1f}%.")
    md.append("")
    md.append("### Detail (remaining horizons)")
    md.append("")
    md.append("| Variant | Win% 5d | Win% 20d | Med 5d | Med 20d |")
    md.append("|---|---|---|---|---|")
    for v in VARIANTS:
        st = stats[v]
        if not st.get("events"):
            md.append(f"| {LABELS[v]} | — | — | — | — |")
            continue
        md.append(f"| {LABELS[v]} | {st['win5']:.1f} | {st['win20']:.1f} "
                  f"| {fmt(st['med5'])}% | {fmt(st['med20'])}% |")
    md.append("")
    md.append("## Verdicts")
    md.append("")
    md.append("Criterion: a confirmation 'adds real edge' if, vs variant "
              "A, it gains >= +0.25% avg 10d return AND >= +2 pts 10d "
              "win rate on >= 100 events (study 1's KEEP-threshold "
              "spirit); symmetric thresholds for 'hurts'.")
    md.append("")
    for v in VARIANTS:
        md.append(f"- **{LABELS[v]}**: {verdicts[v]}")
    md.append("")
    md.append("## Recommendation")
    md.append("")
    rec = build_recommendation(stats, verdicts, bl)
    md += rec
    md.append("")
    md.append("## Limitations")
    md.append("")
    md.append("- **IEX-only volume:** Alpaca's free IEX feed carries only "
              "IEX-exchange volume (~a few % of consolidated tape), so "
              "the volume-median conditions in variants B, D and E+vol — "
              "and any volume-based result — are measured on a partial "
              "tape; on the consolidated tape the same variants may fire "
              "differently (relatively more often on high-attention "
              "names).")
    md.append("- **No transaction costs:** returns are gross — no "
              "commissions, spreads, slippage or taxes; real-world "
              "results would be lower, especially for the 5-day horizon.")
    md.append("- **Survivorship bias:** the universe is the built-in "
              "SP500_FALLBACK snapshot (compiled 2026-10-19) applied "
              "retroactively — companies that left the index (or "
              "failed) during the window are missing, which flatters "
              "results.")
    md.append("- **Window/regime:** the ~2-year window (fetch-bounded "
              "by calendar days, ≈575 trading days of bars, ~1.2 years "
              "of post-warmup evaluation) covers a mostly-rising market; "
              "breakout behaviour — especially momentum continuation "
              "(variant F) — may not transfer to a sustained bear or "
              "choppy regime. Variants are also evaluated independently: "
              "B–F overlap heavily with A and with each other, event "
              "counts are not independent samples, and stacked-signal "
              "behaviour with the rest of the scanner's setups is not "
              "validated here.")
    md.append("- Split-adjusted prices only (no dividend adjustment), "
              "and no liquidity gate was applied in the study (the "
              "production scanner additionally requires 20-day avg "
              "dollar volume >= $250k — immaterial for this universe). "
              "Bands use split-adjusted closes on the IEX feed; a close "
              "exactly at the band edge is a measure-zero case decided "
              "by floating point.")
    md.append("")

    with open(REPORT_PATH, "w") as f:
        f.write("\n".join(md))

    # --- console summary -----------------------------------------------------
    print(f"\nreport written: {REPORT_PATH}")
    print(f"total events: {total_events:,}")
    print(f"baseline long: win10 {bl['win10']:.1f}% "
          f"avg10 {bl['avg10']:+.2f}%")
    for v in VARIANTS:
        st = stats[v]
        if st.get("events"):
            print(f"  {LABELS[v]:36s} ev={st['events']:>6,} "
                  f"win10={st['win10']:5.1f}% avg10={st['avg10']:+.2f}% "
                  f"tf={st['tf_rate']:4.1f}%")
            print(f"      verdict: {verdicts[v]}")
        else:
            print(f"  {LABELS[v]:36s} ev=0")
    return 0


def build_recommendation(stats: dict, verdicts: dict,
                         bl: dict) -> list[str]:
    """Data-driven closing recommendation (text built from the numbers)."""
    lines = []
    ranked = sorted(
        (v for v in VARIANTS if stats[v].get("events", 0) >= LOW_CONF),
        key=lambda v: (stats[v]["avg10"] - bl["avg10"]), reverse=True)
    if not ranked:
        lines.append("No variant reached 100 events — no live-signal "
                     "recommendation can be made from this study.")
        return lines
    best = ranked[0]
    a = stats["A_raw"]
    lines.append(f"Ranked by avg-10d edge vs the no-setup baseline, the "
                 f"strongest variant is **{LABELS[best]}** "
                 f"({stats[best]['avg10'] - bl['avg10']:+.2f}% avg 10d, "
                 f"{stats[best]['win10'] - bl['win10']:+.1f} pts win rate, "
                 f"{stats[best]['events']} events).")
    winners = [v for v in VARIANTS[1:]
               if verdicts[v].startswith("CONFIRMATION ADDS EDGE")]
    if winners:
        names = ", ".join(f"**{LABELS[v]}**" for v in winners)
        lines.append(f"Confirmation demonstrably adds edge for: {names}. "
                     "These deserve to become live scanner signals. "
                     "Relative strength: study 1 put the scanner's "
                     "proven tier at weight ~9–11 (golden_cross 11, "
                     "breakout_watch 9, trend bonuses 10). A confirmed-"
                     "breakout signal with the edge shown here belongs "
                     "in that same tier — proposed as a NEW setup at "
                     "weight ~10, fired only in its confirmed form, "
                     "while the raw cross (variant A) stays a 0-weight "
                     "component of breakout_watch rather than a scored "
                     "signal of its own.")
    else:
        lines.append("No confirmation variant cleared the 'adds real "
                     "edge' bar against raw breakouts. On this evidence "
                     "the honest move is to add NO new breakout signal: "
                     "variant A's own edge vs baseline is "
                     f"{a.get('avg10', 0) - bl['avg10']:+.2f}% avg 10d, "
                     "and layering volume/squeeze/second-close "
                     "conditions on top does not improve it beyond "
                     "noise. The existing breakout_watch (proximity to "
                     "the 52-week high, weight 9) remains the scanner's "
                     "breakout exposure.")
    hurt = [v for v in VARIANTS[1:]
            if verdicts[v].startswith("CONFIRMATION HURTS")]
    if hurt:
        names = ", ".join(f"**{LABELS[v]}**" for v in hurt)
        lines.append(f"Actively avoid productising: {names} — the "
                     "condition selects worse outcomes than the raw "
                     "cross, so if anything it belongs as a negative "
                     "filter, not a signal.")
    lines.append("These are proposals from this study only — not applied "
                 "to scanner.py.")
    return lines


if __name__ == "__main__":
    sys.exit(main())
