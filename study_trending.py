"""What did the Top-10 trending list do next? (re-runnable)

Replays trending.mover_metrics / is_candidate / score on every Nth past
session (full-day bars, no chart read — that part adds at most 10 of 100
points), takes the top 10 up and top 10 down, and measures the return
from the NEXT session's open to the close 1, 5 and 10 sessions later,
against the average of all liquid stocks on the same days.

Output: data/trending_stats.json, shown under the Trending tab.

    .venv/bin/python study_trending.py --bars a.json b.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections import defaultdict

import trending

CLIP = 0.30
HOR = (1, 5, 10)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                   "trending_stats.json")


def evaluate(data: dict, step: int = 2) -> dict:
    syms = [s for s in data if s != "SPY" and len(data[s]) > 80]
    by_ts: dict[int, dict] = defaultdict(dict)       # ts -> {sym: idx}
    for s in syms:
        for i, b in enumerate(data[s]):
            by_ts[b["ts"]][s] = i
    days = sorted(by_ts)[60:-11:step]
    acc = {k: {h: [] for h in HOR} for k in ("up", "down", "base", "up_hot", "down_hot")}
    for ts in days:
        rows = []
        for s, i in by_ts[ts].items():
            bars = data[s]
            if i < 60 or i + 10 >= len(bars):
                continue
            m = trending.mover_metrics(bars[i - 59:i + 1], None)
            if not m:
                continue
            ent = bars[i + 1]["o"]
            if not ent:
                continue
            rets = {h: max(-CLIP, min(CLIP, bars[i + h]["c"] / ent - 1)) for h in HOR}
            for h in HOR:
                acc["base"][h].append(rets[h])
            if trending.is_candidate(m):
                sc, _ = trending.score(m, None)
                rows.append((sc, m["pct"] >= 0, rets))
        rows.sort(key=lambda r: r[0], reverse=True)
        for side, up in (("up", True), ("down", False)):
            top = [r for r in rows if r[1] == up][:trending.TOP_N]
            for sc, _, rets in top:
                for h in HOR:
                    acc[side][h].append(rets[h])
                    if sc >= 70:
                        acc[side + "_hot"][h].append(rets[h])

    def summ(d):
        out = {"n": len(d[HOR[0]])}
        for h in HOR:
            v = d[h]
            if v:
                out[f"avg{h}"] = round(sum(v) / len(v) * 100, 2)
                out[f"up{h}"] = round(sum(x > 0 for x in v) / len(v) * 100, 1)
        return out
    first = dt.date.fromtimestamp(days[0]) if days else None
    last = dt.date.fromtimestamp(days[-1]) if days else None
    return {"meta": {"symbols": len(syms), "days": len(days), "step": step,
                     "from": str(first), "to": str(last),
                     "generated": dt.date.today().isoformat(),
                     "note": "Entry next open; exit close N sessions later; "
                             "clipped at +/-30%; chart-read points not replayed."},
            **{k: summ(v) for k, v in acc.items()}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bars", nargs="+", required=True)
    ap.add_argument("--step", type=int, default=2)
    a = ap.parse_args()
    data: dict = {}
    for p in a.bars:
        data.update(json.load(open(p)))
    res = evaluate(data, a.step)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
