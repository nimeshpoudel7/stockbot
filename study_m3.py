"""Stress test for the 3-month momentum rule (re-runnable).

Rule under test: S&P 500 stocks (point-in-time membership, >= $3,
>= $5M/day) above their 50- and 200-day averages; rank by the last
3 months' return (63 sessions); buy the top 10; hold 63 sessions;
a new monthly batch every 21 sessions (3 overlapping batches).

Checks — a real edge should survive all of them:
  1. every calendar year vs SPY
  2. trading costs 0.1% / 0.3% / 0.5% round trip
  3. neighbouring settings (lookback 42/63/84, top 5/10/15/20,
     hold 42/63/84) — a lucky setting is a lone spike, a real effect is
     a plateau
  4. rebalance-day luck: the monthly batch shifted by 0..20 sessions
  5. two random halves of the stock universe
  6. random baseline: 10 random stocks from the SAME uptrend pool
     (does the ranking add anything beyond the trend filter?)
  7. with and without the 'cash when SPY < 200-day' rule

    python study_m3.py bars.json membership.csv
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

import study_long as SL

STEP = 21


def pool(t, D, lookback, extra_mask=None):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    c = C[:, t - 252:t + 1]
    last = c[:, -1]
    with np.errstate(all="ignore"):
        ok = MEM[:, t] & (~np.isnan(c[:, 0])) & (~np.isnan(last)) & (last >= 3)
        ok &= np.nanmedian(V[:, t - 19:t + 1], axis=1) * last >= 5e6
        ok &= (last > np.nanmean(c[:, -50:], axis=1)) & (last > np.nanmean(c[:, -200:], axis=1))
        mom = last / c[:, -1 - lookback] - 1
    if extra_mask is not None:
        ok &= extra_mask
    return ok & np.isfinite(mom), mom


def run(D, lookback=63, top=10, hold=63, cost=0.001, offset=0, cash=False,
        mask=None, rng=None, lo=None, hi=None):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    n = len(dates)
    start = 253 + offset
    trades, batches, spy, ts = [], [], [], []
    for t in range(start, n - hold - 2, STEP):
        if lo is not None and t < lo or hi is not None and t >= hi:
            continue
        spy.append(spyc[t + hold] / spyo[t + 1] - 1)
        ts.append(t)
        if cash and spyc[t] < spyc[t - 199:t + 1].mean():
            batches.append(0.0)
            continue
        ok, mom = pool(t, D, lookback, mask)
        idx = np.where(ok)[0]
        if rng is not None:
            pick = rng.choice(idx, size=min(top, len(idx)), replace=False) if len(idx) else []
        else:
            pick = idx[np.argsort(-mom[idx])][:top]
        rs = []
        for k in pick:
            e = O[k, t + 1]
            path = C[k, t + 1:t + hold + 1]
            v = path[~np.isnan(path)]
            if np.isnan(e) or e <= 0 or not len(v):
                continue
            rs.append(v[-1] / e - 1 - cost)
        trades += rs
        batches.append(float(np.mean(rs)) if rs else 0.0)
    a, b, s = np.array(trades or [0.0]), np.array(batches), np.array(spy)
    tranches = hold / STEP
    years = len(b) * STEP / 252
    # portfolio: 1/tranches of capital per batch -> monthly P&L contributions
    curve = np.concatenate([[0], np.cumsum(b / tranches)])
    spy_curve = np.concatenate([[0], np.cumsum(s / tranches)])
    return {"trades": len(trades), "hit": round(float((a > 0).mean() * 100), 1),
            "batch_hit": round(float((b > 0).mean() * 100), 1),
            "avg": round(float(a.mean() * 100), 2),
            "year": round(float(curve[-1] / years * 100), 1),
            "spy": round(float(spy_curve[-1] / years * 100), 1),
            "maxdd": round(float((np.maximum.accumulate(curve) - curve).max() * 100), 1),
            "spy_maxdd": round(float((np.maximum.accumulate(spy_curve) - spy_curve).max() * 100), 1),
            "worst": round(float(a.min() * 100), 1), "ts": ts, "batches": b, "spy_b": s}


def strip(r):
    return {k: v for k, v in r.items() if k not in ("ts", "batches", "spy_b")}


def main():
    D = SL.load(sys.argv[1], sys.argv[2])
    syms, dates = D[0], D[1]
    yr = lambda t: dt.datetime.utcfromtimestamp(dates[t]).year  # noqa: E731
    base = run(D)
    print("RULE (m3, trend, top 10, hold 63, 0.1% cost):", strip(base))

    print("\n1. By year (batches started that year):")
    beat = 0
    years = sorted({yr(t) for t in base["ts"]})
    for y in years:
        ix = [i for i, t in enumerate(base["ts"]) if yr(t) == y]
        b, s = base["batches"][ix], base["spy_b"][ix]
        r, sp = b.sum() / 3 / (len(ix) * STEP / 252) * 100, s.sum() / 3 / (len(ix) * STEP / 252) * 100
        beat += r > sp
        print(f"   {y}: rule {r:+6.1f}%/yr  SPY {sp:+6.1f}%/yr  batches up {(b > 0).mean() * 100:4.0f}%  {'BEAT' if r > sp else 'lag'}")
    print(f"   beat SPY in {beat} of {len(years)} years")

    print("\n2. Costs:")
    for c in (0.001, 0.003, 0.005):
        print(f"   {c * 100:.1f}% round trip:", strip(run(D, cost=c)))

    print("\n3. Neighbouring settings (year % / hit %):")
    for lb in (42, 63, 84):
        line = []
        for top in (5, 10, 15, 20):
            for hold in (42, 63, 84):
                r = run(D, lookback=lb, top=top, hold=hold)
                line.append(f"t{top}h{hold}:{r['year']:+.0f}/{r['hit']:.0f}")
        print(f"   lookback {lb}: " + "  ".join(line))
    print("   (SPY:", run(D)["spy"], "%/yr)")

    print("\n4. Rebalance-day shift:")
    ys = [run(D, offset=o)["year"] for o in range(0, 21, 2)]
    print("   year % by offset 0..20:", ys, "| min", min(ys), "max", max(ys))

    print("\n5. Random halves of the universe:")
    rng = np.random.default_rng(7)
    for i in range(3):
        m = rng.random(len(syms)) < 0.5
        a, b = run(D, mask=m), run(D, mask=~m)
        print(f"   split {i}: half A {a['year']:+.1f}%/yr hit {a['hit']}% | half B {b['year']:+.1f}%/yr hit {b['hit']}%")

    print("\n6. Random 10 from the same uptrend pool (200 runs):")
    rnd = [run(D, rng=np.random.default_rng(s))["year"] for s in range(200)]
    print(f"   random: median {np.median(rnd):+.1f}%/yr, 95th pct {np.percentile(rnd, 95):+.1f}%, "
          f"rule beats {np.mean(np.array(rnd) < base['year']) * 100:.0f}% of random runs")

    print("\n7. With the cash rule:", strip(run(D, cash=True)))

    cut = next(i for i, t in enumerate(dates) if dt.datetime.utcfromtimestamp(t).year >= 2022)
    print("\nHalves: 2016-21", strip(run(D, hi=cut)), "\n        2022+  ", strip(run(D, lo=cut)))


if __name__ == "__main__":
    main()
