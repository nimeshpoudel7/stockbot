"""Web dashboard — the single control panel.

- Watchlist table with live prices (2s polling)
- Alert CRUD: create / edit / pause / resume / delete, plus fire history
- Per-symbol 1-minute candlestick chart (lightweight-charts via CDN)

No auth — bind to localhost (see README for remote access via Tailscale/SSH).
"""
from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import indicators as ind
from alerts import BAR_TYPES, EVENT_TYPES, TICK_TYPES, describe
from scanner import SCAN_MORNING_KEY, SCAN_WATCH_KEY

import fundamentals
import scanner as scanner_mod
import sentiment as sentiment_mod

ALERT_TYPES = sorted(TICK_TYPES | BAR_TYPES | EVENT_TYPES)

# params spec: field -> (label, default, step) for numbers, or a select-spec
# dict {"select": True, "label": ..., "options": [...], "default": ...}.
PARAM_SPECS = {
    "price_above": {"target": ("Target price $", 0.0, 0.01)},
    "price_below": {"target": ("Target price $", 0.0, 0.01)},
    "day_pct_change": {"pct": ("% move (e.g. 5 or -5)", 5.0, 0.5)},
    "rsi_14": {"above": ("Overbought level", 70, 1), "below": ("Oversold level", 30, 1)},
    "volume_spike": {"mult": ("Volume multiple", 3.0, 0.5), "lookback": ("Avg bars", 20, 1)},
    "high_52w": {},
    "low_52w": {},
    "sma_cross": {"fast": ("Fast SMA", 20, 1), "slow": ("Slow SMA", 50, 1)},
    "bollinger_break": {"n": ("BB period", 20, 1), "k": ("Std devs", 2.0, 0.1)},
    "bollinger_squeeze": {"lookback_days": ("Lookback days", 126, 1)},
    "fib_cross": {"lookback": ("Swing lookback bars", 50, 5)},
    "atr_stop": {"entry": ("Entry price $", 0.0, 0.01), "k": ("ATR multiplier", 2.0, 0.1),
                 "direction": {"select": True, "label": "Direction",
                               "options": ["long", "short"], "default": "long"}},
    "split_announced": {},
    "buyback_announced": {},
    "news_spike": {},
    "sell_the_news": {},
}

TIMEFRAMES = ["1m", "5m", "15m", "1h"]
REGIMES = ["any", "bull", "bear"]

# Chart timeframes: tf param -> (tf label, daily sessions kept). "1d" is the
# live 1-minute view; the others are daily bars cached for 6h.
CHART_TF_DAILY = {"1m": 30, "6m": 130, "1y": 250}
DAILY_BARS_CACHE_DAYS = 260
DAILY_BARS_TTL_SECS = 6 * 3600   # ~6h DB cache on daily bars
QUOTE_CLOSE_TTL_DAYS = 1          # quotes fallback: max one REST fetch/day/symbol

ET_ZONE = ZoneInfo("America/New_York")
MARKET_CLOSE_ET = dtime(16, 0)


def _distance_pct(level: float | None, price: float | None) -> float | None:
    """Signed % the LEVEL sits from the price: (level/price - 1) x 100.

    Positive = the level is above the current price ("+1.8% to go" for a
    breakout). This is the ONE distance definition for the whole stock
    page — the levels table and the breakout note both use it, computed
    from the same single price snapshot, so they can never disagree.
    """
    if level is None or not price:
        return None
    return (level / price - 1.0) * 100.0


def _distance_text(level: float | None, price: float | None) -> str:
    """'+1.8% above' / '-27.3% below' — _distance_pct, formatted once."""
    pct = _distance_pct(level, price)
    if pct is None:
        return "—"
    return f"{pct:+.1f}% {'above' if pct >= 0 else 'below'}"


def _level_rows(levels: dict, price: float | None) -> list[dict]:
    """Ordered display rows for the stock page's price levels.

    Duplicate values collapse into one row carrying both names —
    Resistance (60-session high) IS the 52-week high whenever the high
    printed inside the last 60 sessions, and showing it twice reads as
    two different levels. SMA rows always get their own row.
    """
    spec = [("Support", "support"),
            ("Resistance", "resistance"),
            ("Major resistance (52w high)", "major_resistance"),
            ("Breakout level", "breakout"),
            ("Breakdown point", "breakdown")]
    rows: list[dict] = []
    seen: dict[float, dict] = {}
    for name, key in spec:
        obj = levels.get(key) or {}
        v = obj.get("level")
        basis = obj.get("kind", "")
        if v is None:
            rows.append({"name": name, "value": None, "basis": basis,
                         "distance_text": "—"})
            continue
        k = round(v, 2)
        if k in seen:
            seen[k]["name"] += f" / {name}"
            continue
        row = {"name": name, "value": v, "basis": basis,
               "distance_text": _distance_text(v, price)}
        rows.append(row)
        seen[k] = row
    for name, key, basis in (("SMA 50", "sma50", "50-session trend zone"),
                             ("SMA 200", "sma200", "200-session trend zone")):
        v = levels.get(key)
        rows.append({"name": name, "value": v, "basis": basis,
                     "distance_text": _distance_text(v, price)})
    return rows


