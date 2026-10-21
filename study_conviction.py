"""High-conviction search: can we beat the Moderate-risk list on BOTH
return and precision? (re-runnable)

Same honest setup as study_position.py (point-in-time S&P 500 membership,
next-open entry, 0.1% cost, monthly rebalancing, overlapping tranches,
cash while SPY < 200-day, uptrend-only stocks), with pre-declared
variants:

  base       12-1 momentum (= the Moderate-risk rule)                  benchmark
  near_high  12-1 momentum among stocks within 10% of their 52-week high
  ensemble   average rank of 12-1, 6-1, closeness to the 52-week high and
             low volatility (a blend of four well-known effects)
  lowvol     the 30 strongest by 12-1, keep the least volatile
  resid      12-1 momentum minus what the market explains (beta x SPY)
  accel      6-1 momentum among stocks whose last 6 months beat the
             first 6 (accelerating leaders)

x top 5 / 10 stocks x hold 42 / 63 sessions x no stop / 15% trailing stop.

Choice on the FIRST half only: the highest trade hit rate among variants
that also beat the benchmark's train return. Judged once on the second
half, and the second half is split in two to check it holds in both.

    python study_conviction.py bars.json added.json
"""
from __future__ import annotations

import datetime as dt
import itertools
import sys

import numpy as np

import study_position as SPM

STEP = 21
COST = 0.001
STRATS = ("base", "near_high", "ensemble", "lowvol", "resid", "accel")
TOPS = (5, 10)
HOLDS = (42, 63)
STOPS = (None, 0.15)


def _rank(x, ok):
    r = np.full(x.shape, np.nan)
    idx = np.where(ok & np.isfinite(x))[0]
    if len(idx):
        r[idx] = np.argsort(np.argsort(x[idx])) / max(1, len(idx) - 1)
    return r


def picks(strat, t, top, dates, C, V, since, spyc):
    if spyc[t] < spyc[t - 199:t + 1].mean():
        return []
    c = C[:, t - 252:t + 1]
    last = c[:, -1]
    with np.errstate(all="ignore"):
        ok = (~np.isnan(c[:, 0])) & (~np.isnan(last)) & (last >= 3) & (since <= dates[t])
        ok &= np.nanmedian(V[:, t - 19:t + 1], axis=1) * last >= 5e6
        s50 = np.nanmean(c[:, -50:], axis=1)
        s200 = np.nanmean(c[:, -200:], axis=1)
        ok &= (last > s50) & (last > s200)
        m12 = c[:, -22] / c[:, 0] - 1
        m6 = c[:, -22] / c[:, -127] - 1
        first6 = c[:, -127] / c[:, 0] - 1
        hi = np.nanmax(c, axis=1)
        near = last / hi
        rets = np.diff(c, axis=1) / c[:, :-1]
        vol = np.nanstd(rets, axis=1)
        sp = spyc[t - 252:t + 1]
        spr = np.diff(sp) / sp[:-1]
        spm12 = sp[-22] / sp[0] - 1
        var = np.var(spr)
        beta = np.array([np.nanmean((r - np.nanmean(r)) * (spr - spr.mean())) / var
                         if np.isfinite(r).sum() > 200 else np.nan for r in rets])
    if strat == "base":
        score = m12
    elif strat == "near_high":
        score = np.where(near >= 0.90, m12, np.nan)
    elif strat == "ensemble":
        score = (_rank(m12, ok) + _rank(m6, ok) + _rank(near, ok) + _rank(-vol, ok)) / 4
    elif strat == "lowvol":
        s = np.where(ok, m12, -np.inf)
        top30 = [k for k in np.argsort(-s)[:30] if np.isfinite(s[k])]
        return sorted(top30, key=lambda k: vol[k])[:top]
    elif strat == "resid":
        score = m12 - beta * spm12
    elif strat == "accel":
        score = np.where(m6 > first6, m6, np.nan)
    score = np.where(ok & np.isfinite(score), score, -np.inf)
    return [k for k in np.argsort(-score)[:top] if np.isfinite(score[k])]


def trade(O, C, k, t, hold, stop):
    e = O[k, t + 1]
    if np.isnan(e) or e <= 0:
        return None
    path = C[k, t + 1:t + hold + 1]
    if np.isnan(path).any():
        return None
    if stop:
        peak = e
        for px in path:
            peak = max(peak, px)
            if px <= peak * (1 - stop):
                return px / e - 1 - COST
    return path[-1] / e - 1 - COST


def run(strat, top, hold, stop, rebal, D, cache):
    dates, O, C, V, since, spyo, spyc = D
    n = len(dates)
    tr, months = [], []
    for t in rebal:
        if t + 1 + hold >= n:
            break
        key = (strat, top, t)
        if key not in cache:
            cache[key] = picks(strat, t, top, dates, C, V, since, spyc)
        rs = [r for r in (trade(O, C, k, t, hold, stop) for k in cache[key]) if r is not None]
        tr += rs
        months.append(np.mean(rs) if rs else 0.0)
    if not tr:
        return None
    a, m = np.array(tr), np.array(months)
    years = len(m) * STEP / 252
    curve = np.concatenate([[0], np.cumsum(m / (hold // STEP))])
    return {"trades": int(len(a)), "hit": round(float((a > 0).mean() * 100), 1),
            "month_hit": round(float((m[m != 0] > 0).mean() * 100), 1) if (m != 0).any() else None,
            "avg": round(float(a.mean() * 100), 2), "median": round(float(np.median(a) * 100), 2),
            "year": round(float(curve[-1] / years * 100), 1),
            "maxdd": round(float((np.maximum.accumulate(curve) - curve).max() * 100), 1),
            "worst": round(float(a.min() * 100), 1)}


def main():
    *bars, added = sys.argv[1:]
    syms, dates, O, C, V, since, spyo, spyc = SPM.load(bars, added)
    D = (dates, O, C, V, since, spyo, spyc)
    rebal = list(range(253, len(dates) - 22, STEP))
    half = len(rebal) // 2
    train, test = rebal[:half], rebal[half:]
    q = len(test) // 2
    day = lambda i: dt.date.fromtimestamp(dates[i]).isoformat()  # noqa: E731
    print(f"train {day(train[0])}..{day(train[-1])}  test {day(test[0])}..{day(test[-1])}")
    cache: dict = {}
    res = []
    for s, k, h, st in itertools.product(STRATS, TOPS, HOLDS, STOPS):
        a = run(s, k, h, st, train, D, cache)
        res.append(((s, k, h, st), a))
        print(f"{s:9s} top {k:2d} hold {h} stop {st} | TRAIN {a}")
    bench = {(k, h, st): a for (s, k, h, st), a in res if s == "base"}
    elig = [(cfg, a) for cfg, a in res if a and cfg[0] != "base"
            and a["year"] > bench[cfg[1:]]["year"]]
    best_cfg, best = max(elig, key=lambda x: (x[1]["hit"], x[1]["year"]))
    print("\nCHOSEN on train:", best_cfg, best)
    for name, per in (("TEST", test), ("TEST 1st half", test[:q]), ("TEST 2nd half", test[q:])):
        print(f"{name:14s} chosen   ", run(*best_cfg, per, D, cache))
        print(f"{name:14s} moderate ", run("base", 10, 63, None, per, D, cache))
        print(f"{name:14s} SPY      ", SPM.spy_run(63, per, D))
    print("\nall TEST (record only):")
    for cfg, _ in res:
        print(cfg, run(*cfg, test, D, cache))


if __name__ == "__main__":
    main()
