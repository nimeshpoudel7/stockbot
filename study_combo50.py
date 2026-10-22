"""'All three, under $50' — test (re-runnable).

Combines the three strategy rules (Moderate risk: 12-1 momentum top 10 +
SPY cash rule; High conviction: same, near 52-week high, top 5; Leaders 3
months: 3-month momentum top 10, no cash rule) and limits them to stocks
whose ACTUAL share price was under $50 on the pick date. Actual past
prices are rebuilt from split-adjusted closes and the split history (the
backtest's dividend-adjusted series would understate old prices).

Variants (all: point-in-time S&P 500, monthly batches, hold 63 sessions,
equal money per pick, 0.1% cost):
  union_all     the three lists combined (no price limit)
  union_u50     the three lists combined, keep only stocks under $50
  ranked_u50    each rule run ONLY on stocks under $50, lists combined
  leaders_u50   Leaders 3 months run only on stocks under $50

    python study_combo50.py bars.json membership.csv raw.json splits.json
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import numpy as np

import study_long as SL

HOLD, STEP, COST = 63, 21, 0.001


def nominal(D, raw_path, split_path):
    syms, dates = D[0], D[1]
    raw, splits = json.load(open(raw_path)), json.load(open(split_path))
    di = {dt.datetime.utcfromtimestamp(t).date(): i for i, t in enumerate(dates)}
    P = np.full((len(syms), len(dates)), np.nan)
    for k, s in enumerate(syms):
        sp = sorted(splits.get(s) or [])
        for ts, c in raw.get(s, []):
            i = di.get(dt.datetime.utcfromtimestamp(ts).date())
            if i is None:
                continue
            f = 1.0
            for sts, ratio in sp:
                if sts > ts:
                    f *= ratio      # later splits divided the price: undo them
            P[k, i] = c * f
    return P


def rule_picks(rule, t, D, mask):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    if rule != "leaders" and spyc[t] < spyc[t - 199:t + 1].mean():
        return []
    c = C[:, t - 252:t + 1]
    last = c[:, -1]
    with np.errstate(all="ignore"):
        ok = MEM[:, t] & ~np.isnan(c[:, 0]) & ~np.isnan(last) & mask
        ok &= np.nanmedian(V[:, t - 19:t + 1], axis=1) * last >= 5e6
        ok &= (last > np.nanmean(c[:, -50:], axis=1)) & (last > np.nanmean(c[:, -200:], axis=1))
        near = last / np.nanmax(c, axis=1)
        sc = c[:, -22] / c[:, 0] - 1 if rule != "leaders" else last / c[:, -64] - 1
    if rule == "conviction":
        ok &= near >= 0.90
    top = 5 if rule == "conviction" else 10
    s = np.where(ok & np.isfinite(sc), sc, -np.inf)
    return [k for k in np.argsort(-s)[:top] if np.isfinite(s[k])]


def variant_picks(var, t, D, P):
    price_ok = (P[:, t] >= 3) & (P[:, t] < 50)
    anyp = np.ones(len(D[0]), bool)
    if var == "union_all":
        ks = set().union(*(rule_picks(r, t, D, anyp) for r in ("moderate", "conviction", "leaders")))
    elif var == "union_u50":
        ks = {k for k in set().union(*(rule_picks(r, t, D, anyp) for r in ("moderate", "conviction", "leaders")))
              if price_ok[k]}
    elif var == "ranked_u50":
        ks = set().union(*(rule_picks(r, t, D, price_ok) for r in ("moderate", "conviction", "leaders")))
    elif var == "leaders_u50":
        ks = set(rule_picks("leaders", t, D, price_ok))
    elif var.startswith("only_"):
        ks = set(rule_picks(var[5:], t, D, anyp))
    return sorted(ks)


def run(var, D, P, lo=0, hi=None):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    n = len(dates)
    hi = hi or n
    tr, batches, spy, ts, sizes = [], [], [], [], []
    for t in range(max(253, lo), min(hi, n - HOLD - 2), STEP):
        rs = []
        for k in variant_picks(var, t, D, P):
            e = O[k, t + 1]
            p = C[k, t + 1:t + HOLD + 1]
            v = p[~np.isnan(p)]
            if np.isnan(e) or e <= 0 or not len(v):
                continue
            rs.append(v[-1] / e - 1 - COST)
        tr += rs
        sizes.append(len(rs))
        batches.append(float(np.mean(rs)) if rs else 0.0)
        spy.append(spyc[t + HOLD] / spyo[t + 1] - 1)
        ts.append(t)
    a, b, s = np.array(tr or [0.0]), np.array(batches), np.array(spy)
    years = len(b) * STEP / 252
    curve = np.concatenate([[0], np.cumsum(b / 3)])
    yr = np.array([dt.datetime.utcfromtimestamp(dates[t]).year for t in ts])
    beat = sum(b[yr == y].sum() > s[yr == y].sum() for y in sorted(set(yr)))
    return {"trades": len(tr), "avg_picks": round(float(np.mean(sizes)), 1),
            "hit": round(float((a > 0).mean() * 100), 1), "batch_hit": round(float((b > 0).mean() * 100), 1),
            "avg": round(float(a.mean() * 100), 2), "year": round(float(curve[-1] / years * 100), 1),
            "spy": round(float(s.sum() / 3 / years * 100), 1),
            "maxdd": round(float((np.maximum.accumulate(curve) - curve).max() * 100), 1),
            "worst": round(float(a.min() * 100), 1),
            "years_beat": f"{beat} of {len(set(yr))}"}


def main():
    D = SL.load(sys.argv[1], sys.argv[2])
    P = nominal(D, sys.argv[3], sys.argv[4])
    cut = next(i for i, t in enumerate(D[1]) if dt.datetime.utcfromtimestamp(t).year >= 2022)
    for var in ("only_moderate", "only_conviction", "only_leaders", "union_all",
                "union_u50", "ranked_u50", "leaders_u50"):
        full, a, b = run(var, D, P), run(var, D, P, hi=cut), run(var, D, P, lo=cut)
        print(f"\n{var}\n  10y   {full}\n  16-21 hit {a['hit']}% year {a['year']:+.1f}% (SPY {a['spy']:+.1f}%)"
              f"\n  22-26 hit {b['hit']}% year {b['year']:+.1f}% (SPY {b['spy']:+.1f}%)")


if __name__ == "__main__":
    main()
