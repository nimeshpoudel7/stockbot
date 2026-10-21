"""Trader upgrades for the Top-10 lists, tested honestly (re-runnable).

Starting from the combined score (top_picks / study_combined rows), this
replays the lists as a trader would actually trade them and compares
pre-declared upgrades:

  exits    hold 5 days | hold 3 days | first up close (max 5) |
           close back above the 5-day average (max 5) — each with or
           without a 2x ATR protective stop
  filters  none | market washout (many stocks oversold at once) |
           correlation cap (skip a pick moving in lockstep with one
           already taken) | SELL only when SPY is not in a bull trend

A portfolio of at most K positions (equal size, next-open entries, 0.1%
round-trip cost) is simulated, so the result is a real yearly return to
compare with buying SPY and holding. The rule set is chosen on the FIRST
half of the history and judged once on the second half.

    .venv/bin/python study_trader.py rows.jsonl bars1.json bars2.json
"""
from __future__ import annotations

import itertools
import json
import sys
from collections import defaultdict

import numpy as np

from top_picks import COMPONENTS, comps, eligible, tech_score

K = 10                 # max open positions
COST = 0.001           # round trip
TOP = 10
EXITS = ("hold5", "hold3", "upclose", "sma5")
STOPS = (None, 2.0)
FILTERS = ("none", "washout10", "washout15", "corr", "corr+washout10")


def _corr(a, b):
    a, b = np.asarray(a), np.asarray(b)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


