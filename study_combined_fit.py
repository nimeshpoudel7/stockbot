"""Step 2 of the combined Top-10 study: choose weights honestly.

Reads the rows from study_combined.py. Each stock-day gets a BUY score and
a SELL score built from a few plain components (each roughly 0..1):

  setup    the scanner's own BUY / SELL setup score (the 8:00 AM logic)
  chart    chart-read evidence share in the side's direction (-1..1)
  pullback NEW: short-term pullback inside a long-term trend
           (BUY: above the 200-day and RSI(2) washed out; SELL: below the
           200-day and RSI(2) stretched up)
  action   where today closed in its range (BUY: near the high)
  trend    long-term trend agreement (200-day, 50-day, EMA 9 vs 21)

For each day the top 10 per side are "picked". A pick is CORRECT when the
stock closed higher (BUY) / lower (SELL) N sessions after buying the next
open. Weights and the minimum score are chosen on the FIRST half of the
history only, then judged on the SECOND half they never saw.

    .venv/bin/python study_combined_fit.py rows.jsonl
"""
from __future__ import annotations

import itertools
import json
import sys

from top_picks import COMPONENTS, comps, eligible

TOP = 10
HORIZON = "5"
GRID = (0.0, 1.0, 2.0)
MIN_SCORES = (0.0, 0.3, 0.45, 0.6)
MIN_PICKS = 300            # a weight set needs at least this many train picks


import numpy as np


class Side:
    """Columnar data for one side: components matrix, eligibility, returns,
    and day boundaries (rows sorted by day)."""

    def __init__(self, rows, side):
        sgn = 1 if side == "BUY" else -1
        cs = [comps(r, side) for r in rows]
        self.X = np.array([[c[k] for k in COMPONENTS] for c in cs])
        self.ok = np.array([eligible(c) for c in cs])
        self.R = {h: sgn * np.array([r["r"][h] for r in rows]) for h in ("1", "3", "5", "10")}
        self.hit_tgt = np.array([r.get("hit_" + side) == "target" for r in rows])
        self.hit_stop = np.array([r.get("hit_" + side) == "stop" for r in rows])
        ts = np.array([r["ts"] for r in rows])
        self.bounds = np.flatnonzero(np.diff(ts)) + 1
        self.starts = np.concatenate([[0], self.bounds])
        self.ends = np.concatenate([self.bounds, [len(rows)]])
        self.ndays = len(self.starts)

    def picks(self, w, min_score):
        w = np.array([w[k] for k in COMPONENTS], float)
        sc = self.X @ w / (w.sum() or 1.0)
        sc = np.where(self.ok & (sc >= min_score), sc, -np.inf)
        idx = []
        for a, b in zip(self.starts, self.ends):
            seg = sc[a:b]
            k = min(TOP, int(np.isfinite(seg).sum()))
            if k:
                top = np.argpartition(-seg, k - 1)[:k] if k < len(seg) else np.arange(len(seg))
                idx.extend((a + top[np.isfinite(seg[top])]).tolist())
        return np.array(idx, int)


def stats(S, idx, horizon=HORIZON):
    if len(idx) == 0:
        return {"n": 0}
    r = S.R[horizon][idx]
    return {"n": int(len(idx)), "hit": round(float((r > 0).mean() * 100), 1),
            "avg": round(float(np.clip(r, -.3, .3).mean() * 100), 2),
            "per_day": round(len(idx) / S.ndays, 1),
            "target_first": round(float(S.hit_tgt[idx].mean() * 100), 1),
            "stop_first": round(float(S.hit_stop[idx].mean() * 100), 1)}


def evaluate(S, w, min_score, horizon=HORIZON):
    return stats(S, S.picks(w, min_score), horizon)


def base(S, horizon=HORIZON):
    return stats(S, np.arange(len(S.ok)), horizon)


def fit(A, B, full):
    """Pick the weights whose WORST train half is best (so a set that only
    worked in one stretch of time loses), tie-broken by average return."""
    best = None
    for ws in itertools.product(GRID, repeat=len(COMPONENTS)):
        if not any(ws) or ws[0] + ws[2] == 0:   # must use a setup signal
            continue
        w = dict(zip(COMPONENTS, ws))
        for ms in MIN_SCORES:
            a, b = evaluate(A, w, ms), evaluate(B, w, ms)
            if a["n"] < MIN_PICKS // 2 or b["n"] < MIN_PICKS // 2:
                continue
            key = (min(a["hit"], b["hit"]), min(a["avg"], b["avg"]))
            if best is None or key > best[0]:
                best = (key, w, ms, evaluate(full, w, ms))
    return best


def load(path):
    rows = [json.loads(line) for line in open(path)]
    rows.sort(key=lambda r: r["ts"])
    return rows


def main():
    rows = load(sys.argv[1])
    ts = sorted({r["ts"] for r in rows})
    q1, cut = ts[len(ts) // 4], ts[len(ts) // 2]
    import datetime as dt
    day = lambda t: dt.date.fromtimestamp(t).isoformat()  # noqa: E731
    out = {"meta": {"train_period": f"{day(ts[0])} to {day(cut)}",
                    "test_period": f"{day(cut)} to {day(ts[-1])}",
                    "rows": len(rows), "horizon_days": int(HORIZON), "top": TOP}}
    for side in ("BUY", "SELL"):
        A = Side([r for r in rows if r["ts"] < q1], side)
        B = Side([r for r in rows if q1 <= r["ts"] < cut], side)
        tr = Side([r for r in rows if r["ts"] < cut], side)
        te = Side([r for r in rows if r["ts"] >= cut], side)
        _, w, ms, ev = fit(A, B, tr)
        res = {"weights": w, "min_score": ms, "train": ev,
               "test": evaluate(te, w, ms), "test10": evaluate(te, w, ms, "10"),
               "test3": evaluate(te, w, ms, "3"), "base_test": base(te),
               "setup_only_test": evaluate(te, {k: float(k == "setup") for k in COMPONENTS}, 0.0)}
        for k, v in res.items():
            print(side, k, v, flush=True)
        out[side] = res
    if len(sys.argv) > 2:
        json.dump(out, open(sys.argv[2], "w"), indent=1)


if __name__ == "__main__":
    main()
