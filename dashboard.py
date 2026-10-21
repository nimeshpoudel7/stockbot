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

import chart_analysis
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import indicators as ind
from alerts import BAR_TYPES, EVENT_TYPES, TICK_TYPES, describe
from scanner import (SCAN_MORNING_KEY, SCAN_TOP10_KEY, SCAN_WATCH_KEY,
                     SCAN_WATCH_LAST_KEY)

import fundamentals
import scanner as scanner_mod
import scorecard
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
SNAP_TTL_SECS = 10               # live snapshot cache (prices, today's bar)
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

        # PWA install assets carry no data and no secrets: the manifest
        # deliberately has no start_url (it defaults to the page the user
        # installs from, token included) and the icons are public artwork.
        public_paths = {"/manifest.webmanifest", "/icon-180.png",
                        "/icon-192.png", "/icon-512.png", "/favicon.ico"}

        @app.middleware("http")
        async def token_gate(request: Request, call_next):
            if request.url.path in public_paths:
                return await call_next(request)
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

    # ---- PWA install assets (public: no data, no secrets) ---------------
    @app.get("/manifest.webmanifest")
    def pwa_manifest():
        return FileResponse(os.path.join(_WEB, "manifest.webmanifest"),
                            media_type="application/manifest+json")

    @app.get("/icon-180.png")
    def pwa_icon_180():
        return FileResponse(os.path.join(_WEB, "icon-180.png"),
                            media_type="image/png")

    @app.get("/icon-192.png")
    def pwa_icon_192():
        return FileResponse(os.path.join(_WEB, "icon-192.png"),
                            media_type="image/png")

    @app.get("/icon-512.png")
    def pwa_icon_512():
        return FileResponse(os.path.join(_WEB, "icon-512.png"),
                            media_type="image/png")

    @app.get("/favicon.ico")
    def pwa_favicon():
        return FileResponse(os.path.join(_WEB, "icon-192.png"),
                            media_type="image/png")

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

    # ---- live snapshots for ANY symbol (Alpaca, IEX) ----------------------
    _snap_cache: dict[str, tuple[float, dict | None]] = {}

    def _snapshots(symbols) -> dict[str, dict]:
        """{SYM: snapshot} from a 10-second cache; one REST call for all
        stale symbols. Empty when the 'snapshots' extra is missing or the
        call fails (callers fall back to cached closes)."""
        fn = extras.get("snapshots")
        syms = [x.upper() for x in symbols if x and x.isalpha()][:300]
        now = time.time()
        need = [x for x in syms
                if x not in _snap_cache or now - _snap_cache[x][0] > SNAP_TTL_SECS]
        if need and fn:
            try:
                got = fn(need) or {}
            except Exception:  # noqa: BLE001 - live price is best-effort
                got = {}
            for x in need:
                _snap_cache[x] = (now, got.get(x))
        return {x: _snap_cache[x][1] for x in syms
                if x in _snap_cache and _snap_cache[x][1]}

    def _live_daily(symbol: str):
        """Cached daily bars with TODAY's bar replaced/appended from the live
        snapshot, so indicators and patterns move with the price."""
        bars, ok = _daily_bars_cached(symbol)
        snap = _snapshots([symbol]).get(symbol.upper())
        if bars and snap and snap.get("bar") and snap["bar"].get("o"):
            nb = dict(snap["bar"])
            d_new = datetime.fromtimestamp(nb["ts"], timezone.utc).date()
            d_last = datetime.fromtimestamp(bars[-1]["ts"], timezone.utc).date()
            if d_new == d_last:
                bars = bars[:-1] + [dict(nb, ts=bars[-1]["ts"])]
            elif d_new > d_last:
                bars = bars + [nb]
        return bars, ok, snap

    _intra_cache: dict[str, tuple[float, list]] = {}

    def _minute_bars(symbol: str) -> list[dict]:
        """Recent 1-minute bars: the live engine's for watchlist names,
        else one REST fetch cached for 60s."""
        symbol = symbol.upper()
        bars = store.candles_load(symbol, "1m", 500)
        st = market.get(symbol)
        if st is not None and st.forming:
            bars = bars + [st.forming]
        if not bars and extras.get("intraday_bars"):
            hit = _intra_cache.get(symbol)
            if hit and time.time() - hit[0] < 60:
                bars = hit[1]
            else:
                try:
                    bars = extras["intraday_bars"](symbol) or []
                except Exception:  # noqa: BLE001
                    bars = []
                _intra_cache[symbol] = (time.time(), bars)
        return bars

    def _intraday(symbol: str) -> list[dict]:
        """Today's (ET) 1-minute bars only."""
        bars = _minute_bars(symbol)
        if not bars:
            return []
        today = datetime.now(ET_ZONE).date()
        return [b for b in bars
                if datetime.fromtimestamp(b["ts"], ET_ZONE).date() == today]

    def _scan_ref(symbol: str) -> dict | None:
        """The symbol's entry in today's scanner output (latest watch match,
        else the morning list) — what the Today page showed."""
        symbol = symbol.upper()
        for key, kind in ((SCAN_WATCH_KEY, "watch"), (SCAN_MORNING_KEY, "morning")):
            raw = store.poller_get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            rows = (data.get("entries") or [])[::-1] if kind == "watch" else \
                (data.get("buys") or []) + (data.get("sells") or [])
            for r in rows:
                if r.get("symbol") == symbol:
                    return {"kind": kind, "ts": r.get("time") or data.get("ts"),
                            "side": r.get("side"), "rating": r.get("rating"),
                            "score": r.get("tech_score", r.get("score")),
                            "price": r.get("price"), "rsi": r.get("rsi"),
                            "playbook": r.get("playbook"),
                            "setups": r.get("setups") or []}
        return None

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
        wl = store.watchlist_all()
        # symbols without a fresh websocket tick (IEX can be sparse on
        # quieter names) get a live REST snapshot instead
        quiet = [x for x in wl if market.get(x) is None or market[x].price is None
                 or (market[x].ts or 0) < time.time() - 60]
        snaps = _snapshots(quiet) if quiet else {}
        for symbol in wl:
            st = market.get(symbol)
            price = st.price if st is not None else None
            sn = snaps.get(symbol)
            if sn and (price is None or (st.ts or 0) < sn.get("ts", 0)):
                out[symbol] = {"price": sn["price"], "day_pct": sn["day_pct"],
                               "ts": sn.get("ts"), "stale": False}
                continue
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

    @app.get("/api/prices")
    def api_prices(symbols: str = ""):
        """Live price + day % for any symbols (comma-separated, max 300):
        websocket ticks for watchlist names, Alpaca snapshots otherwise."""
        syms = [x.strip().upper() for x in symbols.split(",") if x.strip()]
        syms = [x for x in syms if x.isalpha() and len(x) <= 6][:300]
        out = {}
        fresh_tick = {}
        for x in syms:
            st = market.get(x)
            if st is not None and st.price is not None and (st.ts or 0) > time.time() - 60:
                fresh_tick[x] = {"price": st.price, "ts": st.ts, "live": True,
                                 "day_pct": ind.day_pct_change(st.price, st.prev_close or 0)}
        snaps = _snapshots([x for x in syms if x not in fresh_tick])
        for x in syms:
            if x in fresh_tick:
                out[x] = fresh_tick[x]
            elif x in snaps:
                sn = snaps[x]
                out[x] = {"price": sn["price"], "day_pct": sn["day_pct"],
                          "ts": sn.get("ts"), "live": True}
        return out

    @app.get("/api/candles")
    def api_candles(symbol: str, limit: int = 200, tf: str = "1d"):
        symbol = symbol.upper()
        if tf in CHART_TF_DAILY:
            bars, _ok, _snap = _live_daily(symbol)   # today's bar is live
            if not bars:
                _need("daily_bars")  # 503 when the extra is not configured
            bars = bars[-CHART_TF_DAILY[tf]:]
        elif tf == "1d":
            bars = _minute_bars(symbol)[-min(limit, 500):]
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

        def _scorecard():
            try:
                return scorecard.summary(store)
            except Exception:  # noqa: BLE001 - never breaks the page
                return None

        return {"morning": _poller(SCAN_MORNING_KEY),
                "watch": _poller(SCAN_WATCH_KEY),
                "watch_last": _poller(SCAN_WATCH_LAST_KEY),
                "top10": _poller(SCAN_TOP10_KEY),
                "scorecard": _scorecard()}


    # ---- moderate-risk strategy (separate page/tab) ------------------------
    @app.get("/api/moderate")
    def api_moderate():
        import moderate
        raw = store.poller_get(moderate.LIST_KEY)
        try:
            data = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            data = None
        try:
            card = moderate.summary(store)
        except Exception:  # noqa: BLE001
            card = None
        return {"list": data, "scorecard": card, "backtest": moderate.BACKTEST}

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

    @app.get("/api/analysis/{symbol}")
    def api_analysis(symbol: str):
        """Chart reading (chart_analysis.analyze) on the cached daily bars,
        plus today's 1-minute bars for watchlist symbols (VWAP, opening
        range). Descriptive only — not part of the scanner score."""
        symbol = symbol.upper()
        if not symbol.isalpha() or not 1 <= len(symbol) <= 6:
            raise HTTPException(400, "invalid symbol")
        bars, _ok, snap = _live_daily(symbol)
        if not bars:
            raise HTTPException(404, f"no daily data for {symbol}")
        spy = [r["c"] for r in store.candles_load("SPY", "1d", 400)] or None
        intraday = _intraday(symbol)
        rep = chart_analysis.analyze(bars, spy_closes=spy,
                                     intraday=intraday or None,
                                     session_vwap=(snap or {}).get("vwap"))
        if rep is None:
            raise HTTPException(404, f"not enough history for {symbol} "
                                     "(needs 60 daily bars)")
        return {"symbol": symbol, "updated": time.time(),
                "live": bool(snap), **rep}

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
        bars, _ok, snap = _live_daily(symbol)
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
        if live_price is None and snap:
            price_last, is_live = snap["price"], True
            day_pct = snap["day_pct"]
        elif live_price is not None:
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
        vol_ratio_paced = None
        if vol_ratio is not None:
            now_et = datetime.now(ET_ZONE)
            last_bar_et = datetime.fromtimestamp(bars[-1]["ts"], ET_ZONE)
            vol_ratio_partial = (last_bar_et.date() == now_et.date()
                                 and now_et.time() < MARKET_CLOSE_ET)
            # Paced ratio: today's partial volume vs what a normal day has
            # usually traded BY THIS TIME (U-shaped intraday volume curve),
            # so 0.63x at noon reads as ~1.7x the normal pace, not "light".
            mins = chart_analysis.session_minutes(now_et)
            if vol_ratio_partial and mins is not None:
                vol_ratio_paced = chart_analysis.paced_volume_ratio(
                    vols[-1], statistics.median(vols[-21:-1]), mins)
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
            "vol_ratio_paced": vol_ratio_paced,
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
                "stop": ((res.get("levels") or {}).get(side) or (None, None))[0],
                "target": ((res.get("levels") or {}).get(side) or (None, None))[1],
                "trend": res.get("trend"),
                "push_bar": scanner_mod.push_bar(side, None,
                                                 res.get("playbook")),
                "rsi": (res.get("indicators") or {}).get("rsi"),
                "price": last,
                "partial_today": bool(snap and snap.get("bar")),
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
            "scan_ref": _scan_ref(symbol),
            "updated": time.time(),
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


# ---- front-end: web/*.html with shared CSS/JS inlined (one request per page,
# so the ?token= gate never has to cover separate asset URLs) ---------------
_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def _read_web(name: str) -> str:
    with open(os.path.join(_WEB, name), encoding="utf-8") as f:
        return f.read()


def _page(name: str) -> str:
    return (_read_web(name)
            .replace("{{BASE_CSS}}", _read_web("base.css"))
            .replace("{{COMMON_JS}}", _read_web("common.js")))


HTML_PAGE = _page("index.html")
STOCK_PAGE = _page("stock.html") if os.path.exists(
    os.path.join(_WEB, "stock.html")) else ""


