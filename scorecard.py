"""Live scorecard for the Top-10 BUY / SELL lists.

Every symbol that makes a Top-10 list is logged once per day per side,
with the price and score when it first appeared. Each later watch pass
grades the open picks from the daily bars it already has: a BUY is
CORRECT when the close N sessions after the pick day is above the pick
price (SELL: below). Results roll up into hit rates the dashboard shows
next to the backtest numbers — the honest check that the list keeps
working with live data.
"""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

LOG_KEY = "top10_log"
KEEP_DAYS = 120
HORIZONS = (5, 10)
ET = ZoneInfo("America/New_York")


def _load(store) -> list[dict]:
    raw = store.poller_get(LOG_KEY)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def _save(store, log: list[dict]) -> None:
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    store.poller_set(LOG_KEY, json.dumps([e for e in log if e["date"] >= cutoff]))


def record(store, lists: dict, today: str) -> int:
    """Log today's first appearance of each (symbol, side). Returns # new."""
    log = _load(store)
    seen = {(e["date"], e["symbol"], e["side"]) for e in log}
    new = 0
    for side in ("BUY", "SELL"):
        for rank, r in enumerate(lists.get(side) or [], 1):
            k = (today, r["symbol"], side)
            if k in seen:
                continue
            seen.add(k)
            log.append({"date": today, "symbol": r["symbol"], "side": side,
                        "price": r["price"], "score": r["score"], "rank": rank})
            new += 1
    if new:
        _save(store, log)
    return new


def pending_symbols(store) -> set[str]:
    return {e["symbol"] for e in _load(store)
            if any(f"r{h}" not in e for h in HORIZONS)}


def grade(store, bars_by_sym: dict[str, list[dict]]) -> int:
    """Fill r5/r10 (signed % return in the pick's direction) for picks whose
    horizon has passed, from daily bars (oldest-first). Returns # graded."""
    log = _load(store)
    done = 0
    for e in log:
        bars = bars_by_sym.get(e["symbol"])
        if not bars or all(f"r{h}" in e for h in HORIZONS):
            continue
        dates = [dt.datetime.fromtimestamp(b["ts"], dt.timezone.utc).date().isoformat()
                 for b in bars]
        if e["date"] not in dates:
            continue
        i = dates.index(e["date"])
        sgn = 1 if e["side"] == "BUY" else -1
        for h in HORIZONS:
            # only COMPLETED sessions: the last bar may be today's partial one
            if f"r{h}" not in e and i + h < len(bars) and \
                    dates[i + h] < dt.datetime.now(ET).date().isoformat():
                e[f"r{h}"] = round(sgn * (bars[i + h]["c"] / e["price"] - 1) * 100, 2)
                done += 1
    if done:
        _save(store, log)
    return done


def summary(store) -> dict:
    log = _load(store)
    out = {"picks": len(log), "since": min((e["date"] for e in log), default=None)}
    for side in ("BUY", "SELL"):
        s = {}
        for h in HORIZONS:
            v = [e[f"r{h}"] for e in log if e["side"] == side and f"r{h}" in e]
            s[f"n{h}"] = len(v)
            if v:
                s[f"hit{h}"] = round(sum(x > 0 for x in v) / len(v) * 100, 1)
                s[f"avg{h}"] = round(sum(v) / len(v), 2)
        s["open"] = sum(1 for e in log if e["side"] == side and "r10" not in e)
        out[side] = s
    out["recent"] = sorted(log, key=lambda e: (e["date"], -e["score"]), reverse=True)[:40]
    return out
