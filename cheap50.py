"""Cheap leaders: the 5 strongest S&P 500 stocks priced under $50.

Separate from everything else (own list, log, scorecard, tab).

Rule (study_combo50.py + the cheap-stock search, point-in-time S&P 500
incl. removed stocks, ACTUAL past share prices rebuilt from split history):
  - S&P 500 stocks with a share price from $3 to $50, >= $5M traded per day
  - rank by the 6-month return excluding the latest month; top 5
  - hold about 3 months; a new batch every month; equal money in each
  - no trend filter and no cash rule (both did worse for cheap stocks)

Chosen on 2016-2021 (62% of trades up, ~+30% a year) and checked on
2022-2026: 55% of trades up, ~+15.5% a year vs SPY +13.7% — only slightly
better than the index, with a -25% drop. 10 years: ~+25% a year vs +15.5%.
Fewer, cheaper, more volatile stocks: weaker evidence than Leaders 3 months.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import moderate

log = logging.getLogger(__name__)
sp500_members = moderate.sp500_members  # same cached S&P member list

LIST_KEY = "cheap50_list"
LOG_KEY = "cheap50_log"
TOP_N = 5
MAX_PRICE = 50.0
LOOK_6M, SKIP_1M = 126, 21
MIN_PRICE = 3.0
MIN_DVOL = 5_000_000
LOOKBACK, SKIP = 252, 21
HORIZONS = (21, 63)            # trading days: ~1 and ~3 months
DEDUPE_DAYS = 30               # a held name is logged once per ~month
KEEP_DAYS = 400
ET = ZoneInfo("America/New_York")

# cheap-stock search (6-1 momentum, under $50, top 5, hold 63), 10 years
BACKTEST = {
    "rule": "6-1 momentum, S&P 500 stocks priced $3-$50, top 5, hold ~3 months, new batch monthly",
    "long": {"years": "2016-10 to 2026-07", "trades": 585, "hit": 59.3, "batch_hit": 66,
             "avg_trade": 6.2, "year": 24.7, "spy_year": 15.5, "maxdd": 27.1, "spy_maxdd": 18.0,
             "worst": -50, "beat_years": "7 of 11"},
    "by_year": [[2016, 29.2, 24.6], [2017, 28.2, 18.9], [2018, 12.9, 3.3], [2019, -1.2, 11.2],
                [2020, 49.2, 31.7], [2021, 58.6, 14.7], [2022, -8.0, -7.8], [2023, 8.8, 26.7],
                [2024, 24.8, 15.8], [2025, 10.4, 16.3], [2026, 100.0, 27.4]],
    "halves": [["2016-10 to 2021 (picked on)", 62.5, 29.5, 16.4], ["2022 to 2026-07 (checked on)", 55.3, 15.5, 13.7]],
    "checks": ["picked on 2016-2021 from 90 settings (5 rankings x 3 filters x cash on/off x 5/10/20 stocks)",
               "2022-2026 check: +15.5% a year vs SPY +13.7%, 55% of trades up, worst drop -25%",
               "the next four settings from the search did worse than SPY on 2022-2026 — the edge is fragile",
               "all three strategies limited to under $50 made only +12% to +13% a year (below SPY)"],
    "outcomes": {"p10": -18, "p25": -7, "median": 3, "p75": 16, "p90": 32},
    "entry": "Buy at the market price as soon as it is on the list (next open).",
    "expect": "Realistic: close to SPY or a little better (the 2022-2026 check), with bigger swings "
              "than the other tabs. Cheap stocks are not cheaper in value — share price does not "
              "change returns; with fractional shares any stock is affordable.",
}


# ---------------------------------------------------------------- the list
def row(symbol: str, bars: list[dict]) -> dict | None:
    """moderate.row's numbers plus the 6-1 month return (no trend filter)."""
    r = moderate.row(symbol, bars)
    if not r or len(bars) < LOOK_6M + 1 or not bars[-1 - LOOK_6M]["c"]:
        return None
    if not (moderate.MIN_PRICE <= r["price"] < MAX_PRICE):
        return None
    r["mom6"] = round((bars[-1 - SKIP_1M]["c"] / bars[-1 - LOOK_6M]["c"] - 1) * 100, 1)
    return r


def build(rows: list[dict], spy_closes: list[float] | None = None) -> dict:
    uniq = {r["symbol"]: r for r in rows if r}
    ranked = sorted(uniq.values(), key=lambda r: r["mom6"], reverse=True)[:TOP_N]
    for k, r in enumerate(ranked, 1):
        r["rank"] = k
        # no "wait for a pullback" note: study_entry.py found that waiting for
        # a dip, limit orders and skipping gap-ups all did WORSE than buying
        # at the next open, and very stretched picks did not do worse
        r["notes"] = []
    # no trend filter and no cash rule (both did worse for cheap stocks)
    return {"picks": ranked, "watch": [], "risk_off": False}


# ---------------------------------------------------------------- log
def _load(store) -> list[dict]:
    try:
        return json.loads(store.poller_get(LOG_KEY) or "[]")
    except (ValueError, TypeError):
        return []


def _save(store, entries: list[dict]) -> None:
    cut = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    store.poller_set(LOG_KEY, json.dumps([e for e in entries if e["date"] >= cut]))


def record(store, picks: list[dict], today: str) -> int:
    entries = _load(store)
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=DEDUPE_DAYS)).isoformat()
    recent = {e["symbol"] for e in entries if e["date"] > since}
    new = 0
    for r in picks:
        if r["symbol"] in recent:
            continue
        entries.append({"date": today, "symbol": r["symbol"], "price": r["price"],
                        "rank": r["rank"], "mom6": r.get("mom6")})
        recent.add(r["symbol"])
        new += 1
    if new:
        _save(store, entries)
    return new