def create_app(store, engine, market, actions, dashboard_token: str = "",
               extras: dict | None = None) -> FastAPI:
    """actions: {"add_symbol": async fn(symbol), "remove_symbol": async fn(symbol)}

    When dashboard_token is set, every route requires it as ?token=, an
    X-Dashboard-Token header, or a Bearer token.

    extras (optional): {"regime": fn()->dict|None,
                        "scores": fn()->dict, "scores_refresh": fn(symbol)->dict,
                        "predict_get": fn(symbol)->dict|None,
                        "predict_train": fn(symbol)->dict,
                        "backtest_run": fn()->dict, "backtest_last": fn()->dict|None,
                        "daily_bars": fn(symbol, days)->list[dict]}
    Missing extras -> the related endpoints answer 503.
    """
    app = FastAPI(title="Stockbot dashboard")
    extras = extras or {}

    def _need(name: str):
        fn = extras.get(name)
        if fn is None:
            raise HTTPException(503, f"{name} not configured")
        return fn

    if dashboard_token:
        from fastapi import Request

        @app.middleware("http")
        async def token_gate(request: Request, call_next):
            tok = request.query_params.get("token")
            if not tok:
                tok = request.headers.get("x-dashboard-token", "")
            if not tok:
                auth = request.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    tok = auth[7:].strip()
            if tok != dashboard_token:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

    # ---- pages ---------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML_PAGE

    @app.get("/stock", response_class=HTMLResponse)
    def stock_page():
        return STOCK_PAGE

    # ---- watchlist -----------------------------------------------------
    @app.get("/api/watchlist")
    def api_watchlist():
        return {"symbols": store.watchlist_all()}

    @app.post("/api/watchlist")
    async def api_watchlist_add(body: dict):
        symbol = str(body.get("symbol", "")).strip().upper()
        if not symbol.isalpha() or len(symbol) > 6:
            raise HTTPException(400, "invalid symbol")
        try:
            await actions["add_symbol"](symbol)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "symbol": symbol}

    @app.delete("/api/watchlist/{symbol}")
    async def api_watchlist_remove(symbol: str):
        await actions["remove_symbol"](symbol.upper())
        return {"ok": True}

    # ---- daily-bar cache (shared by chart + quotes fallback) ---------------
    def _daily_bars_cached(symbol: str):
        """Daily bars for symbol, newest-last, DB-cached ~6h (tf='1d').

        Returns a (bars, live_fetch_ok) pair; never raises REST errors —
        callers fall back to whatever is cached. Uses the 'daily_bars'
        extra for a fresh fetch; without it, only the cache is served.
        """
        symbol = symbol.upper()
        now = time.time()
        ts_raw = store.poller_get(f"daily_bars_ts:{symbol}")
        try:
            fresh = ts_raw is not None and (now - float(ts_raw)) < DAILY_BARS_TTL_SECS
        except (TypeError, ValueError):
            fresh = False
        cached = store.candles_load(symbol, "1d", DAILY_BARS_CACHE_DAYS)
        if fresh and cached:
            return cached, True
        fn = extras.get("daily_bars")
        if fn is None:
            return cached, False
        try:
            bars = fn(symbol, DAILY_BARS_CACHE_DAYS)
        except Exception:  # noqa: BLE001 - REST failure -> stale cache only
            return cached, False
        if bars:
            store.candles_save(symbol, "1d", bars)
            store.poller_set(f"daily_bars_ts:{symbol}", repr(now))
            return bars[-DAILY_BARS_CACHE_DAYS:], True
        return cached, False

    def _last_session_close(symbol: str):
        """(close, prev_close) from the daily-bar cache; at most one REST
        fetch per symbol per day. Returns (None, None) when unknown."""
        symbol = symbol.upper()
        key = f"quote_close:{symbol}"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = store.poller_get(key)
        if raw:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = None
            if data and data.get("day") == today:
                return data.get("close"), data.get("prev_close")
        bars, _ok = _daily_bars_cached(symbol)
        if len(bars) >= 1:
            close = bars[-1]["c"]
            prev = bars[-2]["c"] if len(bars) >= 2 else None
            store.poller_set(key, json.dumps(
                {"close": close, "prev_close": prev, "day": today}))
            return close, prev
        return None, None

    # ---- quotes & candles ----------------------------------------------
    @app.get("/api/quotes")
    def api_quotes():
        out = {}
        for symbol in store.watchlist_all():
            st = market.get(symbol)
            price = st.price if st is not None else None
            if price is not None:
                out[symbol] = {
                    "price": price,
                    "day_pct": ind.day_pct_change(
                        price, (st.prev_close or 0) if st is not None else 0),
                    "ts": st.ts if st is not None else None,
                    "stale": False,
                }
                continue
            # no live tick (e.g. market closed): fall back to the last close
            close, prev_close = _last_session_close(symbol)
            if close is None:
                out[symbol] = {"price": None, "day_pct": None, "ts": None,
                               "stale": False}
            else:
                out[symbol] = {
                    "price": close,
                    "day_pct": ind.day_pct_change(close, prev_close or 0),
                    "ts": None,
                    "stale": True,
                }
        return out

    @app.get("/api/candles")
    def api_candles(symbol: str, limit: int = 200, tf: str = "1d"):
        symbol = symbol.upper()
        if tf in CHART_TF_DAILY:
            bars, _ok = _daily_bars_cached(symbol)
            if not bars:
                _need("daily_bars")  # 503 when the extra is not configured
            bars = bars[-CHART_TF_DAILY[tf]:]
        elif tf == "1d":
            bars = store.candles_load(symbol, "1m", min(limit, 500))
            st = market.get(symbol)
            if st and st.forming:
                bars = bars + [st.forming]
        else:
            raise HTTPException(400, "tf must be one of 1d, 1m, 6m, 1y")
        return {"symbol": symbol, "tf": tf,
                "bars": [{"time": b["ts"], "open": b["o"], "high": b["h"],
                          "low": b["l"], "close": b["c"]} for b in bars]}

    # ---- alerts ----------------------------------------------------------
    def _serialize(a: dict) -> dict:
        return {
            "id": a["id"], "symbol": a["symbol"], "type": a["type"],
            "params": a["params"], "one_time": bool(a["one_time"]),
            "cooldown_min": a["cooldown_min"], "expires_at": a["expires_at"],
            "hysteresis_pct": a["hysteresis_pct"],
            "ntfy_priority": a["ntfy_priority"],
            "paused": bool(a["paused"]),
            "timeframe": a.get("timeframe", "1m") or "1m",
            "regime": a.get("regime", "any") or "any",
            "state": a.get("live_state", "ARMED"),
            "description": describe(a),
            "created_at": a["created_at"],
        }

    @app.get("/api/alerts")
    def api_alerts():
        return [_serialize(a) for a in store.alert_list()]

    @app.post("/api/alerts")
    def api_alert_create(body: dict):
        type_ = body.get("type")
        if type_ not in ALERT_TYPES:
            raise HTTPException(400, f"unknown alert type (pick one of {ALERT_TYPES})")
        symbol = str(body.get("symbol", "")).strip().upper()
        if not symbol:
            raise HTTPException(400, "symbol required")
        params = {}
        for field, spec in PARAM_SPECS[type_].items():
            if field not in body.get("params", {}):
                raise HTTPException(400, f"missing param: {field}")
            raw = body["params"][field]
            if isinstance(spec, dict) and spec.get("select"):
                if raw not in spec["options"]:
                    raise HTTPException(
                        400, f"param {field} must be one of {spec['options']}")
                params[field] = raw
            else:
                try:
                    params[field] = float(raw)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"param {field} must be a number")
        timeframe = body.get("timeframe", "1m")
        regime = body.get("regime", "any")
        alert_id = store.alert_create(
            symbol, type_, params,
            one_time=bool(body.get("one_time", False)),
            cooldown_min=float(body.get("cooldown_min", 15)),
            expires_at=body.get("expires_at") or None,
            hysteresis_pct=float(body.get("hysteresis_pct", 0.005)),
            ntfy_priority=int(body.get("ntfy_priority", 3)),
            timeframe=timeframe, regime=regime,
        )
        engine.refresh_alerts()
        return _serialize(store.alert_get(alert_id))

    @app.patch("/api/alerts/{alert_id}")
    def api_alert_update(alert_id: int, body: dict):
        fields: dict = {}
        if "paused" in body:
            fields["paused"] = body["paused"]
        for k in ("params", "one_time", "cooldown_min", "expires_at",
                  "hysteresis_pct", "ntfy_priority", "timeframe", "regime"):
            if k in body:
                fields[k] = body[k]
        if not store.alert_update(alert_id, **fields):
            raise HTTPException(404, "alert not found")
        engine.refresh_alerts()
        return _serialize(store.alert_get(alert_id))

    @app.delete("/api/alerts/{alert_id}")
    def api_alert_delete(alert_id: int):
        if not store.alert_delete(alert_id):
            raise HTTPException(404, "alert not found")
        engine.refresh_alerts()
        return {"ok": True}

    @app.get("/api/alerts/{alert_id}/events")
    def api_alert_events(alert_id: int):
        return store.events_for_alert(alert_id)

    @app.get("/api/meta")
    def api_meta():
        return {"alert_types": ALERT_TYPES, "param_specs": PARAM_SPECS,
                "timeframes": TIMEFRAMES, "regimes": REGIMES,
                "bar_types": sorted(BAR_TYPES)}

    # ---- scanner picks ---------------------------------------------------
    @app.get("/api/scanner")
    def api_scanner():
        def _poller(key: str):
            raw = store.poller_get(key)
            if not raw:
                return None
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None

        return {"morning": _poller(SCAN_MORNING_KEY),
                "watch": _poller(SCAN_WATCH_KEY)}

    # ---- stock detail ----------------------------------------------------
    PROFILE_TTL_SECS = 24 * 3600   # company profile: one Finnhub call/day

    def _company_profile(symbol: str):
        """Finnhub company-profile2 (industry/market cap), 24h cached
        in poller_state under 'company_profile:v2:<SYM>'. None on any miss —
        the detail page just shows '—' for the market-context fields."""
        symbol = symbol.upper()
        key = f"company_profile:v2:{symbol}"
        raw = store.poller_get(key)
        if raw:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = None
            if data and time.time() - float(data.get("fetched_ts", 0)) \
                    < PROFILE_TTL_SECS:
                return data
        api_key = os.environ.get("FINNHUB_KEY", "").strip()
        if not api_key:
            return None
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": symbol, "token": api_key}, timeout=20)
            r.raise_for_status()
            j = r.json() or {}
        except Exception:  # noqa: BLE001 - profile is advisory only
            return None
        if not j.get("ticker"):
            return None
        # profile2's industry field is 'finnhubIndustry' — there are no
        # 'sector'/'industry' keys in the real payload.
        data = {"name": j.get("name"), "ticker": j.get("ticker"),
                "exchange": j.get("exchange"),
                "industry": j.get("finnhubIndustry"),
                "sector": j.get("sector"), "currency": j.get("currency"),
                "market_cap_musd": j.get("marketCapitalization"),
                "fetched_ts": time.time()}
        try:
            store.poller_set(key, json.dumps(data))
        except Exception:  # noqa: BLE001 - cache write is best-effort
            pass
        return data

    @app.get("/api/stock/{symbol}")
    def api_stock(symbol: str):
        """Full detail bundle for one symbol — real data only.

        Every field comes from the 6h-cached daily bars, the 24h-cached
        fundamentals/sentiment/profile pipelines, or scanner.technical_screen
        run on those bars. Fields that cannot be computed are null.
        """
        symbol = symbol.upper()
        if not symbol.isalpha() or not 1 <= len(symbol) <= 6:
            raise HTTPException(400, "invalid symbol")
        bars, _ok = _daily_bars_cached(symbol)
        if not bars:
            raise HTTPException(404, f"no daily data for {symbol}")
        closes = [b["c"] for b in bars]
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        vols = [b["v"] for b in bars]
        last = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else None

        # --- price block (live tick wins, last close otherwise) -----------
        st = market.get(symbol)
        live_price = st.price if st is not None else None
        if live_price is not None:
            price_last, is_live = live_price, True
            day_pct = ind.day_pct_change(live_price, (st.prev_close or 0))
        else:
            price_last, is_live = last, False
            day_pct = ind.day_pct_change(last, prev or 0)
        # ONE price snapshot for every percentage on the page: the header
        # price above is what the levels table, the breakout note and the
        # stats are all measured against. (The verdict/fundamentals keep
        # the daily close — they describe the last completed session.)
        snapshot = price_last

        # --- stats over the 52w window --------------------------------------
        lookback = min(252, len(highs))
        hi52 = max(highs[-lookback:])
        lo52 = min(lows[-lookback:])
        pct_range = ((snapshot - lo52) / (hi52 - lo52) * 100.0
                     if hi52 > lo52 else None)
        stats = {"high_52w": hi52, "low_52w": lo52,
                 "pct_of_52w_range": pct_range,
                 "avg_dollar_volume_20d":
                     scanner_mod.avg_dollar_volume_20d(bars),
                 "bars": len(bars), "as_of": bars[-1]["ts"]}

        # --- technical levels, daily bars only -------------------------------
        # Support    = lowest low of the last 60 sessions (consolidation floor)
        # Resistance = highest high of the last 60 sessions (swing high)
        # Breakout / breakdown levels = Bollinger(20, 2σ) upper / lower band.
        #              The breakout level is only a level: a close above it
        #              counts as a confirmed breakout with volume >=1.5x the
        #              20-day average or a second close above it (see the
        #              buy_zone note, which carries that rule to the page).
        # SMA50/SMA200 = trend zones. RSI/vol-ratio mirror scanner definitions.
        _mid, bb_upper, bb_lower = ind.bollinger_bands(closes)
        rsi14 = None
        if len(closes) >= 15:
            rsi14 = ind.rsi_wilder(closes)[-1]
        vol_ratio = None
        if len(vols) >= 21:
            med = statistics.median(vols[-21:-1])
            vol_ratio = (vols[-1] / med) if med > 0 else None
        # Partial-day flag: the last bar is TODAY's session and the close
        # hasn't printed yet — its volume is a fraction of a full day, so
        # the ratio is labelled "early session" on the page (display only).
        vol_ratio_partial = False
        if vol_ratio is not None:
            now_et = datetime.now(ET_ZONE)
            last_bar_et = datetime.fromtimestamp(bars[-1]["ts"], ET_ZONE)
            vol_ratio_partial = (last_bar_et.date() == now_et.date()
                                 and now_et.time() < MARKET_CLOSE_ET)
        # %B: where the last close sits inside the bands (1.0 = upper band,
        # 0.0 = lower band, 0.5 = the 20-day average); None when the bands
        # are unavailable or degenerate (upper == lower).
        pct_b = ind.bollinger_pctb(last, bb_upper, bb_lower)
        if pct_b is not None:
            pct_b = round(pct_b, 2)
        # Bandwidth + its percentile context vs its own last-126-session
        # history (same rolling construction as the scanner's squeeze
        # check): <=10th percentile = coiled, >=75th = wide, else normal.
        bw_now = ind.bollinger_bandwidth(closes)
        bandwidth = round(bw_now, 4) if bw_now is not None else None
        bandwidth_context = None
        if bw_now is not None and len(closes) >= 126 + 20:
            bw_hist = [ind.bollinger_bandwidth(
                closes[: len(closes) - 126 + k]) for k in range(127)]
            bw_hist = [h for h in bw_hist if h is not None]
            if bw_hist:
                bw_pct = (sum(1 for h in bw_hist if h <= bw_now)
                          / len(bw_hist) * 100.0)
                bandwidth_context = ("coiled" if bw_pct <= 10
                                     else "wide" if bw_pct >= 75
                                     else "normal")
        buy_zone = None
        if bb_upper is not None:
            dist = _distance_pct(bb_upper, snapshot)
            buy_zone = {
                "level": bb_upper, "distance_pct": dist,
                "distance_text": _distance_text(bb_upper, snapshot),
                "note": (("price is already above the breakout level — "
                          if snapshot >= bb_upper else
                          "a daily close above this level is a breakout — ")
                         + "but a breakout counts as confirmed only with "
                           "volume ≥1.5× the 20-day average or a second "
                           "daily close above the level; an unconfirmed "
                           "close above is a possible fake-out "
                           "— not financial advice")}
        levels = {
            "support": {"level": (min(lows[-60:]) if len(lows) >= 60
                                  else None),
                        "kind": "60-session swing low"},
            "resistance": {"level": (max(highs[-60:]) if len(highs) >= 60
                                     else None),
                           "kind": "60-session swing high"},
            "major_resistance": {"level": hi52, "kind": "52-week high"},
            "breakout": {"level": bb_upper,
                         "kind": "Bollinger upper band (20d, 2σ)"},
            "breakdown": {"level": bb_lower,
                          "kind": "Bollinger lower band (20d, 2σ)"},
            "buy_zone": buy_zone,
            "bollinger_mid": _mid,
            "pct_b": pct_b,
            "bandwidth": bandwidth,
            "bandwidth_context": bandwidth_context,
            "sma50": ind.sma(closes, 50), "sma200": ind.sma(closes, 200),
            "rsi14": rsi14, "vol_ratio": vol_ratio,
            "vol_ratio_partial": vol_ratio_partial,
        }
        # Display rows for the page: duplicates collapsed, every distance
        # formatted by the one shared helper off the one snapshot price.
        levels["rows"] = _level_rows(levels, snapshot)

        # --- scanner verdict (technical_screen on the same bars) ------------
        sent = sentiment_mod.get_sentiment(store, symbol)
        verdict = None
        # SPY closes from the regime cache (no extra REST call) give the
        # relative-strength leg of the momentum playbook
        spy_rows = store.candles_load("SPY", "1d", 400)
        res = scanner_mod.technical_screen(
            bars, spy_closes=[r["c"] for r in spy_rows] or None)
        if res is not None:
            fb, freasons = scanner_mod.fundamental_bonus(store, symbol, last)
            sb, sreasons = scanner_mod.short_bonus(store, symbol, last)
            row = dict(res)
            row.update({"fund_bonus": fb, "fund_reasons": freasons,
                        "composite": res["tech_score"] + fb,
                        "short_bonus": sb, "short_reasons": sreasons,
                        "short_composite": res["bear_score"] + sb,
                        "sentiment": sent["sentiment"],
                        "sentiment_payload": sent})
            # Earnings proximity flag (24h-cached, fail open) — flows into
            # the verdict reasons via scanner._candidate_reasons.
            note = scanner_mod.earnings_note(store, symbol)
            if note:
                row["earnings_note"] = note
            side = scanner_mod.classify_side(res)
            score = (row["composite"] if side == "BUY"
                     else row["short_composite"])
            rating = (scanner_mod.side_rating(res["tech_score"], "BUY",
                                              sent["sentiment"], fb)
                      if side == "BUY" else
                      scanner_mod.side_rating(res["bear_score"], "SELL",
                                              sent["sentiment"], sb))
            win = res["setups"] if side == "BUY" else res["bear_setups"]
            lose = res["bear_setups"] if side == "BUY" else res["setups"]
            # Reasons list = the NON-setup lines only (fundamental overlay
            # + earnings flag). The setup reasons themselves render once,
            # in the setups table below — the old payload repeated them
            # here verbatim (via _candidate_reasons) and the page showed
            # every reason twice.
            extra_reasons = list(freasons if side == "BUY" else sreasons)
            if note:
                extra_reasons.append(note)
            risk = scanner_mod.risk_note(res, side)
            if risk:
                extra_reasons.insert(0, risk + " (ATR-based)")
            verdict = {
                "side": side,
                "tech_score": res["tech_score"],
                "bear_score": res["bear_score"],
                "composite": score, "rating": rating,
                "reasons": extra_reasons,
                "setups": [{"name": n, "points": p, "reason": r}
                           for n, p, r in win],
                "counter_setups": [{"name": n, "points": p, "reason": r}
                                   for n, p, r in lose],
                "fundamental_bonus": fb, "fundamental_reasons": freasons,
                "short_bonus": sb, "short_reasons": sreasons,
                "playbook": res.get("playbook") if side == "BUY" else None,
                "trend": res.get("trend"),
                "push_bar": scanner_mod.push_bar(side, None,
                                                 res.get("playbook")),
            }

        # --- fundamentals (24h-cached pipeline; degrades, never 500s) ------
        try:
            fund = fundamentals.get_scores(store, symbol, price=last)
        except Exception as e:  # noqa: BLE001 - detail page must still render
            fund = {"symbol": symbol, "error": str(e)}

        profile = _company_profile(symbol)
        company = None
        if profile:
            company = {"name": profile.get("name"),
                       "sector": profile.get("sector"),
                       "industry": profile.get("industry"),
                       "exchange": profile.get("exchange"),
                       "currency": profile.get("currency"),
                       "market_cap_musd": profile.get("market_cap_musd")}

        return {
            "symbol": symbol,
            "price": {"last": price_last, "live": is_live,
                      "day_pct": day_pct, "prev_close": prev},
            "stats": stats,
            "levels": levels,
            "scanner": verdict,
            "fundamentals": fund,
            "sentiment": sent,
            "company": company,
        }

    # ---- regime ----------------------------------------------------------
    @app.get("/api/regime")
    def api_regime():
        return _need("regime")()

    # ---- fundamental scores ----------------------------------------------
    @app.get("/api/scores")
    def api_scores():
        return _need("scores")()

    @app.post("/api/scores/{symbol}/refresh")
    def api_scores_refresh(symbol: str):
        try:
            return _need("scores_refresh")(symbol.upper())
        except RuntimeError as e:
            raise HTTPException(400, str(e))

    # ---- experimental predictions ----------------------------------------
    @app.get("/api/predict/{symbol}")
    def api_predict(symbol: str):
        payload = _need("predict_get")(symbol.upper())
        if payload is None:
            raise HTTPException(404, "no trained model for this symbol")
        return payload

    @app.post("/api/predict/{symbol}/train")
    def api_predict_train(symbol: str):
        try:
            return _need("predict_train")(symbol.upper())
        except (RuntimeError, ValueError) as e:
            raise HTTPException(400, str(e))

    # ---- backtests --------------------------------------------------------
    @app.get("/api/backtest")
    def api_backtest_last():
        return _need("backtest_last")() or {"ran_at": None, "results": {}}

    @app.post("/api/backtest")
    def api_backtest_run():
        return _need("backtest_run")()

    return app


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stockbot — alerts dashboard</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;max-width:1100px;margin:0 auto;padding:16px;background:#0b0d12;color:#e5e7eb;line-height:1.45;-webkit-text-size-adjust:100%}
h1{font-size:1.3em;margin:0}
h2{font-size:1.08em;margin:30px 0 6px;color:#f3f4f6;border-bottom:1px solid #262a33;padding-bottom:6px}
h3{font-size:1em;margin:16px 0 2px}
header.topbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;justify-content:space-between;background:#12141b;border:1px solid #262a33;border-radius:10px;padding:12px 16px}
header.topbar .meta{text-align:right;color:#9ca3af;font-size:.85em}
.card{background:#12141b;border:1px solid #262a33;border-radius:10px;padding:14px 16px;margin-top:10px;box-shadow:0 1px 3px rgba(0,0,0,.35)}
table{border-collapse:collapse;width:100%;margin-top:8px;font-size:.92em}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #23262e;vertical-align:top}
thead th{background:#1b1f27;color:#cbd5e1;font-weight:600;white-space:nowrap}
tbody tr:nth-child(even){background:rgba(255,255,255,.025)}
tbody tr:hover{background:#1d232e}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tscroll.sticky{overflow-y:auto;max-height:460px}
.tscroll.sticky thead th{position:sticky;top:0;z-index:2}
.sym{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:600;letter-spacing:.02em}
.r-good{color:#4ade80;font-weight:700}.r-mid{color:#fbbf24;font-weight:700}.r-bad{color:#f87171;font-weight:700}
.up{color:#4ade80}.down{color:#f87171}
.tag{display:inline-block;font-size:.7em;font-weight:600;background:#374151;color:#d1d5db;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;text-transform:uppercase;letter-spacing:.05em}
button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:5px 11px;cursor:pointer;margin:1px;font-size:.92em}
button:hover{filter:brightness(1.15)}
button.warn{background:#7c2d12}button.ghost{background:#374151}
button.tfbtn.active{background:#2563eb}
input,select{background:#1a1d24;color:#e6e6e6;border:1px solid #444;border-radius:6px;padding:5px 8px;margin:2px}
form.inline{display:inline}
#chart{height:380px;margin-top:10px}.muted{color:#9ca3af;font-size:.85em}
.paramrow{display:inline-block;margin-right:8px}
@media (max-width:640px){body{padding:10px}.card{padding:10px 12px}#chart{height:300px}header.topbar .meta{text-align:left}}
</style>
</head>
<body>
<header class="topbar">
<div>
<h1>📈 Stockbot</h1>
<div class="muted">personal real-time alerts — not financial advice</div>
</div>
<div class="meta">
<div id="regimeBadge">regime: …</div>
<div id="hdrScan"></div>
</div>
</header>

<h2>Scanner Picks <span class="muted">(market-wide ranked setups, /10 — not financial advice)</span></h2>
<div class="card">
<div>
<button class="ghost" onclick="showScan('morning')">Morning scan</button>
<button class="ghost" onclick="showScan('watch')">Today's watch matches</button>
<span id="scanTs" class="muted"></span>
</div>
<div id="scanMorning">
<h3 class="up">BUY CANDIDATES</h3>
<div class="tscroll sticky"><table id="scanBuys"><thead><tr><th>#</th><th>Symbol</th><th>Price</th><th>Rating</th><th>Breakdown</th></tr></thead><tbody><tr><td colspan="5" class="muted">loading…</td></tr></tbody></table></div>
<h3 class="down">SELL CANDIDATES</h3>
<div class="tscroll sticky"><table id="scanSells"><thead><tr><th>#</th><th>Symbol</th><th>Price</th><th>Rating</th><th>Breakdown</th></tr></thead><tbody><tr><td colspan="5" class="muted">loading…</td></tr></tbody></table></div>
<p class="muted">Rating colors: <span class="r-good">6+/10 strong</span> · <span class="r-mid">4–6/10 moderate</span> · <span class="r-bad">under 4/10 weak</span></p>
</div>
<div id="scanWatch" style="display:none">
<div class="tscroll sticky"><table id="scanWatchTable"><thead><tr><th>Time (ET)</th><th>Symbol</th><th>Side</th><th>Price</th><th>Rating</th><th>Breakdown</th></tr></thead><tbody><tr><td colspan="6" class="muted">loading…</td></tr></tbody></table></div>
</div>
</div>

<h2>Watchlist <span class="muted">(live, refreshes every 2s)</span></h2>
<div class="card">
<form id="addSym" class="inline">
<input id="symInput" placeholder="AAPL" maxlength="6" style="width:90px;text-transform:uppercase">
<button type="submit">+ Add</button>
</form>
<span id="wlMsg" class="muted"></span>
<div class="tscroll"><table id="wlTable"><thead><tr><th>Symbol</th><th>Price</th><th>Day %</th><th></th></tr></thead><tbody></tbody></table></div>
</div>

<h2>Chart</h2>
<div class="card">
<select id="chartSym"></select>
<span>
<button class="ghost tfbtn" data-tf="1d" onclick="setChartTf('1d')">1D</button>
<button class="ghost tfbtn" data-tf="1m" onclick="setChartTf('1m')">1M</button>
<button class="ghost tfbtn" data-tf="6m" onclick="setChartTf('6m')">6M</button>
<button class="ghost tfbtn active" data-tf="1y" onclick="setChartTf('1y')">1Y</button>
</span>
<div id="chart"></div>
</div>

<h2>Fundamental Scores <span class="muted">(Finnhub, cached 24h — <button class="ghost" onclick="loadScores()">↻ refresh view</button>)</span></h2>
<div class="card">
<div class="tscroll"><table id="scoresTable"><thead><tr><th>Symbol</th><th>Price</th><th>Piotroski (0–9)</th><th>Altman Z</th><th>DCF fair value</th><th>vs price</th><th>Data year</th><th></th></tr></thead><tbody><tr><td colspan="8" class="muted">loading…</td></tr></tbody></table></div>
<p class="muted">Scores use the latest 10-K via Finnhub (free tier). Missing fields → score shown as n/a, never guessed. Refresh forces one Finnhub call per symbol (24h cache otherwise).</p>
</div>

<h2>Experimental Predictions <span class="muted">(NOT advice — experimental signal with its walk-forward hit rate)</span></h2>
<div class="card">
<div class="tscroll"><table id="predTable"><thead><tr><th>Symbol</th><th>P(up, next 5 sessions)</th><th>Walk-forward hit rate</th><th>Trained</th><th></th></tr></thead><tbody><tr><td colspan="5" class="muted">loading…</td></tr></tbody></table></div>
<p class="muted">Model: sklearn HistGradientBoosting on RSI / MACD / Bollinger %B / volume z / ATR% / returns. Train per symbol (needs ~2y daily bars).</p>
</div>

<h2>Backtests <span class="muted">(1y daily bars: +5% target before −3% stop)</span></h2>
<div class="card">
<button onclick="runBacktest()">▶ Run backtest on watchlist</button>
<span id="btMsg" class="muted"></span>
<div class="tscroll"><table id="btTable" style="display:none"><thead><tr><th>Alert type</th><th>Fires</th><th>Target-first rate</th><th>Avg 20d move</th><th>Worst 20d move</th></tr></thead><tbody></tbody></table></div>
</div>

<h2>Alerts</h2>
<div class="card">
<form id="alertForm">
<input type="hidden" id="aId" value="">
<select id="aSymbol" required></select>
<select id="aType" required></select>
<span id="aParams"></span><br>
<label class="muted">timeframe <select id="aTf"><option>1m</option><option>5m</option><option>15m</option><option>1h</option></select></label>
<label class="muted">regime <select id="aRegime"><option value="any">any</option><option value="bull">bull only</option><option value="bear">bear only</option></select></label><br>
<label class="muted"><input type="checkbox" id="aOneTime"> one-time</label>
<label class="muted">cooldown <input id="aCooldown" type="number" value="15" min="1" style="width:60px"> min</label>
<label class="muted">expires <input id="aExpiry" type="datetime-local"></label>
<label class="muted">hysteresis <input id="aHyst" type="number" value="0.5" step="0.1" min="0" style="width:60px"> %</label>
<label class="muted">priority <input id="aPrio" type="number" value="3" min="1" max="5" style="width:50px"></label>
<button type="submit" id="aSubmit">Create alert</button>
<button type="button" class="ghost" id="aCancel" style="display:none">Cancel edit</button>
</form>
</div>
<div class="tscroll"><table id="alTable"><thead><tr><th>ID</th><th>Rule</th><th>State</th><th>One-time</th><th>Cooldown</th><th>Priority</th><th>Actions</th></tr></thead><tbody></tbody></table></div>
<div class="card" id="eventsCard" style="display:none"><h3>Fire history <span id="evTitle"></span></h3><div class="tscroll"><table id="evTable"><thead><tr><th>Time (ET)</th><th>Price</th><th>Detail</th></tr></thead><tbody></tbody></table></div></div>

<script>
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $=id=>document.getElementById(id);
let META={};
const DASH_TOKEN=new URLSearchParams(location.search).get('token')||'';
async function api(m,u,b){const uu=DASH_TOKEN?u+(u.includes('?')?'&':'?')+'token='+encodeURIComponent(DASH_TOKEN):u;const r=await fetch(uu,{method:m,headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});const j=await r.json();if(!r.ok)throw new Error(j.detail||'error');return j;}
async function loadMeta(){META=await api('GET','/api/meta');
 $('aType').innerHTML=META.alert_types.map(t=>`<option>${t}</option>`).join('');
 renderParams();$('aType').onchange=renderParams;}
function renderParams(){const t=$('aType').value,specs=META.param_specs[t]||{};
 $('aParams').innerHTML=Object.entries(specs).map(([f,spec])=>{
  if(spec.select)return `<span class="paramrow"><label class="muted">${spec.label} <select data-p="${f}">${spec.options.map(o=>`<option${o===spec.default?' selected':''}>${o}</option>`).join('')}</select></label></span>`;
  const [lab,dft,step]=spec;
  return `<span class="paramrow"><label class="muted">${lab}<input data-p="${f}" type="number" value="${dft}" step="${step}" required></label></span>`;}).join('');
 const isBar=(META.bar_types||[]).includes(t);
 $('aTf').closest('label').style.display=isBar?'':'none';}
function collectParams(){const o={};document.querySelectorAll('#aParams input,#aParams select').forEach(i=>{o[i.dataset.p]=i.tagName==='SELECT'?i.value:parseFloat(i.value);});return o;}
async function loadWatchlist(){const {symbols}=await api('GET','/api/watchlist');
 const opts=symbols.map(s=>`<option>${s}</option>`).join('');
 $('aSymbol').innerHTML=opts;$('chartSym').innerHTML=opts;
 if(symbols.length&&!chartSym_.cur)selectChart(symbols[0]);}
let chartSym_={cur:null};
async function refreshQuotes(){const q=await api('GET','/api/quotes');
 const tb=$('wlTable').querySelector('tbody');
 tb.innerHTML=Object.keys(q).map(s=>{const v=q[s];if(v.price==null)return `<tr><td class="sym">${s}</td><td colspan=2 class=muted>waiting for data…</td><td><button class=warn onclick="delSym('${s}')">✕</button></td></tr>`;
 const cls=v.day_pct==null?'':(v.day_pct>=0?'up':'down');
 const tag=v.stale?'<span class="tag">close</span>':'';
 return `<tr><td class="sym"><a href="${stockUrl(s)}" style="color:#93c5fd" title="open ${s} detail page">${s}</a></td><td>${fmtP(v.price)}${tag}</td><td class="${cls}">${v.day_pct==null?'—':(v.day_pct>=0?'+':'')+v.day_pct.toFixed(2)+'%'}</td><td><button class=warn onclick="delSym('${s}')">✕</button></td></tr>`;}).join('');}
async function delSym(s){await api('DELETE','/api/watchlist/'+s);loadWatchlist();}
// scanner picks
let SCAN={morning:null,watch:null},scanTab='morning';
function fmtP(p){if(p==null)return '—';if(p>=1)return '$'+p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});if(p>=0.01)return '$'+p.toFixed(4);return '$'+p.toFixed(6);}
function rateCls(r){const v=parseFloat(r);if(isNaN(v))return '';return v>=6?'r-good':v>=4?'r-mid':'r-bad';}
function stockUrl(s){return '/stock?symbol='+encodeURIComponent(s)+(DASH_TOKEN?'&token='+encodeURIComponent(DASH_TOKEN):'');}
function showScan(w){scanTab=w;$('scanMorning').style.display=w==='morning'?'':'none';$('scanWatch').style.display=w==='watch'?'':'none';
 const ts=$('scanTs');
 if(w==='morning'){ts.textContent=SCAN.morning?('scan: '+new Date(SCAN.morning.ts).toLocaleString('en-US',{timeZone:'America/New_York'})):'No scan yet — first scan runs 8:00 AM ET';}
 else{ts.textContent=SCAN.watch?('today: '+SCAN.watch.date):'No watch matches yet today';}}
function fillScan(tid,list,empty){$(tid).querySelector('tbody').innerHTML=(list&&list.length)
 ?list.map((c,i)=>`<tr><td>${i+1}</td><td class="sym"><a href="${stockUrl(c.symbol)}" style="color:#93c5fd">${c.symbol}</a></td><td>${fmtP(c.price)}</td><td class="${rateCls(c.rating)}">${c.rating}</td><td>${esc(c.breakdown)}</td></tr>`).join('')
 :`<tr><td colspan="5" class="muted">${empty}</td></tr>`;}
async function loadScanner(){try{SCAN=await api('GET','/api/scanner');}catch(e){return;}
 $('hdrScan').textContent=SCAN.morning?('last scan: '+new Date(SCAN.morning.ts).toLocaleString('en-US',{timeZone:'America/New_York'})):'no scan yet';
 const m=SCAN.morning;
 if(!m){fillScan('scanBuys',null,'No scan yet — first scan runs 8:00 AM ET');fillScan('scanSells',null,'No scan yet — first scan runs 8:00 AM ET');}
 else{fillScan('scanBuys',m.buys,'no buy candidates above the bar');fillScan('scanSells',m.sells,'no sell candidates above the bar');}
 const rows=(SCAN.watch&&SCAN.watch.entries)||[];
 $('scanWatchTable').querySelector('tbody').innerHTML=rows.length
 ?rows.map(e=>`<tr><td>${e.time?new Date(e.time).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}):'—'}</td><td class="sym"><a href="${stockUrl(e.symbol)}" style="color:#93c5fd">${e.symbol}</a></td><td class="${e.side==='BUY'?'up':'down'}">${e.side}</td><td>${fmtP(e.price)}</td><td class="${rateCls(e.rating)}">${e.rating}</td><td>${esc(e.breakdown)}</td></tr>`).join('')
 :'<tr><td colspan="6" class="muted">no watch matches yet today</td></tr>';
 showScan(scanTab);}
$('addSym').onsubmit=async e=>{e.preventDefault();const s=$('symInput').value.trim().toUpperCase();if(!s)return;
 try{await api('POST','/api/watchlist',{symbol:s});$('symInput').value='';$('wlMsg').textContent='';loadWatchlist();}
 catch(err){$('wlMsg').textContent=err.message;}};
// chart
let chart=null,series=null,chartTf='1y';
function selectChart(s){chartSym_.cur=s;$('chartSym').value=s;drawChart();}
$('chartSym').onchange=e=>selectChart(e.target.value);
function setChartTf(tf){chartTf=tf;document.querySelectorAll('.tfbtn').forEach(b=>b.classList.toggle('active',b.dataset.tf===tf));drawChart();}
async function drawChart(){if(!chartSym_.cur)return;const {bars}=await api('GET','/api/candles?symbol='+chartSym_.cur+'&limit=200&tf='+chartTf);
 if(!chart){chart=LightweightCharts.createChart($('chart'),{layout:{background:{color:'#12141b'},textColor:'#d1d5db'},grid:{vertLines:{color:'#222'},horzLines:{color:'#222'}},timeScale:{timeVisible:true}});series=chart.addCandlestickSeries();}
 series.setData(bars);if(chartTf==='1d'){chart.timeScale().scrollToRealTime();}else{chart.timeScale().fitContent();}}
// alerts
function linkDesc(a){const d=a.description||'',s=a.symbol||'';
 if(s&&d.startsWith(s+' '))return `<a href="${stockUrl(s)}" style="color:#93c5fd">${s}</a>${d.slice(s.length)}`;
 return d;}
async function loadAlerts(){const list=await api('GET','/api/alerts');
 $('alTable').querySelector('tbody').innerHTML=list.map(a=>`<tr><td>${a.id}</td><td>${linkDesc(a)}</td><td>${a.paused?'⏸ paused':a.state}</td><td>${a.one_time?'yes':'no'}</td><td>${a.cooldown_min}m</td><td>${a.ntfy_priority}</td>
 <td><button class=ghost onclick="showEvents(${a.id},'${a.description.replace(/'/g,"")}')">history</button>
 <button onclick="togglePause(${a.id},${a.paused})">${a.paused?'resume':'pause'}</button>
 <button class=ghost onclick="editAlert(${a.id})">edit</button>
 <button class=warn onclick="delAlert(${a.id})">✕</button></td></tr>`).join('');}
async function togglePause(id,paused){await api('PATCH','/api/alerts/'+id,{paused:!paused});loadAlerts();}
async function delAlert(id){if(confirm('Delete alert '+id+'?')){await api('DELETE','/api/alerts/'+id);loadAlerts();}}
async function editAlert(id){const list=await api('GET','/api/alerts');const a=list.find(x=>x.id===id);if(!a)return;
 $('aId').value=a.id;$('aSymbol').value=a.symbol;$('aType').value=a.type;renderParams();
 Object.entries(a.params).forEach(([k,v])=>{const i=document.querySelector(`#aParams [data-p="${k}"]`);if(i)i.value=v;});
 $('aTf').value=a.timeframe||'1m';$('aRegime').value=a.regime||'any';
 $('aOneTime').checked=a.one_time;$('aCooldown').value=a.cooldown_min;
 $('aExpiry').value=a.expires_at?a.expires_at.slice(0,16):'';$('aHyst').value=(a.hysteresis_pct*100);
 $('aPrio').value=a.ntfy_priority;$('aSubmit').textContent='Save changes';$('aCancel').style.display='';window.scrollTo(0,document.body.scrollHeight);}
$('aCancel').onclick=()=>{$('alertForm').reset();$('aId').value='';$('aSubmit').textContent='Create alert';$('aCancel').style.display='none';renderParams();};
$('alertForm').onsubmit=async e=>{e.preventDefault();
 const body={symbol:$('aSymbol').value,type:$('aType').value,params:collectParams(),one_time:$('aOneTime').checked,
  timeframe:$('aTf').value,regime:$('aRegime').value,
  cooldown_min:parseFloat($('aCooldown').value)||15,expires_at:$('aExpiry').value||null,
  hysteresis_pct:(parseFloat($('aHyst').value)||0.5)/100,ntfy_priority:parseInt($('aPrio').value)||3};
 try{if($('aId').value){await api('PATCH','/api/alerts/'+$('aId').value,body);}else{await api('POST','/api/alerts',body);}
  $('aCancel').onclick();loadAlerts();}catch(err){alert(err.message);}};
async function showEvents(id,desc){const ev=await api('GET','/api/alerts/'+id+'/events');
 $('eventsCard').style.display='';$('evTitle').textContent='— '+desc;
 $('evTable').querySelector('tbody').innerHTML=ev.map(e=>`<tr><td>${new Date(e.ts).toLocaleString('en-US',{timeZone:'America/New_York'})}</td><td>$${e.price.toFixed(2)}</td><td>${esc(e.detail)}</td></tr>`).join('')||'<tr><td colspan=3 class=muted>no fires yet</td></tr>';}
(async function init(){await loadMeta();await loadWatchlist();await loadAlerts();loadScanner();loadRegime();loadScores();loadPredictions();loadBacktest();setInterval(refreshQuotes,2000);setInterval(loadScanner,60000);refreshQuotes();})();
// regime badge
async function loadRegime(){try{const r=await api('GET','/api/regime');
 if(!r||!r.regime){$('regimeBadge').textContent='regime: n/a';return;}
 const em=r.regime==='bull'?'🟢':r.regime==='bear'?'🔴':'🟡';
 $('regimeBadge').textContent=`${em} regime: ${r.regime} (SPY vs SMA200)`;}catch(e){$('regimeBadge').textContent='regime: n/a';}}
// fundamental scores
async function loadScores(){try{const s=await api('GET','/api/scores');
 const tb=$('scoresTable').querySelector('tbody');
 tb.innerHTML=Object.keys(s).map(sym=>{const v=s[sym];
  if(v.error)return `<tr><td class="sym">${sym}</td><td colspan="7" class="muted">${v.error}</td></tr>`;
  const p=v.piotroski,pden=(p&&p.evaluable!=null&&p.evaluable<p.of)?p.evaluable:(p?p.of:null);
  const pc=(p&&p.score!=null)?`${p.score}/${pden}`:'n/a';
  const az=v.altman,z=(az&&az.z!=null)?`${az.z.toFixed(2)} (${az.zone})`:'n/a';
  const dcf=v.dcf,fv=(dcf&&dcf.fair_value!=null)?`$${dcf.fair_value.toFixed(2)}`:'n/a';
  const vs=(dcf&&dcf.fair_value!=null&&v.price)?(((dcf.fair_value-v.price)/v.price*100).toFixed(1)+'%'):'—';
  return `<tr><td class="sym">${sym}</td><td>${v.price!=null?'$'+v.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'—'}</td><td>${pc}</td><td>${z}</td><td>${fv}</td><td>${vs}</td><td>${v.data_year||'—'}</td><td><button class="ghost" onclick="refreshScore('${sym}')">↻</button></td></tr>`;}).join('')||'<tr><td colspan="8" class="muted">watchlist empty</td></tr>';}catch(e){$('scoresTable').querySelector('tbody').innerHTML='<tr><td colspan="8" class="muted">scores unavailable</td></tr>';}}
async function refreshScore(sym){await api('POST','/api/scores/'+sym+'/refresh');loadScores();}
// experimental predictions
async function loadPredictions(){const {symbols}=await api('GET','/api/watchlist');
 const tb=$('predTable').querySelector('tbody');
 const rows=await Promise.all(symbols.map(async sym=>{try{const p=await api('GET','/api/predict/'+sym);
  const hr=p.walk_forward.hit_rate;
  return `<tr><td class="sym">${sym}</td><td>${p.latest_proba_up==null?'n/a':(p.latest_proba_up*100).toFixed(1)+'%'}</td><td>${hr!=null?(hr*100).toFixed(1)+'% ('+p.walk_forward.tested+' tests)':'n/a'}</td><td>${p.trained_at.slice(0,10)}</td><td><button class="ghost" onclick="trainPredict('${sym}')">retrain</button></td></tr>`;}
  catch(e){return `<tr><td class="sym">${sym}</td><td colspan="3" class="muted">no model — train one</td><td><button onclick="trainPredict('${sym}')">train</button></td></tr>`;}}));
 tb.innerHTML=rows.join('')||'<tr><td colspan="5" class="muted">watchlist empty</td></tr>';}
async function trainPredict(sym){if(!confirm('Train experimental model for '+sym+'? (uses ~2y daily bars)'))return;
 await api('POST','/api/predict/'+sym+'/train');loadPredictions();}
// backtests
async function loadBacktest(){try{const r=await api('GET','/api/backtest');if(r.ran_at)renderBacktest(r);}catch(e){}}
async function runBacktest(){$('btMsg').textContent='running… (1y daily bars per symbol)';
 try{const r=await api('POST','/api/backtest');renderBacktest(r);$('btMsg').textContent='ran '+r.ran_at;}
 catch(e){$('btMsg').textContent='failed: '+e.message;}}
function renderBacktest(r){const tb=$('btTable').querySelector('tbody');$('btTable').style.display='';
 const fmt=v=>v==null?'n/a':(v*100).toFixed(1)+'%';
 tb.innerHTML=Object.keys(r.results).map(t=>{const s=r.results[t];
  return `<tr><td>${t}</td><td>${s.fires}</td><td>${fmt(s.target_first_rate)}</td><td>${s.avg_move_pct==null?'n/a':s.avg_move_pct.toFixed(2)+'%'}</td><td>${s.worst_move_pct==null?'n/a':s.worst_move_pct.toFixed(2)+'%'}</td></tr>`;}).join('');}
</script>
</body></html>
"""


STOCK_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title id="pgTitle">Stock — Stockbot</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;max-width:1100px;margin:0 auto;padding:16px;background:#0b0d12;color:#e5e7eb;line-height:1.45;-webkit-text-size-adjust:100%}
h1{font-size:1.5em;margin:0}
h2{font-size:1.08em;margin:30px 0 6px;color:#f3f4f6;border-bottom:1px solid #262a33;padding-bottom:6px}
h3{font-size:1em;margin:16px 0 2px}
header.topbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;justify-content:space-between;background:#12141b;border:1px solid #262a33;border-radius:10px;padding:12px 16px}
header.topbar .meta{text-align:right}
header.topbar .big{font-size:1.5em;font-weight:700}
.card{background:#12141b;border:1px solid #262a33;border-radius:10px;padding:14px 16px;margin-top:10px;box-shadow:0 1px 3px rgba(0,0,0,.35)}
table{border-collapse:collapse;width:100%;margin-top:8px;font-size:.92em}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #23262e;vertical-align:top}
thead th{background:#1b1f27;color:#cbd5e1;font-weight:600;white-space:nowrap}
tbody tr:nth-child(even){background:rgba(255,255,255,.025)}
tbody tr:hover{background:#1d232e}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.sym{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:600;letter-spacing:.02em}
.r-good{color:#4ade80;font-weight:700}.r-mid{color:#fbbf24;font-weight:700}.r-bad{color:#f87171;font-weight:700}
.up{color:#4ade80}.down{color:#f87171}
.tag{display:inline-block;font-size:.7em;font-weight:600;background:#374151;color:#d1d5db;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;text-transform:uppercase;letter-spacing:.05em}
.badge{display:inline-block;font-weight:700;border-radius:6px;padding:3px 10px;font-size:1em}
.badge.buy{background:#14532d;color:#bbf7d0}.badge.sell{background:#7f1d1d;color:#fecaca}
button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:5px 11px;cursor:pointer;margin:1px;font-size:.92em}
button.ghost{background:#374151}
button.tfbtn.active{background:#2563eb}
#chart{height:380px;margin-top:10px}.muted{color:#9ca3af;font-size:.85em}
ul.reasons{margin:6px 0 0;padding-left:18px}
a{color:#93c5fd}
@media (max-width:640px){body{padding:10px}.card{padding:10px 12px}#chart{height:300px}header.topbar .meta{text-align:left}}
</style>
</head>
<body>
<p><a href="/" id="backLink">← dashboard</a></p>
<header class="topbar">
<div>
<h1><span class="sym" id="hSym">—</span></h1>
<div class="muted" id="hSub">loading…</div>
</div>
<div class="meta">
<div class="big" id="hPrice">—</div>
<div id="hDay" class="muted">—</div>
</div>
</header>
<div class="muted" id="hMeta" style="margin-top:6px"></div>

<h2>Chart</h2>
<div class="card">
<span>
<button class="ghost tfbtn" data-tf="1d" onclick="setChartTf('1d')">1D</button>
<button class="ghost tfbtn" data-tf="1m" onclick="setChartTf('1m')">1M</button>
<button class="ghost tfbtn" data-tf="6m" onclick="setChartTf('6m')">6M</button>
<button class="ghost tfbtn active" data-tf="1y" onclick="setChartTf('1y')">1Y</button>
</span>
<div id="chart"></div>
</div>

<h2>Levels <span class="muted">(computed from daily bars only — not financial advice)</span></h2>
<div class="card">
<div class="tscroll"><table id="levelsTable"><thead><tr><th>Level</th><th>Value</th><th>Basis</th><th>Distance to level</th></tr></thead><tbody><tr><td colspan="4" class="muted">loading…</td></tr></tbody></table></div>
</div>

<h2>Scanner Verdict <span class="muted">(technical screen + fundamentals overlay + sentiment)</span></h2>
<div class="card" id="verdictCard"><span class="muted">loading…</span></div>

<h2>Fundamental Scores <span class="muted">(Finnhub, cached 24h)</span></h2>
<div class="card">
<div class="tscroll"><table id="fundTable"><thead><tr><th>Price</th><th>Piotroski (0–9)</th><th>Altman Z</th><th>DCF fair value</th><th>vs price</th><th>Data year</th></tr></thead><tbody><tr><td colspan="6" class="muted">loading…</td></tr></tbody></table></div>
<p class="muted" id="fundNotes"></p>
</div>

<h2>Sentiment <span class="muted">(financial-press news tone + analyst ratings, cached 24h)</span></h2>
<div class="card" id="sentCard"><span class="muted">loading…</span></div>

<script>
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $=id=>document.getElementById(id);
const QS=new URLSearchParams(location.search);
const DASH_TOKEN=QS.get('token')||'';
const SYM=(QS.get('symbol')||'').toUpperCase();
async function api(m,u,b){const uu=DASH_TOKEN?u+(u.includes('?')?'&':'?')+'token='+encodeURIComponent(DASH_TOKEN):u;const r=await fetch(uu,{method:m,headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});const j=await r.json();if(!r.ok)throw new Error(j.detail||'error');return j;}
function fmtP(p){if(p==null)return '—';if(p>=1)return '$'+p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});if(p>=0.01)return '$'+p.toFixed(4);return '$'+p.toFixed(6);}
function fmtCap(musd){if(musd==null)return '—';if(musd>=1000)return '$'+(musd/1000).toLocaleString('en-US',{maximumFractionDigits:2})+'B';return '$'+musd.toLocaleString('en-US',{maximumFractionDigits:1})+'M';}
function rateCls(r){const v=parseFloat(r);if(isNaN(v))return '';return v>=6?'r-good':v>=4?'r-mid':'r-bad';}
$('backLink').href='/'+(DASH_TOKEN?'?token='+encodeURIComponent(DASH_TOKEN):'');
let chart=null,series=null,chartTf='1y';
function setChartTf(tf){chartTf=tf;document.querySelectorAll('.tfbtn').forEach(b=>b.classList.toggle('active',b.dataset.tf===tf));drawChart();}
async function drawChart(){if(!SYM)return;try{const {bars}=await api('GET','/api/candles?symbol='+encodeURIComponent(SYM)+'&limit=200&tf='+chartTf);
 if(!chart){chart=LightweightCharts.createChart($('chart'),{layout:{background:{color:'#12141b'},textColor:'#d1d5db'},grid:{vertLines:{color:'#222'},horzLines:{color:'#222'}},timeScale:{timeVisible:true}});series=chart.addCandlestickSeries();}
 series.setData(bars);if(chartTf==='1d'){chart.timeScale().scrollToRealTime();}else{chart.timeScale().fitContent();}}catch(e){}}
function lvlRow(name,lvlObj,last,extra){const obj=lvlObj||{};const v=obj.level!=null?obj.level:(typeof lvlObj==='number'?lvlObj:null);
 const basis=extra||obj.kind||'';
 const vs=(v==null||last==null||!v)?'—':(((last-v)/v*100)>=0?'+':'')+(((last-v)/v)*100).toFixed(1)+'%';
 return `<tr><td>${name}</td><td>${v==null?'—':fmtP(v)}</td><td class="muted">${basis}</td><td>${vs}</td></tr>`;}
function lvlNum(name,v,kind,last){return lvlRow(name,{level:v,kind:kind},last);}
function pctBText(v){if(v==null)return '—';const s=v.toFixed(2);if(v>=1)return s+' — above the upper band (breakout territory)';if(v<=0)return s+' — below the lower band (capitulation/freefall)';if(v>=0.45&&v<=0.55)return s+' — on the 20-day average';if(v<0.25)return s+' — hugging the lower band';return s+' — inside the bands';}
function bwText(c){if(!c)return '—';if(c==='coiled')return 'coiled — at the tight end of its last 126 sessions';if(c==='wide')return 'wide — at the stretched end of its last 126 sessions';return 'normal — mid-range over its last 126 sessions';}
async function load(){
 document.title=SYM?SYM+' — Stockbot':'Stock — Stockbot';
 if(!SYM){$('hSub').textContent='no symbol given';return;}
 let d;
 try{d=await api('GET','/api/stock/'+encodeURIComponent(SYM));}
 catch(e){$('hSub').textContent='could not load: '+e.message;return;}
 const p=d.price,co=d.company||{};
 $('hSym').textContent=SYM+(co.name?' — '+co.name:'');
 const ctx=[co.sector,co.industry,co.exchange].filter(Boolean).join(' · ');
 $('hSub').textContent=ctx||'market context unavailable';
 $('hPrice').textContent=fmtP(p.last)+(p.live?'':'<span class="tag">close</span>');
 const dp=p.day_pct;
 $('hDay').innerHTML=dp==null?'—':`<span class="${dp>=0?'up':'down'}">${dp>=0?'+':''}${dp.toFixed(2)}% today</span>`;
 const st=d.stats;
 $('hMeta').textContent=`market cap ${fmtCap(co.market_cap_musd)} · 52w ${fmtP(st.low_52w)} – ${fmtP(st.high_52w)} (${st.pct_of_52w_range==null?'—':st.pct_of_52w_range.toFixed(0)+'% of range'}) · 20d avg $ volume (IEX) ${st.avg_dollar_volume_20d==null?'—':'$'+Math.round(st.avg_dollar_volume_20d).toLocaleString('en-US')} · as of ${new Date(st.as_of*1000).toLocaleDateString('en-US',{timeZone:'America/New_York'})}`;
 const L=d.levels,last=p.last;
 let rows='';
 if(L.rows&&L.rows.length){rows=L.rows.map(r=>`<tr><td>${r.name}</td><td>${r.value==null?'—':fmtP(r.value)}</td><td class="muted">${r.basis||''}</td><td>${r.distance_text||'—'}</td></tr>`).join('');}
 else{rows+=lvlRow('Support',L.support,last);
 rows+=lvlRow('Resistance',L.resistance,last);
 rows+=lvlRow('Major resistance (52w high)',L.major_resistance,last);
 rows+=lvlRow('Breakout level',L.breakout,last);
 rows+=lvlRow('Breakdown point',L.breakdown,last);
 rows+=lvlNum('SMA 50',L.sma50,'50-session trend zone',last);
 rows+=lvlNum('SMA 200',L.sma200,'200-session trend zone',last);}
 rows+=`<tr><td>RSI (14)</td><td>${L.rsi14==null?'—':L.rsi14.toFixed(1)}</td><td class="muted">oversold &lt; 30 · overbought &gt; 70</td><td>—</td></tr>`;
 rows+=`<tr><td>Volume ratio</td><td>${L.vol_ratio==null?'—':L.vol_ratio.toFixed(2)+'x'}${L.vol_ratio_partial?'<span class="tag">early session</span>':''}</td><td class="muted">last volume vs 20-session median${L.vol_ratio_partial?' — partial day so far, not a full session':''}</td><td>—</td></tr>`;
 rows+=`<tr><td>%B</td><td>${L.pct_b==null?'—':L.pct_b.toFixed(2)}</td><td class="muted">${pctBText(L.pct_b)}</td><td>—</td></tr>`;
 rows+=`<tr><td>Bandwidth</td><td>${L.bandwidth==null?'—':L.bandwidth.toFixed(3)}</td><td class="muted">${bwText(L.bandwidth_context)}</td><td>—</td></tr>`;
 $('levelsTable').querySelector('tbody').innerHTML=rows;
 if(L.buy_zone&&L.buy_zone.note){$('levelsTable').insertAdjacentHTML('afterend',`<p class="muted">Breakout level: ${L.buy_zone.note}${L.buy_zone.distance_text?` (the level sits ${L.buy_zone.distance_text} the current price)`:''}</p>`);}
 // scanner verdict
 const v=d.scanner;
 if(!v){$('verdictCard').innerHTML='<span class="muted">not enough daily history for the technical screen</span>';}
 else{
  const sideCls=v.side==='BUY'?'buy':'sell';
  const setups=v.setups&&v.setups.length?`<div class="tscroll"><table><thead><tr><th>Setup</th><th>Pts</th><th>Why</th></tr></thead><tbody>${v.setups.map(s=>`<tr><td>${s.name}</td><td>${s.points}</td><td>${esc(s.reason)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">no setups fired on the winning side</p>';
  const counter=v.counter_setups&&v.counter_setups.length?`<p class="muted">Counter-signals: ${v.counter_setups.map(s=>`${s.name} (+${s.points})`).join(' · ')}</p>`:'';
  const reasons=v.reasons&&v.reasons.length?`<p class="muted" style="margin:8px 0 0">Fundamental overlay &amp; notes:</p><ul class="reasons">${v.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:'';
  $('verdictCard').innerHTML=`<span class="badge ${sideCls}">${v.side}</span> <b>composite ${v.composite}</b> · <span class="${rateCls(v.rating)}">${v.rating}</span> <span class="muted">(bull ${v.tech_score} vs bear ${v.bear_score}${v.playbook?' · '+esc(v.playbook)+' playbook':''}${v.trend?' · '+esc(v.trend)+' trend':''}${v.push_bar!=null?' · push bar '+v.push_bar:''})</span>${setups}${reasons}${counter}`;
 }
 // fundamentals
 const f=d.fundamentals||{};
 if(f.error){$('fundTable').querySelector('tbody').innerHTML=`<tr><td colspan="6" class="muted">${esc(f.error)}</td></tr>`;}
 else{
  const fp=f.piotroski,denom=(fp&&(fp.evaluable!=null&&fp.evaluable<fp.of))?fp.evaluable:(fp?fp.of:null);
  const pc=fp?(fp.score==null?'n/a':`${fp.score}/${denom}`):'n/a';
  const az=(f.altman&&f.altman.z!=null)?`${f.altman.z.toFixed(2)} (${f.altman.zone}${f.altman.model?', '+f.altman.model:''})`:'n/a';
  const fv=(f.dcf&&f.dcf.fair_value!=null)?fmtP(f.dcf.fair_value):'n/a';
  const vs=(f.dcf&&f.dcf.fair_value!=null&&p.last)?(((f.dcf.fair_value-p.last)/p.last*100).toFixed(1)+'%'):'—';
  $('fundTable').querySelector('tbody').innerHTML=`<tr><td>${fmtP(p.last)}</td><td>${pc}</td><td>${az}</td><td>${fv}</td><td>${vs}</td><td>${f.data_year||'—'}</td></tr>`;
  $('fundNotes').textContent=(f.notes&&f.notes.length)?f.notes.join(' · '):'';
 }
 // sentiment
 const s=d.sentiment||{};
 const ac=s.analyst_counts||{};
 const buys=(ac.buy||0)+(ac.strongBuy||0),sells=(ac.sell||0)+(ac.strongSell||0);
 $('sentCard').innerHTML=`<div>News tone: <b>${s.label||'neutral'}</b> <span class="muted">(score ${s.news!=null?(s.news>=0?'+':'')+s.news.toFixed(2):'—'} from ${s.n||0} recent headlines)</span></div>
 <div style="margin-top:4px">Analysts: <b>${buys} buy / ${ac.hold||0} hold / ${sells} sell</b> <span class="muted">(${ac.total||0} analysts${ac.period?', period '+ac.period:''})</span></div>
 <div class="muted" style="margin-top:4px">Combined sentiment: ${s.sentiment!=null?(s.sentiment>=0?'+':'')+s.sentiment.toFixed(2):'—'} (−1…+1)</div>`;
 drawChart();
}
load();
</script>
</body></html>
"""

