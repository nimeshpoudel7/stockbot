"""Long-history check of the Moderate-risk and High-conviction rules.

The two rules were chosen on 2024-03..2026-08. This replays them, exactly
as shipped, over ~10 years with POINT-IN-TIME S&P 500 membership including
stocks that were later REMOVED (from a start/end membership table), so
everything before 2024 is data the rules never saw — including the 2018 Q4
sell-off, the 2020 crash and the 2022 bear market.

Inputs:
  bars.json        {ticker: [{ts,o,h,l,c,v}, ...]} incl. SPY (dividend-
                   adjusted closes preferred)
  membership.csv   ticker,start_date,end_date (end empty = still a member)

    python study_long.py bars.json membership.csv
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from collections import defaultdict

import numpy as np

STEP = 21
COST = 0.001
HOLD = 63


def load(bars_path, mem_path):
    data = json.load(open(bars_path))
    spy = data.pop("SPY")
    dates = np.array([b["ts"] for b in spy])
    di = {t: i for i, t in enumerate(dates)}
    # map timestamps to the nearest SPY session by date (sources differ in hour)
    day_idx = {dt.datetime.utcfromtimestamp(t).date(): i for i, t in enumerate(dates)}
    spans = defaultdict(list)
    for r in csv.DictReader(open(mem_path)):
        spans[r["ticker"]].append((r["start_date"], r["end_date"] or "9999-12-31"))
    syms = sorted(s for s in data if s in spans)
    n = len(dates)
    O = np.full((len(syms), n), np.nan)
    C = np.full((len(syms), n), np.nan)
    V = np.full((len(syms), n), np.nan)
    MEM = np.zeros((len(syms), n), bool)
    dstr = [dt.datetime.utcfromtimestamp(t).date().isoformat() for t in dates]
    for k, s in enumerate(syms):
        for b in data[s]:
            i = di.get(b["ts"])
            if i is None:
                i = day_idx.get(dt.datetime.utcfromtimestamp(b["ts"]).date())
            if i is not None:
                O[k, i], C[k, i], V[k, i] = b["o"], b["c"], b["v"]
        for a, e in spans[s]:
            for i, d in enumerate(dstr):
                if a <= d < e:
                    MEM[k, i] = True
    return syms, dates, O, C, V, MEM, np.array([b["o"] for b in spy]), np.array([b["c"] for b in spy])


def picks(rule, t, D):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    if spyc[t] < spyc[t - 199:t + 1].mean():
        return []                                            # cash
    c = C[:, t - 252:t + 1]
    last = c[:, -1]
    with np.errstate(all="ignore"):
        ok = MEM[:, t] & (~np.isnan(c[:, 0])) & (~np.isnan(last)) & (last >= 3)
        ok &= np.nanmedian(V[:, t - 19:t + 1], axis=1) * last >= 5e6
        ok &= (last > np.nanmean(c[:, -50:], axis=1)) & (last > np.nanmean(c[:, -200:], axis=1))
        m12 = c[:, -22] / c[:, 0] - 1
        near = last / np.nanmax(c, axis=1)
    if rule == "conviction":
        ok &= near >= 0.90
        top = 5
    else:
        top = 10
    score = np.where(ok & np.isfinite(m12), m12, -np.inf)
    return [k for k in np.argsort(-score)[:top] if np.isfinite(score[k])]


def replay(rule, D):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    n = len(dates)
    trades, tranche = [], []          # tranche: (t_entry, mean return or 0 for cash)
    for t in range(253, n - HOLD - 2, STEP):
        rs = []
        for k in picks(rule, t, D):
            e = O[k, t + 1]
            path = C[k, t + 1:t + HOLD + 1]
            if np.isnan(e) or e <= 0:
                continue
            # delisted / acquired mid-hold: exit at the last available close
            valid = path[~np.isnan(path)]
            if not len(valid):
                continue
            r = valid[-1] / e - 1 - COST
            rs.append(r)
            trades.append((t, syms[k], r))
        tranche.append((t, float(np.mean(rs)) if rs else 0.0, len(rs)))
    return trades, tranche


def summarize(trades, tranche, D, lo=None, hi=None):
    dates, spyo, spyc = D[1], D[6], D[7]
    sel_t = [(t, r) for t, _, r in trades if (lo is None or t >= lo) and (hi is None or t < hi)]
    sel_m = [(t, m, k) for t, m, k in tranche if (lo is None or t >= lo) and (hi is None or t < hi)]
    if not sel_m:
        return None
    a = np.array([r for _, r in sel_t]) if sel_t else np.array([0.0])
    m = np.array([x for _, x, _ in sel_m])
    years = len(m) * STEP / 252
    curve = np.concatenate([[0], np.cumsum(m / (HOLD // STEP))])
    spy = np.array([spyc[t + HOLD] / spyo[t + 1] - 1 for t, _, _ in sel_m])
    return {"trades": int(len(sel_t)), "hit": round(float((a > 0).mean() * 100), 1),
            "month_hit": round(float((m[m != 0] > 0).mean() * 100), 1) if (m != 0).any() else None,
            "avg": round(float(a.mean() * 100), 2), "year": round(float(curve[-1] / years * 100), 1),
            "spy_year": round(float(spy.sum() / (HOLD // STEP) / years * 100), 1),
            "maxdd": round(float((np.maximum.accumulate(curve) - curve).max() * 100), 1),
            "cash_months": int((np.array([k for _, _, k in sel_m]) == 0).sum()),
            "worst": round(float(a.min() * 100), 1)}


def main():
    D = load(sys.argv[1], sys.argv[2])
    if "--grid" in sys.argv:
        grid(D)
        return
    syms, dates = D[0], D[1]
    yr = lambda t: dt.datetime.utcfromtimestamp(dates[t]).year  # noqa: E731
    print(f"{len(syms)} tickers with prices; sessions {dt.date.fromtimestamp(dates[0])}..{dt.date.fromtimestamp(dates[-1])}")
    cut = next(i for i, t in enumerate(dates) if dt.datetime.utcfromtimestamp(t).date() >= dt.date(2024, 3, 1))
    out = {}
    for rule in ("moderate", "conviction"):
        trades, tranche = replay(rule, D)
        res = {"all": summarize(trades, tranche, D),
               "unseen_before_2024_03": summarize(trades, tranche, D, hi=cut),
               "tuned_2024_03_on": summarize(trades, tranche, D, lo=cut)}
        by_year = {}
        for y in sorted({yr(t) for t, _, _ in tranche}):
            idx = [t for t, _, _ in tranche if yr(t) == y]
            by_year[y] = summarize(trades, tranche, D, lo=idx[0], hi=idx[-1] + 1)
        res["by_year"] = by_year
        out[rule] = res
        print(f"\n=== {rule} ===")
        for k in ("all", "unseen_before_2024_03", "tuned_2024_03_on"):
            print(f"{k:24s}", res[k])
        for y, r in by_year.items():
            print(f"  {y}: trades {r['trades']:3d} hit {r['hit']:5.1f}%  year {r['year']:+6.1f}%  SPY {r['spy_year']:+6.1f}%  cash months {r['cash_months']}  worst {r['worst']:+.0f}%")
    json.dump(out, open("data/study_cache/study_long_results.json" if __import__("os").path.isdir("data/study_cache") else "/dev/null", "w"), indent=1, default=str)


# ---------------------------------------------------------------- grid search
def grid_picks(var, t, D, top):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    cash_rule, sig, filt = var
    if cash_rule and spyc[t] < spyc[t - 199:t + 1].mean():
        return []
    c = C[:, t - 252:t + 1]
    last = c[:, -1]
    with np.errstate(all="ignore"):
        ok = MEM[:, t] & (~np.isnan(c[:, 0])) & (~np.isnan(last)) & (last >= 3)
        ok &= np.nanmedian(V[:, t - 19:t + 1], axis=1) * last >= 5e6
        up = (last > np.nanmean(c[:, -50:], axis=1)) & (last > np.nanmean(c[:, -200:], axis=1))
        near = last / np.nanmax(c, axis=1)
        rets = np.diff(c, axis=1) / c[:, :-1]
        vol = np.nanstd(rets, axis=1)
        sig_v = {"m12": c[:, -22] / c[:, 0] - 1, "m6": c[:, -22] / c[:, -127] - 1,
                 "m3": c[:, -1] / c[:, -64] - 1, "sharpe": (c[:, -22] / c[:, 0] - 1) / vol,
                 "rev1": -(c[:, -1] / c[:, -22] - 1), "lowvol": -vol}[sig]
    if filt in ("trend", "trend_near"):
        ok &= up
    if filt == "trend_near":
        ok &= near >= 0.90
    score = np.where(ok & np.isfinite(sig_v), sig_v, -np.inf)
    return [k for k in np.argsort(-score)[:top] if np.isfinite(score[k])]


def grid_run(var, top, hold, D, lo, hi):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    n = len(dates)
    tr, ms, spy = [], [], []
    for t in range(max(253, lo), min(hi, n - hold - 2), STEP):
        rs = []
        for k in grid_picks(var, t, D, top):
            e = O[k, t + 1]
            path = C[k, t + 1:t + hold + 1]
            v = path[~np.isnan(path)]
            if np.isnan(e) or e <= 0 or not len(v):
                continue
            rs.append(v[-1] / e - 1 - COST)
        tr += rs
        ms.append(np.mean(rs) if rs else 0.0)
        spy.append(spyc[t + hold] / spyo[t + 1] - 1)
    a, m = np.array(tr or [0.0]), np.array(ms)
    years = len(m) * STEP / 252
    curve = np.concatenate([[0], np.cumsum(m / (hold // STEP))])
    return {"trades": len(tr), "hit": round(float((a > 0).mean() * 100), 1),
            "year": round(float(curve[-1] / years * 100), 1),
            "spy": round(float(np.sum(spy) / (hold // STEP) / years * 100), 1),
            "maxdd": round(float((np.maximum.accumulate(curve) - curve).max() * 100), 1)}


def grid(D, split_year=2022):
    import itertools
    dates = D[1]
    cut = next(i for i, t in enumerate(dates) if dt.datetime.utcfromtimestamp(t).year >= split_year)
    rows = []
    for cash, sig, filt, top, hold in itertools.product(
            (True, False), ("m12", "m6", "m3", "sharpe", "rev1", "lowvol"),
            ("none", "trend", "trend_near"), (5, 10, 20), (21, 63)):
        a = grid_run((cash, sig, filt), top, hold, D, 0, cut)
        rows.append(((cash, sig, filt, top, hold), a))
    rows.sort(key=lambda r: r[1]["year"] - r[1]["spy"], reverse=True)
    print(f"\nTRAIN (before {split_year}) top 10 by excess return over SPY:")
    for cfg, a in rows[:10]:
        print("  ", cfg, a)
    best = rows[0][0]
    hi_hit = max(rows, key=lambda r: (r[1]["hit"], r[1]["year"]))[0]
    for name, cfg in (("best excess", best), ("best hit", hi_hit),
                      ("moderate rule", (True, "m12", "trend", 10, 63)),
                      ("conviction rule", (True, "m12", "trend_near", 5, 63))):
        print(f"TEST {split_year}+ {name:16s} {cfg}:", grid_run((cfg[0], cfg[1], cfg[2]), cfg[3], cfg[4], D, cut, len(dates)))


if __name__ == "__main__":
    main()