class Data:
    def __init__(self, rows_path, bar_paths, model):
        self.rows = [json.loads(line) for line in open(rows_path)]
        bars = {}
        for p in bar_paths:
            bars.update(json.load(open(p)))
        self.spy = bars.pop("SPY")
        self.bars = bars
        self.idx = {s: {b["ts"]: i for i, b in enumerate(v)} for s, v in bars.items()}
        self.days = sorted({r["ts"] for r in self.rows})
        by_day = defaultdict(list)
        for r in self.rows:
            by_day[r["ts"]].append(r)
        self.by_day = by_day
        # breadth: share of liquid stocks with RSI(14) < 30 that day
        self.breadth = {t: sum((r["rsi"] or 50) < 30 for r in rs) / len(rs) for t, rs in by_day.items()}
        self.spy_idx = {b["ts"]: i for i, b in enumerate(self.spy)}
        self.model = model
        self.ranked = {}
        for side in ("BUY", "SELL"):
            w = model[side]["weights"]
            d = {}
            for t, rs in by_day.items():
                sc = []
                for r in rs:
                    c = comps(r, side)
                    if eligible(c):
                        sc.append((tech_score(c, w), r))
                sc.sort(key=lambda x: x[0], reverse=True)
                d[t] = sc[:TOP * 3]
            self.ranked[side] = d

    def rets60(self, sym, ts):
        i = self.idx[sym].get(ts)
        if i is None or i < 61:
            return None
        c = [b["c"] for b in self.bars[sym][i - 60:i + 1]]
        return np.diff(c) / c[:-1]

    def trade(self, r, side, exit_rule, stop_atr):
        """(return net of cost, holding days, exit index) or None."""
        bars = self.bars[r["sym"]]
        i = self.idx[r["sym"]][r["ts"]]
        if i + 6 >= len(bars):
            return None
        sgn = 1 if side == "BUY" else -1
        ent = bars[i + 1]["o"]
        atr = (r.get("atrp") or 2.0) / 100 * bars[i]["c"]
        stop = ent - sgn * stop_atr * atr if stop_atr else None
        maxd = 3 if exit_rule == "hold3" else 5
        px = None
        for k in range(1, maxd + 1):
            b = bars[i + k]
            if stop is not None:
                if sgn > 0 and (b["l"] <= stop):
                    px = min(stop, b["o"]) if k > 1 or b["o"] <= stop else stop
                    break
                if sgn < 0 and (b["h"] >= stop):
                    px = max(stop, b["o"]) if k > 1 or b["o"] >= stop else stop
                    break
            if exit_rule == "upclose":
                prev = bars[i + k - 1]["c"] if k > 1 else ent
                if sgn * (b["c"] - prev) > 0:
                    px = b["c"]
                    break
            if exit_rule == "sma5":
                c5 = [x["c"] for x in bars[i + k - 4:i + k + 1]]
                if sgn * (b["c"] - sum(c5) / 5) > 0:
                    px = b["c"]
                    break
            if k == maxd:
                px = b["c"]
        return sgn * (px / ent - 1) - COST, k, i + k

    def simulate(self, side, exit_rule, stop_atr, filt, min_score, days):
        open_until: list[int] = []         # global day index when each slot frees
        trades = []
        equity = defaultdict(float)
        for d_i, t in enumerate(days):
            if side == "SELL" and "regime" in filt:
                pass
            cands = self.ranked[side].get(t, [])
            if filt.startswith("washout") or "washout" in filt:
                thr = 0.10 if "10" in filt else 0.15
                if side == "BUY" and self.breadth[t] < thr:
                    continue
            if side == "SELL" and filt == "regime" and cands and cands[0][1]["reg"] == "bull":
                continue
            # free slots (positions exit after their holding days; days are
            # sampled every 2 sessions, so compare in real session index)
            si = self.spy_idx.get(t, 0)
            open_until = [u for u in open_until if u > si]
            chosen, picked_rets = 0, []
            for sc, r in cands:
                if sc < min_score or len(open_until) >= K or chosen >= TOP:
                    break
                if "corr" in filt:
                    rr = self.rets60(r["sym"], t)
                    if rr is not None and any(_corr(rr, x) > 0.7 for x in picked_rets):
                        continue
                    if rr is not None:
                        picked_rets.append(rr)
                res = self.trade(r, side, exit_rule, stop_atr)
                if res is None:
                    continue
                ret, hold, _ = res
                open_until.append(si + 1 + hold)
                trades.append(ret)
                equity[days[min(d_i + (hold + 1) // 2, len(days) - 1)]] += ret / K
                chosen += 1
        if not trades:
            return {"n": 0}
        tr = np.array(trades)
        years = (days[-1] - days[0]) / 86400 / 365.25
        curve = np.cumsum([equity[t] for t in days])
        dd = float((np.maximum.accumulate(curve) - curve).max())
        return {"n": int(len(tr)), "hit": round(float((tr > 0).mean() * 100), 1),
                "avg": round(float(np.clip(tr, -.3, .3).mean() * 100), 2),
                "year": round(float(curve[-1] / years * 100), 1),
                "maxdd": round(dd * 100, 1)}

    def spy_year(self, days):
        a, b = self.spy[self.spy_idx[days[0]]]["c"], self.spy[self.spy_idx[days[-1]]]["c"]
        return round(((b / a) ** (365.25 / ((days[-1] - days[0]) / 86400)) - 1) * 100, 1)


def main():
    model = json.load(open("data/top_picks_model.json"))
    D = Data(sys.argv[1], sys.argv[2:], model)
    half = len(D.days) // 2
    train, test = D.days[:half], D.days[half:]
    out = {"spy_train": D.spy_year(train), "spy_test": D.spy_year(test)}
    print("SPY yearly: train", out["spy_train"], "test", out["spy_test"], flush=True)
    for side in ("BUY", "SELL"):
        filts = FILTERS + (("regime",) if side == "SELL" else ())
        if side == "SELL":
            filts = tuple(f for f in filts if "washout" not in f)
        res = []
        for ex, st, fl, ms in itertools.product(EXITS, STOPS, filts, (0.0, model[side]["min_score"])):
            r = D.simulate(side, ex, st, fl, ms, train)
            if r["n"] >= 80:
                res.append(((ex, st, fl, ms), r))
        res.sort(key=lambda x: (x[1]["year"], x[1]["hit"]), reverse=True)
        print(f"\n{side} — best 8 on TRAIN:")
        for k, r in res[:8]:
            print("  ", k, r)
        best = res[0][0]
        base = D.simulate(side, "hold5", None, "none", 0.0, test)
        chosen = D.simulate(side, *best, test)
        hi = max(res, key=lambda x: (x[1]["hit"], x[1]["year"]))
        hit_pick = D.simulate(side, *hi[0], test)
        print(f"{side} TEST baseline (hold5, no filter):", base)
        print(f"{side} TEST best-by-return rule {best}:", chosen)
        print(f"{side} TEST best-by-hit rule {hi[0]}:", hit_pick)
        out[side] = {"rule": best, "test": chosen, "baseline_test": base,
                     "hit_rule": hi[0], "hit_rule_test": hit_pick,
                     "train_top": [(k, r) for k, r in res[:8]]}
    json.dump(out, open("/dev/stdout" if len(sys.argv) < 2 else sys.argv[1] + ".trader.json", "w"),
              indent=1, default=str)


if __name__ == "__main__":
    main()
