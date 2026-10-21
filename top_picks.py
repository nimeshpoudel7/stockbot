"""Top 10 BUY / Top 10 SELL right now — one combined score per side.

Combines everything the bot knows about a stock, live, every 15 minutes:

  setup     the scanner's BUY / SELL playbooks (the 8:00 AM logic)
  chart     the full chart read: patterns, candles, volume, EMA/SMA trend,
            levels, gaps, breakouts, VWAP (evidence share for the side)
  pullback  short-term pullback inside the long-term trend (RSI 2)
  action    where the stock sits in today's range
  trend     200-day / 50-day / EMA 9-21 agreement
  news      Finnhub headlines + Finnhub analyst ratings + Marketaux
            sentiment (sentiment.get_sentiment), signed by side
  health    Piotroski / Altman / DCF overlay (scanner.fundamental_bonus)

The technical weights and the minimum score come from
data/top_picks_model.json, fitted by study_combined_fit.py on the first
half of ~3.5 years of history and checked on the second half. News and
health can't be replayed historically, so they get small fixed weights
and are judged by the live scorecard instead (see scorecard.py).
"""
from __future__ import annotations

import json
import os
import time

import chart_analysis as CA

TOP_N = 10
SHORTLIST = 40              # per side: chart reads + news on the strongest
READ_BUDGET_SECS = 60.0
NEWS_W = 0.08               # +-0.08 on a 0..1 score at full-strength news
HEALTH_W = 0.04
COMPONENTS = ("setup", "chart", "pullback", "action", "weakclose", "trend", "rs")
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                          "top_picks_model.json")
_DEFAULT = {"BUY": {"weights": {k: 1.0 for k in COMPONENTS}, "min_score": 0.3},
            "SELL": {"weights": {k: 1.0 for k in COMPONENTS}, "min_score": 0.3}}


