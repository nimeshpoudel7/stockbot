"""Step 1 of the combined Top-10 study: extract features + outcomes.

For every Nth past session and every liquid stock (>= $3, >= $5M/day),
compute — using ONLY bars up to that day — the scanner's BUY/SELL scores
and setups, price-action features (RSI 2/14, distance from averages,
today's range position, gap, volume), and for the strongest candidates
the full chart read (bull/bear evidence + active findings). Then record
what happened next: next-open entry, close 1/3/5/10 sessions later, and
whether an ATR stop or target was hit first.

Writes a JSONL file that study_combined_fit.py turns into weights.

    .venv/bin/python study_combined.py --bars a.json b.json --out rows.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from concurrent.futures import ProcessPoolExecutor

import chart_analysis as CA
import scanner

WINDOW = 300
STEP = 2
READ_MIN = 30          # chart read when either side's scanner score >= this
MIN_PRICE = 3.0
MIN_DVOL = 5_000_000
HOR = (1, 3, 5, 10)
_SPY: list[dict] = []
_SPY_IDX: dict[int, int] = {}
_REG: dict[int, str] = {}


def _regimes(spy):
    c = [b["c"] for b in spy]
    out = {}
    for i in range(220, len(c)):
        s200 = sum(c[i - 199:i + 1]) / 200
        s200p = sum(c[i - 219:i - 19]) / 200
        out[spy[i]["ts"]] = ("bull" if c[i] > s200 and s200 > s200p else
                             "bear" if c[i] < s200 and s200 < s200p else "sideways")
    return out


def _init(spy):
    global _SPY, _SPY_IDX, _REG
    _SPY = spy
    _SPY_IDX = {b["ts"]: i for i, b in enumerate(spy)}
    _REG = _regimes(spy)


def _ema(xs, n):
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def _first_hit(bars, i, side, stop, tgt):
    """'target' / 'stop' / None — which ATR level the next 10 sessions hit
    first (same-bar ties count as stop, conservative)."""
    for b in bars[i + 1:i + 11]:
        if side == "BUY":
            if b["l"] <= stop:
                return "stop"
            if b["h"] >= tgt:
                return "target"
        else:
            if b["h"] >= stop:
                return "stop"
            if b["l"] <= tgt:
                return "target"
    return None


def one(args):
    sym, bars = args
    rows = []
    n = len(bars)
    for i in range(WINDOW, n - 11, STEP):
        win = bars[i - WINDOW + 1:i + 1]
        t, p = win[-1], win[-2]
        if t["c"] < MIN_PRICE:
            continue
        med = statistics.median(b["v"] for b in win[-21:-1])
        if med * p["c"] < MIN_DVOL:
            continue
        ent = bars[i + 1]["o"]
        if not ent:
            continue
        j = _SPY_IDX.get(t["ts"])
        spyc = [b["c"] for b in _SPY[max(0, j - WINDOW + 1):j + 1]] if j is not None else None
        res = scanner.technical_screen(win, spy_closes=spyc)
        if res is None:
            continue
        ind = res["indicators"]
        closes = [b["c"] for b in win]
        rng = t["h"] - t["l"]
        row = {
            "sym": sym, "ts": t["ts"], "reg": _REG.get(t["ts"]),
            "tech": res["tech_score"], "bear": res["bear_score"],
            "pb": res.get("playbook"), "trend": res.get("trend"),
            "setups": [s[0] for s in res["setups"]],
            "bsetups": [s[0] for s in res["bear_setups"]],
            "rsi": ind.get("rsi"), "rsi2": ind.get("rsi2"),
            "d20": ind.get("dist_sma20_atr"), "rs63": ind.get("rs63"),
            "vr": ind.get("vol_ratio"),
            "a200": (t["c"] > ind["sma200"]) if ind.get("sma200") else None,
            "a50": (t["c"] > ind["sma50"]) if ind.get("sma50") else None,
            "ema": 1 if _ema(closes[-60:], 9) > _ema(closes[-60:], 21) else -1,
            "cpos": (t["c"] - t["l"]) / rng if rng > 0 else 0.5,
            "pct": (t["c"] / p["c"] - 1) * 100,
            "gap": (t["o"] / p["c"] - 1) * 100,
            "atrp": (ind["atr14"] / t["c"] * 100) if ind.get("atr14") else None,
            "r": {h: bars[i + h]["c"] / ent - 1 for h in HOR},
        }
        lv = res.get("levels") or {}
        for side in ("BUY", "SELL"):
            if lv.get(side):
                stop, tgt = lv[side]
                # levels are from the close; shift to the actual entry
                d = ent - t["c"]
                row["hit_" + side] = _first_hit(bars, i, side, stop + d, tgt + d)
        if max(row["tech"], row["bear"]) >= READ_MIN or (row["rsi2"] or 50) < 10 \
                or (row["rsi2"] or 50) > 90:
            try:
                rep = CA.analyze(win[-260:], spy_closes=spyc[-260:] if spyc else None,
                                 now=None)
            except Exception:  # noqa: BLE001
                rep = None
            if rep:
                s = rep["summary"]
                row["cb"], row["cr"] = s["bull"], s["bear"]
                row["f"] = [f"{f['name']}|{f['bias']}" for sec in rep["sections"]
                            for f in sec["items"] if f["active"] and f["bias"] != "neutral"]
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    a = ap.parse_args()
    data = {}
    for p in a.bars:
        data.update(json.load(open(p)))
    spy = data.pop("SPY")
    jobs = [(s, b) for s, b in data.items() if len(b) > WINDOW + 20]
    n = 0
    with open(a.out, "w") as fh, ProcessPoolExecutor(a.workers, initializer=_init,
                                                     initargs=(spy,)) as ex:
        for rows in ex.map(one, jobs, chunksize=4):
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
            n += len(rows)
    print("rows", n)


if __name__ == "__main__":
    main()
