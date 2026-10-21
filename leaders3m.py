"""Leaders 3 months: the 10 strongest S&P 500 stocks of the last 3 months.

Separate from everything else (own list, log, scorecard, tab).

Rule (study_m3.py, point-in-time S&P 500 membership incl. removed stocks):
  - S&P 500 stocks, >= $3, >= $5M traded per day
  - above their 50- and 200-day averages (in an uptrend)
  - rank by the return of the last 3 months (63 sessions); top 10
  - hold about 3 months; a new batch every month; equal money in each
  - no cash rule: in the 10-year test the SPY < 200-day switch made it worse

10-year test (2016-10..2026-07): about +22% a year vs SPY +15.5%, 57% of
trades up, 64% of monthly batches up, worst portfolio drop -12.5%. Beat
SPY in 6 of 11 calendar years — it can lag for a year or two.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import moderate

log = logging.getLogger(__name__)
sp500_members = moderate.sp500_members  # same cached S&P member list

LIST_KEY = "leaders3m_list"
LOG_KEY = "leaders3m_log"
TOP_N = 10
LOOKBACK_3M = 63
MIN_PRICE = 3.0
MIN_DVOL = 5_000_000
LOOKBACK, SKIP = 252, 21
HORIZONS = (21, 63)            # trading days: ~1 and ~3 months
DEDUPE_DAYS = 30               # a held name is logged once per ~month
KEEP_DAYS = 400
ET = ZoneInfo("America/New_York")

# study_m3.py, point-in-time S&P 500 incl. removed stocks, 2016-10..2026-07
BACKTEST = {
    "rule": "3-month momentum, S&P 500, uptrend only, top 10, hold ~3 months, new batch monthly",
    "long": {"years": "2016-10 to 2026-07", "trades": 1159, "hit": 56.8, "batch_hit": 64.1,
             "avg_trade": 5.4, "year": 22.1, "spy_year": 15.5, "maxdd": 12.5, "spy_maxdd": 18.0,
             "worst": -54.8, "beat_years": "6 of 11"},
    "halves": [["2016-10 to 2021", 60.5, 21.1, 16.4], ["2022 to 2026-07", 52.5, 23.3, 14.4]],
    "by_year": [[2016, 26.2, 24.6], [2017, 28.8, 18.9], [2018, 1.1, 3.3], [2019, 0.1, 11.2],
                [2020, 64.6, 31.7], [2021, 9.6, 14.7], [2022, 3.1, -7.8], [2023, 8.4, 26.7],
                [2024, 1.8, 15.8], [2025, 55.5, 16.3], [2026, 72.2, 27.4]],
    "checks": ["0.5% trading cost: still about +20.5% a year",
               "every nearby setting (2-4 month lookback, 5-20 stocks, 2-4 month hold): +16% to +29% a year",
               "any day of the month to buy: +18% to +23% a year",
               "random half of the stocks: +16% to +19% a year",
               "10 random uptrend stocks: about +12.5% a year; this rule beat all 200 random tries"],
    "expect": "Realistic: roughly +18% to +22% a year over many years, but with flat years "
              "where SPY does better (it lagged in 2018, 2019, 2021, 2023 and 2024). "
              "169 delisted stocks had no price history, so the test is slightly optimistic.",
}


# ---------------------------------------------------------------- the list
def row(symbol: str, bars: list[dict]) -> dict | None:
    """moderate.row's numbers plus the 3-month return."""
    r = moderate.row(symbol, bars)
    if not r or len(bars) < LOOKBACK_3M + 1 or not bars[-1 - LOOKBACK_3M]["c"]:
        return None
    r["ret3m"] = round((bars[-1]["c"] / bars[-1 - LOOKBACK_3M]["c"] - 1) * 100, 1)
    return r


def build(rows: list[dict], spy_closes: list[float] | None = None) -> dict:
    uniq = {r["symbol"]: r for r in rows if r}
    ranked = sorted((r for r in uniq.values() if r["above50"] and r["above200"]),
                    key=lambda r: r["ret3m"], reverse=True)[:TOP_N]
    for k, r in enumerate(ranked, 1):
        r["rank"] = k
        r["notes"] = ([f"Up {r['ret1m']:.0f}% in the last month: stretched, a pullback entry may be better"]
                      if r["ret1m"] > 20 else [])
    # no cash rule for this strategy (it hurt in the 10-year test)
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
                        "rank": r["rank"], "ret3m": r.get("ret3m")})
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

    The tested rule: buy when a stock enters the top 10 (equal money),
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
        log.info("leaders3m: %d picks%s", len(res["picks"]),
                 " (cash mode: SPY below 200-day)" if res["risk_off"] else "")
        return res
    except Exception as e:  # noqa: BLE001 - never breaks the morning scan
        log.warning("leaders3m: list not built: %s", e)
        return None
