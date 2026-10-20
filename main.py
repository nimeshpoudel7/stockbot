"""Stockbot entry point: feed + alert engine + ntfy pushes + web dashboard.

Run:  python main.py          (needs .env with ALPACA_KEY, ALPACA_SECRET, NTFY_TOPIC)
"""
from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from alerts import AlertEngine, MarketState
from config import MAX_SYMBOLS, WARMUP_1M_BARS, Settings
from dashboard import create_app
from feed import AlpacaFeed, FeedManager, FinnhubFeed, Tick
from notify import send_ntfy
from storage import Store

import event_alerts
import fundamentals
import predict as predict_mod
import backtest as backtest_mod
import regime as regime_mod
import sessions as sessions_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger("stockbot")


async def _warm_one(manager: FeedManager, store: Store,
                    market: dict[str, MarketState], symbol: str) -> None:
    """Fetch history, seed market state + candle cache."""
    try:
        w = await manager.warmup(symbol, bars_1m=WARMUP_1M_BARS)
    except Exception:  # noqa: BLE001 - one bad symbol never blocks boot
        log.exception("warm-up failed for %s", symbol)
        return
    st = market[symbol]
    st.prev_close = w["prev_close"]
    st.high_52w = w["high_52w"]
    st.low_52w = w["low_52w"]
    st.daily_closes = w.get("daily_closes", [])
    for b in w["bars_1m"]:
        st.bars.append(b)
    store.candles_save(symbol, "1m", w["bars_1m"])
    log.info("warmed %s: %d 1m bars, prev_close=%s",
             symbol, len(w["bars_1m"]), w["prev_close"])


