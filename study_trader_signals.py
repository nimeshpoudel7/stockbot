"""Validate the scanner's live signals on real history (re-runnable).

Runs the PRODUCTION code (scanner.technical_screen, classify_side,
push_bar) over years of daily bars and measures what actually happened
after every setup that would have been pushed to the phone:

- entry at the NEXT session's open (you act the morning after the scan),
  exit at the close N sessions later; gross returns, no costs
- edge = average return minus the average of ALL (symbol, day) pairs in
  the same market regime (direction-adjusted for SELL); returns clipped
  at +/-30% so a few penny moonshots can't carry the result
- split into two halves of the window — a real edge should show in both

Usage (needs ALPACA_KEY / ALPACA_SECRET in .env; read-only data calls):
    .venv/bin/python study_trader_signals.py             # S&P 500
    .venv/bin/python study_trader_signals.py --sample 600  # + random liquid
                                                          #   non-S&P names
    .venv/bin/python study_trader_signals.py --step 1    # every day (slow)

Caveats: survivorship bias (today's listed names only), IEX-only volume
on the free feed, gross returns. Results tilt the odds; they are not
promises.
"""
from __future__ import annotations

import argparse
import random
from collections import defaultdict

import numpy as np

import scanner
from config import Settings

WINDOW = 300     # bars the scanner sees per screen (matches daily_bars_multi)
HORIZON = 10     # sessions held
CLIP = 0.30


def _regimes(spy: list[dict]) -> dict[int, str]:
    c = [b["c"] for b in spy]
    out = {}
    for i in range(220, len(c)):
        s200 = sum(c[i - 199:i + 1]) / 200
        s200p = sum(c[i - 219:i - 19]) / 200
        out[spy[i]["ts"]] = ("bull" if c[i] > s200 and s200 > s200p else
                             "bear" if c[i] < s200 and s200 < s200p else
                             "sideways")
    return out


def evaluate(bars_by_sym: dict[str, list[dict]], spy: list[dict],
             step: int = 3) -> dict:
    spy_idx = {b["ts"]: i for i, b in enumerate(spy)}
    reg = _regimes(spy)
    hits: dict = defaultdict(list)
    base: dict = defaultdict(list)
    for bars in bars_by_sym.values():
        n = len(bars)
        for i in range(WINDOW, n - HORIZON - 1, step):
            ent = bars[i + 1]["o"]
            if not ent:
                continue
            r = bars[i + HORIZON]["c"] / ent - 1
            rg = reg.get(bars[i]["ts"])
            half = "A" if i < n // 2 else "B"
            for k in ((rg, half), (rg, "*"), ("*", half), ("*", "*")):
                base[k].append(r)
            win = bars[i - WINDOW + 1:i + 1]
            if not scanner.passes_liquidity(win):
                continue
            j = spy_idx.get(bars[i]["ts"])
            spyc = ([b["c"] for b in spy[max(0, j - WINDOW + 1):j + 1]]
                    if j is not None else None)
            res = scanner.technical_screen(win, spy_closes=spyc)
            if res is None:
                continue
            side = scanner.classify_side(res)
            score = res["tech_score"] if side == "BUY" else res["bear_score"]
            if score < scanner.push_bar(side, rg, res.get("playbook")):
                continue
            sgn = 1 if side == "BUY" else -1
            label = (f"BUY-{res['playbook']}" if side == "BUY" else "SELL")
            for name in (label, side):
                for k in ((rg, half), (rg, "*"), ("*", half), ("*", "*")):
                    hits[(name,) + k].append(sgn * r)
    return {"hits": hits, "base": base}


def report(title: str, ev: dict) -> None:
    hits, base = ev["hits"], ev["base"]

    def m(v):
        return float(np.clip(np.array(v), -CLIP, CLIP).mean()) * 100

    print(f"\n[{title}] pushed signals — {HORIZON}-session edge vs "
          f"same-regime random days (clipped ±{CLIP:.0%})")
    names = sorted({k[0] for k in hits})
    for name in names:
        sgn = 1 if name.startswith("BUY") else -1
        line = []
        for rg in ("*", "bull", "sideways", "bear"):
            v = hits.get((name, rg, "*"))
            if not v:
                continue
            e = m(v) - sgn * m(base[(rg, "*")])
            w = (np.array(v) > 0).mean() * 100
            line.append(f"{rg if rg != '*' else 'all'} n={len(v)} "
                        f"{e:+.2f}% win {w:.0f}%")
        halves = []
        for h in ("A", "B"):
            v = hits.get((name, "*", h))
            halves.append(f"{m(v) - sgn * m(base[('*', h)]):+.2f}"
                          if v else "n/a")
        print(f"  {name:13s} halves {halves[0]} / {halves[1]} | "
              + " | ".join(line))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=900,
                    help="daily bars per symbol to fetch (~3.5 years)")
    ap.add_argument("--sample", type=int, default=0,
                    help="also test N random liquid non-S&P symbols")
    ap.add_argument("--step", type=int, default=3,
                    help="evaluate every Nth day (1 = every day, slower)")
    args = ap.parse_args()
    s = Settings.from_env()
    rest = scanner.AlpacaREST(s.alpaca_key, s.alpaca_secret)
    sp = sorted(set(scanner.SP500_FALLBACK) | {"SPY"})
    print(f"fetching {len(sp)} S&P symbols x {args.days} days…")
    data = rest.daily_bars_multi(sp, days=args.days)
    spy = data.pop("SPY")
    report("S&P 500", evaluate(data, spy, args.step))
    if args.sample:
        pool = [x for x in sorted(rest.active_us_equity_symbols())
                if x not in data and x.isalpha() and len(x) <= 4]
        random.seed(42)
        pick = random.sample(pool, min(len(pool), args.sample))
        print(f"\nfetching {len(pick)} random non-S&P symbols…")
        extra = rest.daily_bars_multi(pick, days=args.days)
        extra = {k: v for k, v in extra.items() if len(v) >= 400}
        report(f"random sample ({len(extra)} symbols)",
               evaluate(extra, spy, args.step))


if __name__ == "__main__":
    main()
