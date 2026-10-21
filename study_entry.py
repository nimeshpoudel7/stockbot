"""Buy zone: is there a better ENTRY than buying at the next open?
(re-runnable)

For every pick of the three strategy rules (Moderate risk, High
conviction, Leaders 3 months) over ~10 years, compare entry methods. The
sell date is the same for all of them (signal day + 63 sessions), so only
the entry differs:

  open        buy at the next open (what the backtests assumed)
  limit_pX    limit order X% below the signal-day close, good for N
              sessions; filled if a day's low touches it (at the limit, or
              at the open if it gaps below); unfilled -> NO trade
  limit_aX    same with the limit X x ATR(14) below the close
  sma10/20    limit at the 10/20-day average (pullback to the average)
  limit+mkt   limit X% below; if unfilled after N sessions, buy at market

A limit gets a better price but misses the strongest stocks (the ones
that never dip). Judged on: average return per trade, how often it fills,
and the money result per pick-slot (unfilled = 0%). Chosen on 2016-2021,
checked on 2022+.

    python study_entry.py bars.json membership.csv
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import numpy as np

import study_long as SL

HOLD = 63
COST = 0.001


def load_hl(path, syms, dates):
    data = json.load(open(path))
    di = {dt.datetime.utcfromtimestamp(t).date(): i for i, t in enumerate(dates)}
    H = np.full((len(syms), len(dates)), np.nan)
    L = np.full((len(syms), len(dates)), np.nan)
    for k, s in enumerate(syms):
        for b in data[s]:
            i = di.get(dt.datetime.utcfromtimestamp(b["ts"]).date())
            if i is not None:
                H[k, i], L[k, i] = b["h"], b["l"]
    return H, L


def picks(rule, t, D):
    syms, dates, O, C, V, MEM, spyo, spyc = D
    if rule != "leaders" and spyc[t] < spyc[t - 199:t + 1].mean():
        return []
    c = C[:, t - 252:t + 1]
    last = c[:, -1]
    with np.errstate(all="ignore"):
        ok = MEM[:, t] & ~np.isnan(c[:, 0]) & ~np.isnan(last) & (last >= 3)
        ok &= np.nanmedian(V[:, t - 19:t + 1], axis=1) * last >= 5e6
        ok &= (last > np.nanmean(c[:, -50:], axis=1)) & (last > np.nanmean(c[:, -200:], axis=1))
        near = last / np.nanmax(c, axis=1)
        sc = c[:, -22] / c[:, 0] - 1 if rule != "leaders" else last / c[:, -64] - 1
    if rule == "conviction":
        ok &= near >= 0.90
    top = 5 if rule == "conviction" else 10
    s = np.where(ok & np.isfinite(sc), sc, -np.inf)
    return [k for k in np.argsort(-s)[:top] if np.isfinite(s[k])]


def entry(method, k, t, D, H, L):
    """(fill price, fill index) or None (no trade)."""
    syms, dates, O, C, V, MEM, spyo, spyc = D
    kind, x, n = method
    if kind == "open":
        return (O[k, t + 1], t + 1) if not np.isnan(O[k, t + 1]) else None
    close = C[k, t]
    if kind == "pct":
        lim = close * (1 - x)
    elif kind == "atr":
        tr = np.nanmax(np.vstack([H[k, t - 13:t + 1] - L[k, t - 13:t + 1],
                                  np.abs(H[k, t - 13:t + 1] - C[k, t - 14:t]),
                                  np.abs(L[k, t - 13:t + 1] - C[k, t - 14:t])]), axis=0)
        lim = close - x * np.nanmean(tr)
    elif kind == "sma":
        lim = min(close, np.nanmean(C[k, t - x + 1:t + 1]))
    for i in range(t + 1, min(t + 1 + n, t + HOLD)):
        o, lo = O[k, i], L[k, i]
        if np.isnan(o) or np.isnan(lo):
            continue
        if o <= lim:
            return o, i
        if lo <= lim:
            return lim, i
    if kind == "pct" and method[3:] == ("mkt",):
        return None
    return None


def entry_mkt(method, k, t, D, H, L):
    r = entry(method[:3], k, t, D, H, L)
    if r is not None:
        return r
    i = t + 1 + method[2]
    return (D[2][k, i], i) if i < t + HOLD and not np.isnan(D[2][k, i]) else None


def evaluate(rule, method, D, H, L, lo, hi):
    C = D[3]
    rets, slots = [], 0
    for t in range(max(253, lo), min(hi, len(D[1]) - HOLD - 2), 21):
        for k in picks(rule, t, D):
            slots += 1
            f = entry_mkt(method, k, t, D, H, L) if len(method) > 3 else entry(method, k, t, D, H, L)
            if f is None:
                continue
            px, _ = f
            path = C[k, t + 1:t + HOLD + 1]
            v = path[~np.isnan(path)]
            if not len(v) or px <= 0:
                continue
            rets.append(v[-1] / px - 1 - COST)
    a = np.array(rets or [0.0])
    return {"fill": round(len(rets) / max(slots, 1) * 100, 0), "trades": len(rets),
            "hit": round(float((a > 0).mean() * 100), 1), "avg": round(float(a.mean() * 100), 2),
            "per_slot": round(float(a.sum() / max(slots, 1) * 100), 2)}


METHODS = [("open", 0, 1)] + \
    [("pct", x, n) for x in (0.01, 0.02, 0.03, 0.05) for n in (5, 10)] + \
    [("atr", x, n) for x in (0.5, 1.0, 1.5) for n in (5, 10)] + \
    [("sma", w, n) for w in (10, 20) for n in (10, 21)] + \
    [("pct", x, n, "mkt") for x in (0.02, 0.03) for n in (5, 10)] + \
    [("atr", x, n, "mkt") for x in (0.5, 1.0) for n in (5, 10)]


def main():
    D = SL.load(sys.argv[1], sys.argv[2])
    H, L = load_hl(sys.argv[1], D[0], D[1])
    cut = next(i for i, t in enumerate(D[1]) if dt.datetime.utcfromtimestamp(t).year >= 2022)
    for rule in ("moderate", "conviction", "leaders"):
        print(f"\n=== {rule} ===")
        rows = []
        for m in METHODS:
            a = evaluate(rule, m, D, H, L, 0, cut)
            rows.append((m, a))
        base = rows[0][1]
        rows_sorted = sorted(rows, key=lambda r: r[1]["per_slot"], reverse=True)
        for m, a in rows_sorted[:8]:
            print(f"  TRAIN {str(m):28s} {a}")
        print(f"  TRAIN {'open (baseline)':28s} {base}")
        best = rows_sorted[0][0]
        best_avg = max((r for r in rows if r[1]['fill'] >= 60), key=lambda r: r[1]["avg"])[0]
        for name, m in (("baseline open", ("open", 0, 1)), ("best per slot", best), ("best avg, fill>=60%", best_avg)):
            print(f"  TEST 2022+ {name:20s} {str(m):28s} {evaluate(rule, m, D, H, L, cut, len(D[1]))}")


if __name__ == "__main__":
    main()