async def amain() -> None:
    settings = Settings.from_env()
    problems = settings.validate()
    for p in problems:
        log.error("config: %s", p)
    if any("ALPACA_KEY" in p for p in problems):
        raise SystemExit(1)

    store = Store(settings.db_path)
    market: dict[str, MarketState] = {}

    def notify(title: str, message: str, priority: int, tags: list) -> bool:
        # generous timeout: some public ntfy servers answer slowly
        return send_ntfy(settings.ntfy_server, settings.ntfy_topic,
                         title, message, priority, tags, timeout=30)

    # ---- feeds ---------------------------------------------------------
    feeds = [AlpacaFeed(settings.alpaca_key, settings.alpaca_secret)]
    if settings.finnhub_key:
        feeds.append(FinnhubFeed(settings.finnhub_key))
    manager = FeedManager(feeds, max_symbols=MAX_SYMBOLS)

    # ---- watchlist ------------------------------------------------------
    for s in settings.symbols:  # seed from .env on first run
        store.watchlist_add(s)
    symbols = store.watchlist_all()
    if not symbols:
        log.warning("watchlist is empty — add symbols in the dashboard "
                    "(http://localhost:%d)", settings.port)
    for symbol in symbols:
        market[symbol] = MarketState(symbol)

    engine = AlertEngine(store, notify, market)
    tick_queue: asyncio.Queue = asyncio.Queue()
    feed_task: asyncio.Task | None = None

    # ---- market regime (SPY vs SMA200, refreshed daily) ------------------
    alpaca = next((f for f in feeds if isinstance(f, AlpacaFeed)), None)

    def _daily_bars(symbol: str, days: int) -> list[dict]:
        feed = manager.feed_for(symbol) or alpaca or feeds[0]
        return feed.get_daily_bars(symbol, days)

    regime_cache = regime_mod.RegimeCache(store, _daily_bars)
    engine.regime_fn = regime_cache.current
    try:
        regime_cache.refresh()
    except Exception:  # noqa: BLE001 - regime is advisory only
        log.exception("regime refresh failed")

    # ---- session calendar (holidays/half-days explicit) -------------------
    try:
        n_days = sessions_mod.refresh_cache(store)
        log.info("session today: %s (%d calendar days cached)",
                 sessions_mod.session_state_now(store), n_days)
    except Exception:  # noqa: BLE001
        log.exception("calendar refresh failed")

    async def restart_feeds(new_plan: dict[str, list[str]]) -> None:
        nonlocal feed_task
        if feed_task is not None:
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
            feed_task = None
        if new_plan:
            feed_task = asyncio.create_task(manager.run(new_plan, tick_queue))

    async def add_symbol(symbol: str) -> None:
        symbol = symbol.upper()
        if symbol in market:
            return
        new_symbols = sorted(set(store.watchlist_all()) | {symbol})
        new_plan = manager.assign(new_symbols)  # raises if over capacity
        store.watchlist_add(symbol)
        market[symbol] = MarketState(symbol)
        await _warm_one(manager, store, market, symbol)
        await restart_feeds(new_plan)
        engine.refresh_alerts()

    async def remove_symbol(symbol: str) -> None:
        symbol = symbol.upper()
        store.watchlist_remove(symbol)
        market.pop(symbol, None)
        remaining = store.watchlist_all()
        new_plan = manager.assign(remaining) if remaining else {}
        await restart_feeds(new_plan)
        engine.refresh_alerts()

    # ---- warm-up --------------------------------------------------------
    plan = manager.assign(symbols) if symbols else {}
    log.info("feed plan: %s", {k: len(v) for k, v in plan.items()})
    for symbol in symbols:
        await _warm_one(manager, store, market, symbol)
    engine.refresh_alerts()

    # ---- dashboard ------------------------------------------------------
    def _last_price(symbol: str) -> float | None:
        st = market.get(symbol)
        if st and st.price:
            return st.price
        rows = store.candles_load(symbol, "1d", 1)
        return rows[0]["c"] if rows else None

    def _scores() -> dict:
        """Cached fundamentals for the watchlist; 0 Finnhub calls."""
        out = {}
        for symbol in store.watchlist_all():
            cached = store.fund_cache_get(symbol)
            if not cached:
                out[symbol] = {"symbol": symbol, "price": _last_price(symbol),
                               "error": "not fetched yet — press ↻"}
                continue
            data = dict(cached["data"])
            data["price"] = _last_price(symbol)
            out[symbol] = data
        return out

    def _scores_refresh(symbol: str) -> dict:
        data = fundamentals.get_scores(store, symbol,
                                       price=_last_price(symbol))
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data

    backtest_state: dict = {"ran_at": None, "results": {}}

    def _backtest_run() -> dict:
        res = backtest_mod.run_backtest(store.watchlist_all(), _daily_bars)
        backtest_state["ran_at"] = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat()
        backtest_state["results"] = res
        log.info("backtest done: %s",
                 {t: v["fires"] for t, v in res.items()})
        return {"ran_at": backtest_state["ran_at"], "results": res}

    extras = {
        "regime": regime_cache.refresh,
        "scores": _scores,
        "scores_refresh": _scores_refresh,
        "predict_get": predict_mod.load_model,
        "predict_train": lambda s: predict_mod.train_symbol(s, _daily_bars),
        "backtest_run": _backtest_run,
        "backtest_last": lambda: backtest_state,
        "daily_bars": _daily_bars,
    }
    app = create_app(store, engine, market,
                     {"add_symbol": add_symbol, "remove_symbol": remove_symbol},
                     dashboard_token=settings.dashboard_token,
                     extras=extras)
    server = uvicorn.Server(uvicorn.Config(
        app, host=settings.dashboard_host, port=settings.port, log_level="warning"))

    # ---- event poller (Finnhub: splits/buybacks/news/earnings, hourly) ----
    poller = None
    if settings.finnhub_key:
        poller = event_alerts.EventPoller(
            store, engine, settings.finnhub_key, daily_bars_fn=_daily_bars)

    # ---- run everything --------------------------------------------------
    async def pump_ticks():
        while True:
            tick: Tick = await tick_queue.get()
            try:
                engine.on_tick(tick.symbol, tick.price, tick.size, tick.ts)
            except Exception:  # noqa: BLE001 - one bad tick never kills the loop
                log.exception("tick handling failed for %s", tick.symbol)

    tasks = [
        asyncio.create_task(pump_ticks(), name="ticks"),
        asyncio.create_task(server.serve(), name="dashboard"),
    ]
    if poller is not None:
        tasks.append(asyncio.create_task(poller.run_forever(), name="events"))
    await restart_feeds(plan)

    stop = asyncio.Event()

    def _handle_signal(*_):
        log.info("shutting down…")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    log.info("stockbot live — dashboard at http://%s:%d",
             settings.dashboard_host, settings.port)
    await stop.wait()
    for t in tasks:
        t.cancel()
    if feed_task is not None:
        feed_task.cancel()
        tasks.append(feed_task)
    await asyncio.gather(*tasks, return_exceptions=True)
    store.close()
    log.info("bye")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