def load_model() -> dict:
    try:
        with open(MODEL_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return _DEFAULT


def _ema(xs, n):
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def features(res: dict, bars: list[dict], summary: dict | None = None) -> dict:
    """The same per-stock fields study_combined.py records (no outcomes)."""
    ind = res["indicators"]
    t = bars[-1]
    closes = [b["c"] for b in bars[-60:]]
    rng = t["h"] - t["l"]
    row = {"tech": res["tech_score"], "bear": res["bear_score"],
           "pb": res.get("playbook"), "rsi": ind.get("rsi"), "rsi2": ind.get("rsi2"),
           "a200": (t["c"] > ind["sma200"]) if ind.get("sma200") else None,
           "a50": (t["c"] > ind["sma50"]) if ind.get("sma50") else None,
           "ema": 1 if len(closes) >= 21 and _ema(closes, 9) > _ema(closes, 21) else -1,
           "cpos": (t["c"] - t["l"]) / rng if rng > 0 else 0.5,
           "rs63": ind.get("rs63")}
    if summary:
        row["cb"], row["cr"] = summary["bull"], summary["bear"]
    return row


def comps(r: dict, side: str) -> dict:
    """Component values (each about 0..1, chart/trend -1..1) for one side."""
    up = side == "BUY"
    cb, cr = r.get("cb"), r.get("cr")
    chart = ((cb - cr) / (cb + cr) * (1 if up else -1)) if cb is not None and (cb + cr) else 0.0
    rsi2 = r.get("rsi2")
    pull = 0.0
    if rsi2 is not None and r.get("a200") is not None:
        if up and r["a200"]:
            pull = 1.0 if rsi2 < 5 else 0.6 if rsi2 < 10 else 0.25 if rsi2 < 20 else 0.0
        if not up and not r["a200"]:
            pull = 1.0 if rsi2 > 95 else 0.6 if rsi2 > 90 else 0.25 if rsi2 > 80 else 0.0
    tr = 0.0
    if r.get("a200") is not None:
        tr += (1 if r["a200"] == up else -1) * 0.4
    if r.get("a50") is not None:
        tr += (1 if r["a50"] == up else -1) * 0.3
    tr += (r["ema"] if up else -r["ema"]) * 0.3
    return {"setup": (r["tech"] if up else r["bear"]) / 100.0,
            "chart": chart, "pullback": pull,
            "action": r["cpos"] if up else 1 - r["cpos"],
            # the opposite read: a weak close (BUY) often snaps back next day
            "weakclose": 1 - r["cpos"] if up else r["cpos"],
            "trend": tr,
            "rs": max(-1.0, min(1.0, (r.get("rs63") or 0.0) * 5)) * (1 if up else -1)}


def eligible(c: dict) -> bool:
    """A pick needs an actual setup: a scanner playbook or a trend pullback."""
    return c["setup"] + c["pullback"] > 0


def tech_score(c: dict, w: dict) -> float:
    tot = sum(w.values()) or 1.0
    return sum(c[k] * w.get(k, 0.0) for k in COMPONENTS) / tot


LABELS = {"setup": "Scanner setup", "chart": "Chart read", "pullback": "Trend pullback",
          "action": "Strong close", "weakclose": "Weak close (snap-back)",
          "rs": "Relative strength", "close": "Close in today's range", "trend": "Trend", "news": "News & analysts",
          "health": "Company health"}


OVERLAY_N = 20             # news + health looked up for the best N per side

# Exit plans tested in study_trader.py (chosen on 2024-05..2025-06,
# judged on 2025-06..2026-10). They raise how often a trade ends green;
# the average gain per trade stays small.
EXITS = {
    "BUY": {"text": "Sell at the first close above the 5-day average, or after 5 trading days.",
            "test_hit": 59.8, "test_avg": 0.07},
    "SELL": {"text": "Cover at the first down close, or after 5 trading days.",
             "test_hit": 63.6, "test_avg": 0.06},
}


def rank(cands: list[dict], *, spy=None, now=None, model=None,
         sentiment_fn=None, health_fn=None, earnings_fn=None,
         budget: float = READ_BUDGET_SECS) -> dict:
    """cands: [{symbol, res (technical_screen), bars}] for liquid stocks.

    Returns {"BUY": [...], "SELL": [...], ...} — top 10 per side, best
    first, each with its 0-100 score and the points behind it.

    1. technical score without the chart read for everyone (cheap)
    2. chart read for the best SHORTLIST per side (time-boxed)
    3. news + company health for the best OVERLAY_N per side (API calls
       are 24h-cached; this caps new lookups per pass)
    """
    model = model or load_model()
    t0 = time.monotonic()
    out = {"exits": EXITS, "model": {s: {"weights": model[s]["weights"], "min_score": model[s]["min_score"],
                         "test": model[s].get("test"), "test10": model[s].get("test10"),
                         "test_nobar": model[s].get("test_top10_nobar"),
                         "base_test": model[s].get("base_test"),
                         "period": model.get("meta", {}).get("test_period")}
                     for s in ("BUY", "SELL")}}
    reads: dict = {}
    sents: dict = {}

    def chart(c):
        sym = c["symbol"]
        if sym not in reads:
            reads[sym] = None
            if time.monotonic() - t0 < budget:
                try:
                    reads[sym] = CA.analyze(c["bars"][-260:], spy_closes=spy, now=now)
                except Exception:  # noqa: BLE001
                    reads[sym] = None
        return reads[sym]

    for side in ("BUY", "SELL"):
        w = model[side]["weights"]
        tot = sum(w.values()) or 1.0
        sgn = 1 if side == "BUY" else -1
        pre = []
        for c in cands:
            f = features(c["res"], c["bars"])
            cs = comps(f, side)
            if eligible(cs):
                pre.append((tech_score(cs, w), c))
        pre.sort(key=lambda x: x[0], reverse=True)
        rows = []
        for _, c in pre[:SHORTLIST]:
            rep = chart(c)
            f = features(c["res"], c["bars"], rep["summary"] if rep else None)
            cs = comps(f, side)
            parts = {k: cs[k] * w.get(k, 0.0) / tot for k in COMPONENTS}
            rows.append({"c": c, "rep": rep, "f": f, "parts": parts,
                         "raw": sum(parts.values())})
        rows.sort(key=lambda r: r["raw"], reverse=True)
        final = []
        for k, r in enumerate(rows):
            c, rep, f, parts = r["c"], r["rep"], r["f"], r["parts"]
            sym = c["symbol"]
            sent, news, health, hreasons = None, 0.0, 0.0, []
            if k < OVERLAY_N and sentiment_fn:
                try:
                    if sym not in sents:
                        sents[sym] = sentiment_fn(sym)
                    sent = sents[sym]
                    news = max(-1.0, min(1.0, sgn * float(sent.get("sentiment") or 0.0))) * NEWS_W
                except Exception:  # noqa: BLE001 - news is advisory
                    sent = None
            if k < OVERLAY_N and health_fn:
                try:
                    hb, hreasons = health_fn(sym, c["res"]["price"], side)
                    health = max(-1.0, min(1.0, hb)) * HEALTH_W
                except Exception:  # noqa: BLE001
                    health, hreasons = 0.0, []
            earn = None
            if k < OVERLAY_N and earnings_fn:
                try:
                    earn = earnings_fn(sym)
                except Exception:  # noqa: BLE001
                    earn = None
            parts = dict(parts, news=news, health=health)
            # show the two close-position components as one net line
            parts["close"] = parts.pop("action", 0.0) + parts.pop("weakclose", 0.0)
            score = sum(parts.values())
            lv = (c["res"].get("levels") or {}).get(side) or (None, None)
            setups = c["res"]["setups"] if side == "BUY" else c["res"]["bear_setups"]
            final.append({
                "symbol": sym, "side": side, "price": c["res"]["price"],
                "score": int(round(max(0.0, min(1.0, score)) * 100)),
                "raw": round(score, 4),
                # earnings inside the holding window = a coin-flip gap: never
                # in the tested list, shown below it with the warning
                "qualifies": score >= model[side]["min_score"] and not earn,
                "earnings": earn,
                "exit": EXITS[side]["text"],
                "playbook": c["res"].get("playbook") if side == "BUY" else None,
                "parts": [{"key": key, "label": LABELS[key], "pts": round(v * 100, 1)}
                          for key, v in sorted(parts.items(), key=lambda kv: -abs(kv[1]))
                          if abs(v) >= 0.005],
                "setups": [x[2] for x in setups][:4],
                "chart": ({"bias": rep["summary"]["bias"], "headline": rep["summary"]["headline"]}
                          if rep else None),
                "news": ({"label": sent.get("label"), "score": sent.get("sentiment"),
                          "n": (sent.get("n") or 0) + (sent.get("marketaux_n") or 0)}
                         if sent else None),
                "health": list(hreasons)[:2],
                "rsi": f.get("rsi"), "rsi2": f.get("rsi2"),
                "stop": lv[0], "target": lv[1],
            })
        final.sort(key=lambda r: r["raw"], reverse=True)
        out[side] = [r for r in final if r["qualifies"]][:TOP_N]
        # fill the rest of the 10 with the best below the tested bar
        out[side + "_near"] = [r for r in final if not r["qualifies"]][:TOP_N - len(out[side])]
    out.update({"screened": len(cands), "analyzed": sum(1 for v in reads.values() if v),
                "build_secs": round(time.monotonic() - t0, 1)})
    return out


MIN_PRICE = 3.0
MIN_DVOL = 5_000_000


class Collector:
    """Keeps only the strongest pre-chart candidates per side while the
    15-minute pass streams through ~4,000 stocks chunk by chunk (memory)."""

    def __init__(self, model: dict | None = None, keep: int = SHORTLIST * 2):
        self.model = model or load_model()
        self.keep = keep
        self.pool: dict[str, dict] = {}
        self.best: dict[str, list] = {"BUY": [], "SELL": []}

    def add(self, symbol: str, res: dict | None, bars: list[dict]) -> None:
        if not res or not bars or len(bars) < 61 or bars[-1]["c"] < MIN_PRICE:
            return
        vols = sorted(b["v"] for b in bars[-21:-1])
        if vols[len(vols) // 2] * bars[-2]["c"] < MIN_DVOL:
            return
        f = features(res, bars)
        hit = False
        for side in ("BUY", "SELL"):
            cs = comps(f, side)
            if eligible(cs):
                self.best[side].append((tech_score(cs, self.model[side]["weights"]), symbol))
                hit = True
        if hit:
            self.pool[symbol] = {"symbol": symbol, "res": res, "bars": bars[-260:]}
        if len(self.pool) > self.keep * 3:
            self.prune()

    def prune(self) -> None:
        keepers = set()
        for side in ("BUY", "SELL"):
            self.best[side].sort(reverse=True)
            self.best[side] = self.best[side][:self.keep]
            keepers |= {s for _, s in self.best[side]}
        self.pool = {s: v for s, v in self.pool.items() if s in keepers}

    def candidates(self) -> list[dict]:
        self.prune()
        return list(self.pool.values())