def grade(store, bars_by_sym: dict[str, list[dict]]) -> int:
    entries = _load(store)
    today = dt.datetime.now(ET).date().isoformat()
    done = 0
    for e in entries:
        bars = bars_by_sym.get(e["symbol"])
        if not bars or all(f"r{h}" in e for h in HORIZONS):
            continue
        dates = [dt.datetime.fromtimestamp(b["ts"], dt.timezone.utc).date().isoformat() for b in bars]
        if e["date"] not in dates:
            continue
        i = dates.index(e["date"])
        for h in HORIZONS:
            if f"r{h}" not in e and i + h < len(bars) and dates[i + h] < today:
                e[f"r{h}"] = round((bars[i + h]["c"] / e["price"] - 1) * 100, 2)
                done += 1
    if done:
        _save(store, entries)
    return done


def summary(store) -> dict:
    entries = _load(store)
    out = {"picks": len(entries), "since": min((e["date"] for e in entries), default=None)}
    for h in HORIZONS:
        v = [e[f"r{h}"] for e in entries if f"r{h}" in e]
        out[f"n{h}"] = len(v)
        if v:
            out[f"hit{h}"] = round(sum(x > 0 for x in v) / len(v) * 100, 1)
            out[f"avg{h}"] = round(sum(v) / len(v), 2)
    out["open"] = sum(1 for e in entries if f"r{HORIZONS[-1]}" not in e)
    out["recent"] = sorted(entries, key=lambda e: e["date"], reverse=True)[:30]
    return out


# ---------------------------------------------------------------- plan
HOLD_CAL_DAYS = 91            # ~63 trading days


def plan(store, symbol: str) -> dict | None:
    """When to buy / when to sell for one stock under this strategy.

    The tested rule: buy when a stock enters the top 5 (equal money),
    hold ~3 months (63 trading days), then sell. While SPY is below its
    200-day no NEW buys are made (in the backtest open positions were kept
    to their 3 months; selling them early is the safer option). A close
    below the stock's own 200-day is an extra safety exit that was NOT in
    the backtest. Returns None when the stock was never on the list."""
    symbol = symbol.upper()
    try:
        data = json.loads(store.poller_get(LIST_KEY) or "null")
    except (ValueError, TypeError):
        data = None
    rows = (data or {}).get("picks") or (data or {}).get("watch") or []
    cur = next((r for r in rows if r["symbol"] == symbol), None)
    entries = [e for e in _load(store) if e["symbol"] == symbol]
    last = max(entries, key=lambda e: e["date"]) if entries else None
    if not cur and not last:
        return None
    risk_off = bool((data or {}).get("risk_off"))
    out = {"symbol": symbol, "risk_off": risk_off, "on_list": bool(cur) and not risk_off,
           "rank": cur["rank"] if cur else None, "row": cur,
           "list_date": (data or {}).get("date")}
    if last:
        # the current streak: monthly re-logs while it stays on the list
        ds = sorted(e["date"] for e in entries)
        start = ds[-1]
        for prev, nxt in zip(reversed(ds[:-1]), reversed(ds[1:])):
            if (dt.date.fromisoformat(nxt) - dt.date.fromisoformat(prev)).days > 40:
                break
            start = prev
        first = next(e for e in entries if e["date"] == start)
        # each month on the list is a fresh 3-month tranche, so the sell
        # date is 3 months after the LAST month it was on the list
        sell_by = dt.date.fromisoformat(last["date"]) + dt.timedelta(days=HOLD_CAL_DAYS)
        today = dt.datetime.now(ET).date()
        oc = BACKTEST.get("outcomes") or {}
        out["price_guide"] = {k: round(first["price"] * (1 + v / 100), 2) for k, v in oc.items()}
        out["outcomes"] = oc
        out.update({"listed_on": start, "entry_price": first["price"],
                    "last_listed": last["date"], "sell_by": sell_by.isoformat(),
                    "days_left": (sell_by - today).days,
                    "result_1m": first.get("r21"), "result_3m": first.get("r63")})
    # one clear action
    if risk_off:
        out["action"] = "hold_no_new"
    elif last and out["days_left"] <= 0:
        out["action"] = "sell_due" if not cur else "renew"
    elif cur:
        out["action"] = "buy" if not last or last["date"] == out["list_date"] else "hold"
    else:
        out["action"] = "dropped"
    return out


# ---------------------------------------------------------------- job
def run(store, rest, spy_closes=None) -> dict | None:
    """Build today's list, log new picks, grade old ones. Never raises."""
    try:
        members = sp500_members(store)
        pending = {e["symbol"] for e in _load(store) if any(f"r{h}" not in e for h in HORIZONS)}
        syms = sorted(set(members) | pending | {"SPY"})
        bars = {}
        for i in range(0, len(syms), 100):
            bars.update(rest.daily_bars_multi(syms[i:i + 100], days=300))
        spy = spy_closes or [b["c"] for b in bars.get("SPY", [])]
        rows = [row(s, bars[s]) for s in members if s in bars]
        res = build(rows, spy)
        today = dt.datetime.now(ET).date().isoformat()
        res.update({"ts": dt.datetime.now(ET).isoformat(), "date": today,
                    "ranked_from": sum(1 for r in rows if r), "backtest": BACKTEST})
        store.poller_set(LIST_KEY, json.dumps(res))
        record(store, res["picks"], today)
        grade(store, bars)
        log.info("cheap50: %d picks%s", len(res["picks"]),
                 " (cash mode: SPY below 200-day)" if res["risk_off"] else "")
        return res
    except Exception as e:  # noqa: BLE001 - never breaks the morning scan
        log.warning("cheap50: list not built: %s", e)
        return None
