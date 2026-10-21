"""High-conviction strategy: the 5 strongest S&P 500 leaders near their highs.

Separate from everything else (own list, log, scorecard, tab) — it does
not change the Today picks, Top 10, alerts or the Moderate-risk list.

Rule (study_conviction.py, point-in-time S&P 500 membership):
  - S&P 500 stocks, >= $3, >= $5M traded per day
  - above their 50- and 200-day averages AND within 10% of their 52-week
    high (leaders still near their highs, the "52-week-high momentum"
    effect)
  - rank by 12-month return excluding the latest month; top 5 only
  - hold about 3 months, re-check monthly, equal money in each
  - CASH (no stocks) while SPY is below its 200-day average

Higher return and precision than Moderate risk in the backtest, but only 5
stocks: one bad stock moves the whole portfolio (worst single trade in the
test was about -40%).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import moderate

log = logging.getLogger(__name__)
row = moderate.row                      # same per-stock numbers
sp500_members = moderate.sp500_members  # same cached S&P member list

LIST_KEY = "conviction_list"
LOG_KEY = "conviction_log"
TOP_N = 5
NEAR_HIGH = -10.0           # within 10% of the 52-week high
MIN_PRICE = 3.0
MIN_DVOL = 5_000_000
LOOKBACK, SKIP = 252, 21
HORIZONS = (21, 63)            # trading days: ~1 and ~3 months
DEDUPE_DAYS = 30               # a held name is logged once per ~month
KEEP_DAYS = 400
ET = ZoneInfo("America/New_York")

# study_conviction.py, point-in-time S&P 500 (near_high, top 5, hold 63)
BACKTEST = {
    "rule": "12-1 momentum, S&P 500, uptrend, within 10% of 52-week high, cash when SPY < 200-day, top 5, hold ~3 months",
    "train_period": "2024-03 to 2025-04",
    "test_period": "2025-05 to 2026-08",
    "train": {"hit": 67.7, "month_hit": 84.6, "avg_trade": 12.14, "year": 42.1, "maxdd": 1.8},
    "test": {"hit": 60.0, "month_hit": 75.0, "avg_trade": 12.34, "year": 45.6, "maxdd": 7.8,
             "trades": 60, "worst": -40.4},
    "moderate_test": {"hit": 56.7, "year": 32.5},
    "spy_year_test": 16.9,
    "periods": [["2024-03 to 2024-10", 74.3, 53.3], ["2024-10 to 2025-04", 60.0, 32.2],
                ["2025-05 to 2025-12", 65.7, 37.3], ["2025-12 to 2026-08", 52.0, 55.1]],
    # study_long.py: the same rule replayed over ~10 years with point-in-time
    # S&P membership incl. later-removed stocks (169 delisted tickers had no
    # price history, so some bias remains). Before 2024-03 = never seen.
    "long": {'years': '2016-10 to 2026-07', 'trades': 480, 'hit': 56.7, 'year': 15.1, 'spy_year': 15.5, 'maxdd': 28.4, 'unseen': {'period': '2016-10 to 2024-02', 'hit': 56.9, 'year': 9.9, 'spy_year': 14.5}, 'bad_years': '2018: -20.6% (SPY +3.3%); 2022: mostly cash, -3.4% (SPY -7.8%)'},
    "expect": 'Over 10 years the same rule roughly matched SPY (about +15% a year vs +15.5%) with deeper drops (worst about -28%), lagged it in 2016-2023 and beat it in 2024-2026. Not a reliable way to beat the index.',
}


# ---------------------------------------------------------------- the list
def build(rows: list[dict], spy_closes: list[float] | None) -> dict:
    uniq = {r["symbol"]: r for r in rows if r}
    risk_off = None
    if spy_closes and len(spy_closes) >= 200:
        risk_off = spy_closes[-1] < sum(spy_closes[-200:]) / 200
    ranked = sorted((r for r in uniq.values() if r["above50"] and r["above200"]
                     and r["from_high"] >= NEAR_HIGH),
                    key=lambda r: r["mom12"], reverse=True)[:TOP_N]
    for k, r in enumerate(ranked, 1):
        r["rank"] = k
        r["notes"] = ([f"Up {r['ret1m']:.0f}% in the last month: stretched, a pullback entry may be better"]
                      if r["ret1m"] > 20 else [])
    return {"picks": [] if risk_off else ranked, "watch": ranked if risk_off else [],
            "risk_off": risk_off}


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
                        "rank": r["rank"], "mom12": r["mom12"]})
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
        log.info("conviction: %d picks%s", len(res["picks"]),
                 " (cash mode: SPY below 200-day)" if res["risk_off"] else "")
        return res
    except Exception as e:  # noqa: BLE001 - never breaks the morning scan
        log.warning("conviction: list not built: %s", e)
        return None
