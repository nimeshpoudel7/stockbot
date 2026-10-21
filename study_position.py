"""Position trading: hold 1-3 months, rebalance monthly (re-runnable).

Pre-declared strategies, each picking TOP stocks every 21 sessions:

  mom12     12-month momentum skipping the last month (classic 12-1)
  mom6      6-month momentum skipping the last month
  mom12_trend  mom12, only stocks above their 50- and 200-day averages,
               and cash when SPY is below its 200-day average
  leader_dip   the 30 strongest by 12-1 momentum that are above the
               200-day; buy the 10 with the lowest RSI(14) (leaders on a
               pullback)
  near_high    within 5% of the 52-week high, ranked by 12-1 momentum

Hold 21 / 42 / 63 sessions (longer holds run as overlapping monthly
tranches). Entry next open, 0.1% round trip. Results: hit rate (trade
up), share of trades that beat SPY over the same window, yearly return
and max drawdown vs SPY. Rules are chosen on the first half of the
rebalance dates and judged on the second.

Caveat: today's listed stocks only (survivorship bias flatters
momentum somewhat).

    .venv/bin/python study_position.py bars1.json bars2.json
"""
from __future__ import annotations

import itertools
import json
import sys

import numpy as np

TOP = 10
STEP = 21
COST = 0.001
STRATS = ("mom12", "mom6", "mom12_trend", "leader_dip", "near_high")
HOLDS = (21, 42, 63)


def load(paths):
    data = {}
    for p in paths:
        data.update(json.load(open(p)))
    spy = data.pop("SPY")
    dates = [b["ts"] for b in spy]
    di = {t: i for i, b in enumerate(spy) for t in [b["ts"]]}
    n, syms = len(dates), sorted(data)
    O = np.full((len(syms), n), np.nan)
    C = np.full((len(syms), n), np.nan)
    V = np.full((len(syms), n), np.nan)
    for k, s in enumerate(syms):
        for b in data[s]:
            i = di.get(b["ts"])
            if i is not None:
                O[k, i], C[k, i], V[k, i] = b["o"], b["c"], b["v"]
    spyc = np.array([b["c"] for b in spy])
    spyo = np.array([b["o"] for b in spy])
    return syms, dates, O, C, V, spyo, spyc


def rsi14(c):
    d = np.diff(c)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:14].mean(), dn[:14].mean()
    for u, x in zip(up[14:], dn[14:]):
        au, ad = (au * 13 + u) / 14, (ad * 13 + x) / 14
    return 100 - 100 / (1 + au / ad) if ad else 100.0


def picks(strat, t, C, V, spyc):
    c = C[:, :t + 1]
    last = c[:, -1]
    ok = (~np.isnan(c[:, -253])) & (~np.isnan(last)) & (last >= 3)
    dvol = np.nanmedian(V[:, t - 19:t + 1], axis=1) * last
    ok &= dvol >= 5e6
    m12 = c[:, -22] / c[:, -253] - 1
    m6 = c[:, -22] / c[:, -127] - 1
    s50 = np.nanmean(c[:, -50:], axis=1)
    s200 = np.nanmean(c[:, -200:], axis=1)
    hi52 = np.nanmax(c[:, -252:], axis=1)
    if strat == "mom12":
        score = np.where(ok, m12, -np.inf)
    elif strat == "mom6":
        score = np.where(ok, m6, -np.inf)
    elif strat == "mom12_trend":
        if spyc[t] < spyc[t - 199:t + 1].mean():
            return []
        score = np.where(ok & (last > s50) & (last > s200), m12, -np.inf)
    elif strat == "near_high":
        score = np.where(ok & (last >= 0.95 * hi52), m12, -np.inf)
    elif strat == "leader_dip":
        lead = np.where(ok & (last > s200), m12, -np.inf)
        top30 = [k for k in np.argsort(-lead)[:30] if np.isfinite(lead[k])]
        r = sorted(top30, key=lambda k: rsi14(c[k, -60:]))
        return r[:TOP]
    order = np.argsort(-score)
    return [k for k in order[:TOP] if np.isfinite(score[k])]


def run(strat, hold, rebal, O, C, spyo, spyc, n):
    tranches = hold // STEP
    trades, beat, monthly = [], [], []
    for t in rebal:
        if t + 1 + hold >= n:
            break
        ks = PICKS[(strat, t)]
        spy_r = spyc[t + hold] / spyo[t + 1] - 1
        rs = []
        for k in ks:
            e, x = O[k, t + 1], C[k, t + hold]
            if np.isnan(e) or np.isnan(x) or e <= 0:
                continue
            r = x / e - 1 - COST
            rs.append(r)
            trades.append(r)
            beat.append(r > spy_r)
        # each monthly tranche gets 1/tranches of capital, equal-weight inside
        monthly.append((np.mean(rs) if rs else 0.0) / tranches)
    if not trades:
        return None
    tr = np.array(trades)
    # monthly P&L contributions summed over time -> simple yearly rate
    years = len(monthly) * STEP / 252
    curve = np.cumsum(monthly)
    dd = float((np.maximum.accumulate(curve) - curve).max())
    return {"trades": len(tr), "hit": round(float((tr > 0).mean() * 100), 1),
            "beat_spy": round(float(np.mean(beat) * 100), 1),
            "avg_trade": round(float(np.clip(tr, -.5, 1).mean() * 100), 2),
            "year": round(float(curve[-1] / years * 100), 1),
            "maxdd": round(dd * 100, 1)}


PICKS: dict = {}


def spy_year(rebal, hold, spyo, spyc, n):
    rs = [spyc[t + hold] / spyo[t + 1] - 1 for t in rebal if t + 1 + hold < n]
    return round(float(np.sum(rs) / (hold // STEP) / (len(rs) * STEP / 252) * 100), 1)


def main():
    syms, dates, O, C, V, spyo, spyc = load(sys.argv[1:])
    n = len(dates)
    rebal = list(range(253, n - 22, STEP))
    for s in STRATS:
        for t in rebal:
            PICKS[(s, t)] = picks(s, t, C, V, spyc)
    half = len(rebal) // 2
    tr_r, te_r = rebal[:half], rebal[half:]
    import datetime as dt
    d = lambda i: dt.date.fromtimestamp(dates[i]).isoformat()  # noqa: E731
    print(f"train {d(tr_r[0])}..{d(tr_r[-1])}  test {d(te_r[0])}..{d(te_r[-1])}")
    res = []
    for s, h in itertools.product(STRATS, HOLDS):
        a = run(s, h, tr_r, O, C, spyo, spyc, n)
        b = run(s, h, te_r, O, C, spyo, spyc, n)
        res.append((s, h, a, b))
        print(f"{s:12s} hold {h:2d} | TRAIN {a} | SPY {spy_year(tr_r, h, spyo, spyc, n)}")
    best = max(res, key=lambda x: (x[2]["year"] if x[2] else -99))
    print("\nchosen on train:", best[0], best[1])
    print("TEST:", best[3], "| SPY", spy_year(te_r, best[1], spyo, spyc, n))
    print("\nall TEST (for the record, not used to choose):")
    for s, h, a, b in res:
        print(f"{s:12s} hold {h:2d} | TEST {b} | SPY {spy_year(te_r, h, spyo, spyc, n)}")


if __name__ == "__main__":
    main()
