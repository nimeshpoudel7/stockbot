"""Buy zone, part 2: support, Fibonacci, RSI(2) timing, breakout-retest
(re-runnable). Same setup as study_entry.py: same picks and the same sell
date (signal day + 63 sessions); only the entry differs. Settings chosen on
2016-2021, checked on 2022+.

  support_N     limit at the lowest low of the last N sessions
  fib382/500    limit at the 38.2% / 50% pullback of the last 3-month rally
  rsi2_X        buy at the close of the first day RSI(2) < X within 10
                sessions (a short dip inside the uptrend)
  retest        limit at the prior 20-day high (the breakout level)
  next_close    buy at the next day's close instead of the open
  *+mkt         if the zone is not reached in time, buy at market anyway

    python study_entry2.py bars.json membership.csv
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

import study_entry as E
import study_long as SL

HOLD, COST, WAIT = 63, 0.001, 10


def rsi2(c):
    d = np.diff(c)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:2].mean(), dn[:2].mean()
    out = []
    for u, x in zip(up[2:], dn[2:]):
        au, ad = (au + u) / 2, (ad + x) / 2
        out.append(100 - 100 / (1 + au / ad) if ad else 100.0)
    return out[-1] if out else 50.0


def entry(m, k, t, D, H, L):
    O, C = D[2], D[3]
    kind, x, mkt = m
    if kind == "open":
        return O[k, t + 1]
    if kind == "next_close":
        return C[k, t + 1]
    lim = None
    if kind == "support":
        lim = np.nanmin(L[k, t - x + 1:t + 1])
    elif kind in ("fib382", "fib500"):
        c = C[k, t - 62:t + 1]
        hi_i = int(np.nanargmax(c))
        lo = np.nanmin(c[:hi_i + 1]) if hi_i > 0 else np.nanmin(c)
        hi = c[hi_i]
        lim = hi - (0.382 if kind == "fib382" else 0.5) * (hi - lo)
    elif kind == "retest":
        lim = np.nanmax(H[k, t - 20:t])
        if lim >= C[k, t]:
            lim = C[k, t]
    if kind == "rsi2":
        for i in range(t + 1, t + 1 + WAIT):
            if np.isnan(C[k, i]):
                continue
            if rsi2(C[k, i - 30:i + 1]) < x:
                return C[k, i]
    else:
        for i in range(t + 1, t + 1 + WAIT):
            o, lo = O[k, i], L[k, i]
            if np.isnan(o) or np.isnan(lo):
                continue
            if o <= lim:
                return o
            if lo <= lim:
                return lim
    if mkt:
        return O[k, t + 1 + WAIT]
    return None


def evaluate(rule, m, D, H, L, lo, hi):
    C = D[3]
    rets, slots = [], 0
    for t in range(max(253, lo), min(hi, len(D[1]) - HOLD - 2), 21):
        for k in E.picks(rule, t, D):
            slots += 1
            px = entry(m, k, t, D, H, L)
            if px is None or np.isnan(px) or px <= 0:
                continue
            p = C[k, t + 1:t + HOLD + 1]
            v = p[~np.isnan(p)]
            if len(v):
                rets.append(v[-1] / px - 1 - COST)
    a = np.array(rets or [0.0])
    return {"fill": round(len(rets) / max(slots, 1) * 100), "hit": round(float((a > 0).mean() * 100), 1),
            "avg": round(float(a.mean() * 100), 2), "per_slot": round(float(a.sum() / max(slots, 1) * 100), 2)}


METHODS = [("open", 0, False), ("next_close", 0, False)] + \
    [(k, x, mkt) for k, x in (("support", 5), ("support", 10), ("fib382", 0), ("fib500", 0),
                              ("rsi2", 10), ("rsi2", 20), ("rsi2", 30), ("retest", 0)) for mkt in (False, True)]


def main():
    D = SL.load(sys.argv[1], sys.argv[2])
    H, L = E.load_hl(sys.argv[1], D[0], D[1])
    cut = next(i for i, t in enumerate(D[1]) if dt.datetime.utcfromtimestamp(t).year >= 2022)
    for rule in ("moderate", "conviction", "leaders"):
        print(f"\n=== {rule} ===   (fill %, hit %, avg per trade, money per pick)")
        rows = [(m, evaluate(rule, m, D, H, L, 0, cut)) for m in METHODS]
        for m, a in sorted(rows, key=lambda r: -r[1]["per_slot"]):
            print(f"  TRAIN {str(m):24s} {a}")
        best = max(rows, key=lambda r: r[1]["per_slot"])[0]
        for name, m in (("market (open)", ("open", 0, False)), ("best on train", best)):
            print(f"  TEST 2022+ {name:15s} {str(m):24s} {evaluate(rule, m, D, H, L, cut, len(D[1]))}")


if __name__ == "__main__":
    main()
