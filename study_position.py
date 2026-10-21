"""Position trading on S&P 500 stocks: hold 1-3 months (re-runnable).

Point-in-time membership: a stock only counts from the date it joined the
S&P 500 (Wikipedia "Date added"), so names that rose INTO the index are
not bought before they were members — the main survivorship bias of a
backtest built on today's list. (Stocks REMOVED during the period are
still missing; that bias remains and flatters results somewhat.)

Pre-declared strategies (pick TOP every 21 sessions, hold HOLD sessions,
entry next open, 0.1% round-trip cost):

  mom12        12-month return skipping the last month (12-1)
  mom12_trend  mom12 among stocks above their 50- and 200-day averages;
               cash when SPY is below its 200-day average
  riskadj      12-1 return divided by 12-month volatility (steadier
               leaders), same trend + SPY filters
  smooth       12-1 return x share of up days (steady climbers), filters
  riskadj_dip  riskadj leaders (top 30) bought at their lowest RSI(14)

Selection on the first half of rebalance dates: highest hit rate among
variants making at least MIN_YEAR %/yr; judged once on the second half.

    python study_position.py bars.json added.json        # cached files
    python study_position.py --fetch --years 8           # on the server:
        fetches S&P members + add dates (Wikipedia) and daily bars (Alpaca,
        keys from .env) — a longer history incl. the 2022 bear market
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import sys

import numpy as np

STEP = 21
COST = 0.001
STRATS = ("mom12", "mom12_trend", "riskadj", "smooth", "riskadj_dip")
HOLDS = (21, 63)
TOPS = (10, 20)
MIN_YEAR = 13.0


def load(paths, added_path):
    data = {}
    for p in paths:
        data.update(json.load(open(p)))
    spy = data.pop("SPY")
    added = json.load(open(added_path))
    syms = sorted(s for s in data if s in added)
    dates = [b["ts"] for b in spy]
    di = {t: i for i, t in enumerate(dates)}
    n = len(dates)
    O = np.full((len(syms), n), np.nan)
    C = np.full((len(syms), n), np.nan)
    V = np.full((len(syms), n), np.nan)
    since = np.zeros(len(syms))
    for k, s in enumerate(syms):
        for b in data[s]:
            i = di.get(b["ts"])
            if i is not None:
                O[k, i], C[k, i], V[k, i] = b["o"], b["c"], b["v"]
        try:
            since[k] = dt.datetime.fromisoformat(added[s]).timestamp()
        except ValueError:
            since[k] = 0
    return syms, np.array(dates), O, C, V, since, \
        np.array([b["o"] for b in spy]), np.array([b["c"] for b in spy])


def rsi14(c):
    d = np.diff(c)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:14].mean(), dn[:14].mean()
    for u, x in zip(up[14:], dn[14:]):
        au, ad = (au * 13 + u) / 14, (ad * 13 + x) / 14
    return 100 - 100 / (1 + au / ad) if ad else 100.0


def picks(strat, t, top, dates, C, V, since, spyc):
    c = C[:, t - 252:t + 1]
    last = c[:, -1]
    with np.errstate(all="ignore"):
        ok = (~np.isnan(c[:, 0])) & (~np.isnan(last)) & (last >= 3) & (since <= dates[t])
        dvol = np.nanmedian(V[:, t - 19:t + 1], axis=1) * last
        ok &= dvol >= 5e6
        m12 = c[:, -22] / c[:, 0] - 1
        s50 = np.nanmean(c[:, -50:], axis=1)
        s200 = np.nanmean(c[:, -200:], axis=1)
        rets = np.diff(c, axis=1) / c[:, :-1]
        vol = np.nanstd(rets, axis=1) * np.sqrt(252)
        upshare = np.nanmean(rets > 0, axis=1)
    if strat != "mom12":
        if spyc[t] < spyc[t - 199:t + 1].mean():
            return []                                   # cash
        ok &= (last > s50) & (last > s200)
    if strat in ("mom12", "mom12_trend"):
        score = m12
    elif strat in ("riskadj", "riskadj_dip"):
        score = m12 / np.where(vol > 0, vol, np.nan)
    elif strat == "smooth":
        score = m12 * upshare
    score = np.where(ok & np.isfinite(score), score, -np.inf)
    order = [k for k in np.argsort(-score) if np.isfinite(score[k])]
    if strat == "riskadj_dip":
        order = sorted(order[:30], key=lambda k: rsi14(C[k, t - 60:t + 1]))
    return order[:top]


def run(strat, hold, top, rebal, D):
    dates, O, C, V, since, spyo, spyc = D
    n = len(dates)
    tranches = hold // STEP
    trades, months = [], []
    for t in rebal:
        if t + 1 + hold >= n:
            break
        rs = []
        for k in picks(strat, t, top, dates, C, V, since, spyc):
            e, x = O[k, t + 1], C[k, t + hold]
            if np.isnan(e) or np.isnan(x) or e <= 0:
                continue
            rs.append(x / e - 1 - COST)
        trades += rs
        months.append(np.mean(rs) if rs else 0.0)    # cash month = 0
    if not trades:
        return None
    tr = np.array(trades)
    m = np.array(months)
    years = len(m) * STEP / 252
    curve = np.cumsum(m / tranches)
    dd = float((np.maximum.accumulate(np.concatenate([[0], curve])) - np.concatenate([[0], curve])).max())
    return {"trades": int(len(tr)), "hit": round(float((tr > 0).mean() * 100), 1),
            "month_hit": round(float((m[m != 0] > 0).mean() * 100), 1) if (m != 0).any() else None,
            "avg_trade": round(float(np.clip(tr, -.5, 1).mean() * 100), 2),
            "median_trade": round(float(np.median(tr) * 100), 2),
            "year": round(float(curve[-1] / years * 100), 1),
            "maxdd": round(dd * 100, 1),
            "in_cash": round(float((m == 0).mean() * 100), 0)}


def spy_run(hold, rebal, D):
    dates, O, C, V, since, spyo, spyc = D
    rs = [spyc[t + hold] / spyo[t + 1] - 1 for t in rebal if t + 1 + hold < len(dates)]
    m = np.array(rs)
    return {"hit": round(float((m > 0).mean() * 100), 1),
            "year": round(float(m.sum() / (hold // STEP) / (len(m) * STEP / 252) * 100), 1)}


class _Constituents:
    """Symbol -> 'Date added' from Wikipedia's S&P 500 constituents table
    (stdlib parser, no pandas/lxml needed)."""

    def __init__(self, html: str):
        from html.parser import HTMLParser
        rows, cur, cell = [], None, None
        state = {"in_table": False, "done": False}

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                nonlocal cur, cell
                a = dict(attrs)
                if tag == "table" and a.get("id") == "constituents":
                    state["in_table"] = True
                elif state["in_table"] and tag == "tr":
                    cur = []
                elif state["in_table"] and tag in ("td", "th") and cur is not None:
                    cell = []

            def handle_endtag(self, tag):
                nonlocal cur, cell
                if not state["in_table"]:
                    return
                if tag in ("td", "th") and cell is not None and cur is not None:
                    cur.append("".join(cell).strip())
                    cell = None
                elif tag == "tr" and cur is not None:
                    rows.append(cur)
                    cur = None
                elif tag == "table":
                    state["in_table"] = False

            def handle_data(self, data):
                if cell is not None:
                    cell.append(data)

        P().feed(html)
        self.added = {r[0].replace(".", "."): r[5][:10] for r in rows[1:] if len(r) >= 6}


def fetch(years: int, tmpdir: str) -> tuple[list[str], str]:
    """Download members + bars to tmpdir; returns (bar_paths, added_path)."""
    import os
    import requests
    import scanner
    from config import Settings
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        headers={"User-Agent": "stockbot-study"}, timeout=30).text
    added = _Constituents(html).added
    s = Settings.from_env()
    rest = scanner.AlpacaREST(s.alpaca_key, s.alpaca_secret)
    syms = sorted(added) + ["SPY"]
    print(f"fetching {len(syms)} symbols x {years} years of daily bars...")
    bars = rest.daily_bars_multi(syms, days=years * 252 + 260)
    os.makedirs(tmpdir, exist_ok=True)
    bp, ap = os.path.join(tmpdir, "bars.json"), os.path.join(tmpdir, "added.json")
    json.dump(bars, open(bp, "w"))
    json.dump(added, open(ap, "w"))
    return [bp], ap


def main():
    args = sys.argv[1:]
    if args and args[0] == "--fetch":
        years = int(args[args.index("--years") + 1]) if "--years" in args else 8
        bars, added = fetch(years, "data/study_cache")
    else:
        *bars, added = args
    syms, dates, O, C, V, since, spyo, spyc = load(bars, added)
    D = (dates, O, C, V, since, spyo, spyc)
    n = len(dates)
    rebal = list(range(253, n - 22, STEP))
    half = len(rebal) // 2
    tr_r, te_r = rebal[:half], rebal[half:]
    day = lambda i: dt.date.fromtimestamp(dates[i]).isoformat()  # noqa: E731
    print(f"{len(syms)} S&P stocks | train {day(tr_r[0])}..{day(tr_r[-1])} | test {day(te_r[0])}..{day(te_r[-1])}")
    rows = []
    for s, h, k in itertools.product(STRATS, HOLDS, TOPS):
        a, b = run(s, h, k, tr_r, D), run(s, h, k, te_r, D)
        rows.append((s, h, k, a, b))
        print(f"{s:12s} hold {h} top {k} | TRAIN {a}")
    print("SPY train", {h: spy_run(h, tr_r, D) for h in HOLDS})
    ok = [r for r in rows if r[3] and r[3]["year"] >= MIN_YEAR]
    best = max(ok, key=lambda r: (r[3]["hit"], r[3]["year"]))
    print(f"\nCHOSEN on train: {best[0]} hold {best[1]} top {best[2]}  train={best[3]}")
    print("TEST:", best[4], "| SPY test", spy_run(best[1], te_r, D))
    print("\nall TEST (record only):")
    for s, h, k, a, b in rows:
        print(f"{s:12s} hold {h} top {k} | TEST {b}")
    print("SPY test", {h: spy_run(h, te_r, D) for h in HOLDS})


if __name__ == "__main__":
    main()
