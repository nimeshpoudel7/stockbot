"""Moderate-risk strategy: S&P 500 momentum leaders, held 1-3 months.

Self-contained (its own data fetch, list, log and scorecard) so it never
changes the existing scanner picks, Top-10 lists or alerts.

Rule (see study_position.py, point-in-time S&P 500 membership):
  - S&P 500 stocks, >= $3, >= $5M traded per day
  - only stocks above their 50- and 200-day averages (in an uptrend)
  - rank by 12-month return excluding the most recent month; top 10
  - hold about 3 months, re-check monthly, equal money in each
  - CASH (no stocks) while SPY is below its 200-day average

Runs once per morning scan (scanner.run_scan calls run(); failures are
logged and never affect the scan). Every new pick is logged with its price
and graded 1 and 3 months later.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LIST_KEY = "moderate_list"
LOG_KEY = "moderate_log"
MEMBERS_KEY = "moderate_sp500_members"
MEMBERS_TTL = 7 * 86400
TOP_N = 10
MIN_PRICE = 3.0
MIN_DVOL = 5_000_000
LOOKBACK, SKIP = 252, 21
HORIZONS = (21, 63)            # trading days: ~1 and ~3 months
DEDUPE_DAYS = 30               # a held name is logged once per ~month
KEEP_DAYS = 400
ET = ZoneInfo("America/New_York")

# study_position.py, point-in-time S&P 500 (rule mom12_trend, hold 63, top 10)
BACKTEST = {
    "rule": "12-1 momentum, S&P 500, uptrend only, cash when SPY < 200-day, top 10, hold ~3 months",
    "train_period": "2024-03 to 2025-04",
    "test_period": "2025-05 to 2026-08",
    "train": {"hit": 62.3, "month_hit": 69.2, "avg_trade": 8.14, "year": 28.6, "maxdd": 4.1},
    "test": {"hit": 56.7, "month_hit": 75.0, "avg_trade": 8.39, "year": 32.5, "maxdd": 6.7, "trades": 120},
    "spy_year_test": 16.9,
    "expect": "Realistic: roughly +15% to +25% a year in normal markets; losing years "
              "(-10% to -20% or worse) are possible.",
}


# ---------------------------------------------------------------- the list
def row(symbol: str, bars: list[dict]) -> dict | None:
    if not bars or len(bars) < LOOKBACK + 1:
        return None
    c = [b["c"] for b in bars]
    if c[-1] < MIN_PRICE or not c[-1 - LOOKBACK] or not c[-1 - SKIP]:
        return None
    if statistics.median(b["v"] for b in bars[-20:]) * c[-1] < MIN_DVOL:
        return None
    s50, s200 = sum(c[-50:]) / 50, sum(c[-200:]) / 200
    hi = max(b["h"] for b in bars[-252:])
    return {"symbol": symbol, "price": c[-1],
            "mom12": round((c[-1 - SKIP] / c[-1 - LOOKBACK] - 1) * 100, 1),
            "ret1m": round((c[-1] / c[-1 - SKIP] - 1) * 100, 1),
            "above50": c[-1] > s50, "above200": c[-1] > s200,
            "from_high": round((c[-1] / hi - 1) * 100, 1),
            "sma200": round(s200, 4)}


def build(rows: list[dict], spy_closes: list[float] | None) -> dict:
    uniq = {r["symbol"]: r for r in rows if r}
    risk_off = None
    if spy_closes and len(spy_closes) >= 200:
        risk_off = spy_closes[-1] < sum(spy_closes[-200:]) / 200
    ranked = sorted((r for r in uniq.values() if r["above50"] and r["above200"]),
                    key=lambda r: r["mom12"], reverse=True)[:TOP_N]
    for k, r in enumerate(ranked, 1):
        r["rank"] = k
        r["notes"] = ([f"Up {r['ret1m']:.0f}% in the last month: stretched, a pullback entry may be better"]
                      if r["ret1m"] > 20 else [])
    return {"picks": [] if risk_off else ranked, "watch": ranked if risk_off else [],
            "risk_off": risk_off}


# ---------------------------------------------------------------- members
def sp500_members(store) -> list[str]:
    """Current S&P 500 members (scanner's Wikipedia/datahub fetchers),
    cached 7 days; bundled snapshot if both fail."""
    import scanner
    raw = store.poller_get(MEMBERS_KEY)
    if raw:
        try:
            d = json.loads(raw)
            if time.time() - d["ts"] < MEMBERS_TTL and len(d["symbols"]) > 400:
                return d["symbols"]
        except (ValueError, TypeError, KeyError):
            pass
    for fetch in (scanner._fetch_sp500_wikipedia, scanner._fetch_sp500_datahub):
        try:
            syms = fetch()
        except Exception:  # noqa: BLE001 - network is best-effort
            syms = None
        if syms and len(syms) > 400:
            store.poller_set(MEMBERS_KEY, json.dumps({"ts": time.time(), "symbols": syms}))
            return list(syms)
    return sorted(set(scanner.SP500_FALLBACK) | {"MSFT"})


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
        log.info("moderate: %d picks%s", len(res["picks"]),
                 " (cash mode: SPY below 200-day)" if res["risk_off"] else "")
        return res
    except Exception as e:  # noqa: BLE001 - never breaks the morning scan
        log.warning("moderate: list not built: %s", e)
        return None
