"""What usually happened next after each chart-reading finding.

For every stock and every Nth trading day, run chart_analysis.analyze on
the chart AS IT LOOKED THAT DAY (window ending that day — no look-ahead),
collect each active finding, and measure the stock's return from the NEXT
session's open to the close 5 and 10 sessions later. Compare with the
average of all evaluated days ("any day").

Output: data/pattern_stats.json —
  {"meta": {...}, "base": {"up5":..,"up10":..,"avg10":..},
   "stats": {"<key>": {"n":.., "up5":.., "up10":.., "avg5":.., "avg10":..,
                        "med10":..}}}
key = "<finding name>|<bias>" (+ "|<status>" for patterns whose status is
one of a fixed set, e.g. Confirmed / Forming / Failed).

Usage:
    .venv/bin/python study_patterns.py                  # fetch via Alpaca
    .venv/bin/python study_patterns.py --bars a.json b.json  # cached bars
These are descriptive frequencies from one historical window (mostly a
bull market), not promises.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import chart_analysis as CA

WINDOW = 260
STEP = 5
CLIP = 0.30
from pattern_outlook import STATUS_KEYS, finding_key  # noqa: E402,F401 - one key format
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                   "pattern_stats.json")


def one_symbol(args):
    bars, spy = args
    acc = defaultdict(lambda: [[], []])
    base = [[], []]
    n = len(bars)
    spy_by_ts = {b["ts"]: i for i, b in enumerate(spy or [])}
    for i in range(WINDOW, n - 11, STEP):
        win = bars[i - WINDOW + 1:i + 1]
        ent = bars[i + 1]["o"]
        if not ent:
            continue
        r5 = bars[i + 5]["c"] / ent - 1
        r10 = bars[i + 10]["c"] / ent - 1
        base[0].append(r5)
        base[1].append(r10)
        j = spy_by_ts.get(bars[i]["ts"])
        spyc = [b["c"] for b in spy[max(0, j - WINDOW + 1):j + 1]] if j is not None else None
        # a late-evening "now": the day's bar is complete
        d = dt.datetime.fromtimestamp(bars[i]["ts"], dt.timezone.utc).date()
        now = dt.datetime(d.year, d.month, d.day, 20, 0, tzinfo=CA.ET)
        try:
            rep = CA.analyze(win, spy_closes=spyc, now=now)
        except Exception:  # noqa: BLE001 - one bad window never stops the study
            continue
        if not rep:
            continue
        for sec in rep["sections"]:
            for f in sec["items"]:
                if f["active"]:
                    k = finding_key(f)
                    acc[k][0].append(r5)
                    acc[k][1].append(r10)
    return dict(acc), base


def _clip(x):
    return max(-CLIP, min(CLIP, x))


def run(all_bars: dict, spy: list[dict], workers: int = 2) -> dict:
    merged = defaultdict(lambda: [[], []])
    base = [[], []]
    jobs = [(b, spy) for s, b in all_bars.items() if s != "SPY" and len(b) > WINDOW + 20]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for acc, b in ex.map(one_symbol, jobs, chunksize=8):
            for k, (a5, a10) in acc.items():
                merged[k][0] += a5
                merged[k][1] += a10
            base[0] += b[0]
            base[1] += b[1]

    def summ(r5, r10):
        s10 = sorted(r10)
        return {"n": len(r10),
                "up5": round(sum(x > 0 for x in r5) / len(r5) * 100, 1),
                "up10": round(sum(x > 0 for x in r10) / len(r10) * 100, 1),
                "avg5": round(sum(map(_clip, r5)) / len(r5) * 100, 2),
                "avg10": round(sum(map(_clip, r10)) / len(r10) * 100, 2),
                "med10": round(s10[len(s10) // 2] * 100, 2)}
    out = {"meta": {"symbols": len(jobs), "window": WINDOW, "step": STEP,
                    "horizons": [5, 10], "generated": dt.date.today().isoformat(),
                    "note": "Entry next open, exit close N sessions later; "
                            "returns clipped at +/-30% for averages."},
           "base": summ(*base),
           "stats": {k: summ(*v) for k, v in sorted(merged.items()) if len(v[1]) >= 30}}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bars", nargs="*", help="cached {symbol: [bars]} json files")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    data: dict = {}
    if args.bars:
        for p in args.bars:
            data.update(json.load(open(p)))
    else:
        import scanner
        from config import Settings
        s = Settings.from_env()
        rest = scanner.AlpacaREST(s.alpaca_key, s.alpaca_secret)
        data = rest.daily_bars_multi(sorted(set(scanner.SP500_FALLBACK) | {"SPY"}), days=900)
    spy = data["SPY"]
    res = run(data, spy, args.workers)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(res, f, indent=1)
    print(f"wrote {OUT}: {len(res['stats'])} findings, base {res['base']}")


if __name__ == "__main__":
    main()
