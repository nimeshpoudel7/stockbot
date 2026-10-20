"""Alert engine: evaluates alert rules on ticks and 1-minute bar closes.

State machine per alert (persisted in SQLite, so a restart never re-fires):
    ARMED → TRIGGERED → COOLDOWN → ARMED
                          ↘ EXPIRED (one-time alerts, or past expires_at)

- TRIGGERED is transient: on fire we notify + record the event, then move to
  COOLDOWN (or EXPIRED for one-time alerts).
- Re-arm from COOLDOWN requires the cooldown to elapse AND hysteresis:
  price must move back ``hysteresis_pct`` past the trigger before re-arming.
- Dedupe: an alert never fires twice for the same trigger minute
  (key = alert_id + minute bucket).
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import indicators as ind
from storage import Store

log = logging.getLogger("stockbot.engine")

ET = ZoneInfo("America/New_York")

# Alert types evaluated on every tick vs on bar close.
# Bar-close types can target a timeframe via the alert's ``timeframe`` field
# ("1m" default, or "5m"/"15m"/"1h"); the engine evaluates each type on the
# close of its own timeframe's bars.
TICK_TYPES = {"price_above", "price_below", "day_pct_change",
              "high_52w", "low_52w", "atr_stop"}
BAR_TYPES = {"rsi_14", "volume_spike", "sma_cross",
             "bollinger_break", "bollinger_squeeze", "fib_cross"}

# Event-driven types: fired by the hourly Finnhub poller (event_alerts.py),
# never by ticks/bars directly.
EVENT_TYPES = {"split_announced", "buyback_announced", "news_spike",
               "sell_the_news"}

TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


class MarketState:
    """In-memory per-symbol state: quote, forming 1m bar, closed bars."""

    def __init__(self, symbol: str, max_bars: int = 300):
        self.symbol = symbol
        self.price: float | None = None
        self.prev_price: float | None = None
        self.ts: float | None = None
        self.prev_close: float | None = None
        self.high_52w: float | None = None
        self.low_52w: float | None = None
        self.bars: deque[dict] = deque(maxlen=max_bars)  # closed 1m bars
        self.forming: dict | None = None
        # multi-timeframe: closed + forming bars per timeframe
        self.mtf: dict[str, deque[dict]] = {
            tf: deque(maxlen=max_bars) for tf in TF_MINUTES}
        self.mtf_forming: dict[str, dict | None] = {
            tf: None for tf in TF_MINUTES}
        self.daily_closes: list[float] = []  # oldest-first, seeded at warm-up

    def closes(self) -> list[float]:
        return [b["c"] for b in self.bars]


def _et(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%I:%M %p").lstrip("0") + " ET"


class AlertEngine:
    def __init__(self, store: Store, notify_fn, market: dict[str, MarketState],
                 regime_fn=None):
        """
        notify_fn(title, message, priority, tags) -> bool
        market: symbol -> MarketState (shared with the feed runner)
        regime_fn: optional () -> "bull" | "bear" | "sideways" | None
        """
        self.store = store
        self.notify = notify_fn
        self.market = market
        self.regime_fn = regime_fn
        self._alerts: dict[int, dict] = {}
        self.refresh_alerts()

    # ---- alert registry -------------------------------------------------
    def refresh_alerts(self) -> None:
        self._alerts = {a["id"]: a for a in self.store.alert_list()}

    # ---- entry points ---------------------------------------------------
    def on_tick(self, symbol: str, price: float, size: float, ts: float) -> None:
        st = self.market.get(symbol)
        if st is None:
            return
        st.prev_price = st.price
        st.price, st.ts = price, ts
        minute = int(ts // 60 * 60)
        if st.forming is None or st.forming["ts"] != minute:
            if st.forming is not None:  # finalize the previous bar
                st.bars.append(st.forming)
                self.store.candles_save(symbol, "1m", [st.forming])
                self._evaluate(symbol, BAR_TYPES, st.forming["c"], ts,
                               bar=st.forming, bars=list(st.bars), tf="1m")
                self._roll_mtf(st, symbol, st.forming, ts)
            st.forming = {"ts": minute, "o": price, "h": price,
                          "l": price, "c": price, "v": 0.0}
        f = st.forming
        f["h"] = max(f["h"], price)
        f["l"] = min(f["l"], price)
        f["c"] = price
        f["v"] += size
        self._evaluate(symbol, TICK_TYPES, price, ts)

    def _roll_mtf(self, st: MarketState, symbol: str,
                  closed_1m: dict, ts: float) -> None:
        """Fold a closed 1m bar into 5m/15m/1h; evaluate TF alerts on TF close."""
        for tf, mins in TF_MINUTES.items():
            span = mins * 60
            bucket = closed_1m["ts"] // span
            forming = st.mtf_forming[tf]
            if forming is None or forming.get("_bucket") != bucket:
                if forming is not None:
                    forming.pop("_bucket", None)
                    st.mtf[tf].append(forming)
                    self.store.candles_save(symbol, tf, [forming])
                    self._evaluate(symbol, BAR_TYPES, forming["c"], ts,
                                   bar=forming, bars=list(st.mtf[tf]), tf=tf)
                st.mtf_forming[tf] = {
                    "_bucket": bucket, "ts": bucket * span,
                    "o": closed_1m["o"], "h": closed_1m["h"],
                    "l": closed_1m["l"], "c": closed_1m["c"],
                    "v": closed_1m.get("v", 0)}
            else:
                forming["h"] = max(forming["h"], closed_1m["h"])
                forming["l"] = min(forming["l"], closed_1m["l"])
                forming["c"] = closed_1m["c"]
                forming["v"] = forming.get("v", 0) + closed_1m.get("v", 0)

    # ---- evaluation -----------------------------------------------------
    def _regime_ok(self, alert: dict) -> bool:
        want = alert.get("regime", "any") or "any"
        if want == "any" or self.regime_fn is None:
            return True
        try:
            cur = self.regime_fn()
        except Exception:  # noqa: BLE001 - fail open on regime errors
            return True
        return cur is None or cur == want

    def _evaluate(self, symbol: str, types: set[str], price: float,
                  ts: float, bar: dict | None = None,
                  bars: list[dict] | None = None, tf: str = "1m") -> None:
        for alert in self._alerts.values():
            if alert["symbol"] != symbol or alert["type"] not in types:
                continue
            if (alert.get("timeframe") or "1m") != tf:
                continue
            if alert["paused"]:
                continue
            if not self._regime_ok(alert):
                continue
            state = self.store.state_get(alert["id"])
            if self._expired(alert, state, ts):
                continue
            if state["state"] == "COOLDOWN":
                if self._cooldown_done(state, ts, alert) and \
                   self._rearm_ok(alert, state, symbol, price):
                    self.store.state_set(alert["id"], state="ARMED",
                                         rearm_ref=None)
                    log.info("alert %d re-armed", alert["id"])
                continue
            if state["state"] != "ARMED":
                continue  # EXPIRED (or anything unexpected)
            fired, detail = self._check(alert, symbol, price, bar, bars)
            if not fired:
                continue
            minute = int(ts // 60 * 60)
            if self.store.event_exists_for_minute(alert["id"], minute):
                continue  # dedupe: already fired for this minute
            self._fire(alert, price, ts, detail)

    # ---- state machine --------------------------------------------------
    def _expired(self, alert: dict, state: dict, ts: float) -> bool:
        if state["state"] == "EXPIRED":
            return True
        exp = alert.get("expires_at")
        if exp:
            try:
                exp_ts = datetime.fromisoformat(exp).timestamp()
            except ValueError:
                return False
            if ts >= exp_ts:
                self.store.state_set(alert["id"], state="EXPIRED")
                log.info("alert %d expired", alert["id"])
                return True
        return False

    def _cooldown_done(self, state: dict, ts: float, alert: dict) -> bool:
        cu = state.get("cooldown_until")
        if not cu:
            return True
        try:
            return ts >= datetime.fromisoformat(cu).timestamp()
        except ValueError:
            return True

    def _rearm_ok(self, alert: dict, state: dict, symbol: str,
                  price: float) -> bool:
        """Hysteresis: price must move back past the trigger before re-arm."""
        st = self.market.get(symbol)
        h = alert.get("hysteresis_pct", 0.005) or 0.0
        ref = state.get("rearm_ref")
        t = alert["type"]
        p = alert["params"]
        if t == "price_above" and ref:
            return price <= ref * (1 - h)
        if t == "price_below" and ref:
            return price >= ref * (1 + h)
        if t == "day_pct_change" and st and st.prev_close:
            cur = ind.day_pct_change(price, st.prev_close)
            if cur is None:
                return False
            thr = float(p["pct"])
            # re-arm once the move has faded back past the threshold band
            return cur <= thr * (1 - h) if thr >= 0 else cur >= thr * (1 - h)
        if t in ("high_52w",) and st and st.high_52w:
            return price <= st.high_52w * (1 - h)
        if t in ("low_52w",) and st and st.low_52w:
            return price >= st.low_52w * (1 + h)
        # Indicator alerts: re-arm once the condition is no longer true.
        fired, _ = self._check(alert, symbol, price, None, None)
        return not fired

    def _fire(self, alert: dict, price: float, ts: float, detail: str) -> None:
        symbol = alert["symbol"]
        title = f"\U0001F514 {symbol} ${price:,.2f}"
        message = f"{detail} — triggered {_et(ts)}"
        tags = ["chart_with_upwards_trend"]
        ok = self.notify(title, message, alert.get("ntfy_priority", 3), tags)
        self.store.event_record(alert["id"], symbol, price,
                                detail + ("" if ok else " [push failed]"),
                                trigger_ts=ts)
        if alert.get("one_time"):
            self.store.state_set(alert["id"], state="EXPIRED",
                                 last_trigger_ts=datetime.fromtimestamp(
                                     ts, tz=timezone.utc).isoformat(),
                                 rearm_ref=price)
            log.info("alert %d fired (one-time) -> EXPIRED", alert["id"])
        else:
            cooldown_until = datetime.fromtimestamp(
                ts + alert.get("cooldown_min", 15) * 60,
                tz=timezone.utc).isoformat()
            self.store.state_set(
                alert["id"], state="COOLDOWN",
                last_trigger_ts=datetime.fromtimestamp(
                    ts, tz=timezone.utc).isoformat(),
                cooldown_until=cooldown_until, rearm_ref=price)
            log.info("alert %d fired -> COOLDOWN until %s",
                     alert["id"], cooldown_until)

    # ---- external event firing (Finnhub poller) -------------------------------
    def fire_external(self, alert_id: int, price: float, detail: str,
                      ts: float, event_key: str) -> bool:
        """Fire an EVENT_TYPES alert from the poller.

        ``event_key`` (e.g. a split ex-date or news date) dedupes across
        poller runs: the same key never fires twice for one alert.
        """
        alert = self._alerts.get(alert_id)
        if alert is None:
            return False
        if alert["paused"]:
            return False
        state = self.store.state_get(alert_id)
        if self._expired(alert, state, ts):
            return False
        if state["state"] != "ARMED":
            return False
        if not self._regime_ok(alert):
            return False
        last_key = self.store.poller_get(f"fired:{alert_id}")
        if last_key == event_key:
            return False
        self._fire(alert, price, ts, detail)
        self.store.poller_set(f"fired:{alert_id}", event_key)
        return True

    # ---- rule checks ------------------------------------------------------
    def _check(self, alert: dict, symbol: str, price: float,
               bar: dict | None, bars: list[dict] | None = None
               ) -> tuple[bool, str]:
        st = self.market.get(symbol)
        t, p = alert["type"], alert["params"]
        work = bars if bars is not None else (list(st.bars) if st else [])
        wcloses = [b["c"] for b in work]
        if t == "price_above":
            target = float(p["target"])
            if price >= target:
                return True, f"Price above ${target:,.2f}"
        elif t == "price_below":
            target = float(p["target"])
            if price <= target:
                return True, f"Price below ${target:,.2f}"
        elif t == "day_pct_change":
            if st and st.prev_close:
                pct = ind.day_pct_change(price, st.prev_close)
                thr = float(p["pct"])
                if pct is not None and (
                        (thr >= 0 and pct >= thr) or (thr < 0 and pct <= thr)):
                    arrow = "Up" if pct >= 0 else "Down"
                    return True, f"{arrow} {abs(pct):.2f}% on the day"
        elif t == "high_52w":
            if st and st.high_52w and price > st.high_52w:
                return True, (f"New 52-week high ${price:,.2f} "
                               f"(prev ${st.high_52w:,.2f})")
        elif t == "low_52w":
            if st and st.low_52w and price < st.low_52w:
                return True, (f"New 52-week low ${price:,.2f} "
                               f"(prev ${st.low_52w:,.2f})")
        elif t == "rsi_14" and len(wcloses) >= 16:
            series = ind.rsi_wilder(wcloses, 14)
            prev, last = series[-2], series[-1]
            above = float(p.get("above", 70))
            below = float(p.get("below", 30))
            if prev is not None and last is not None:
                if prev <= above < last:
                    return True, f"RSI(14) crossed above {above:g} ({last:.1f})"
                if prev >= below > last:
                    return True, f"RSI(14) crossed below {below:g} ({last:.1f})"
        elif t == "volume_spike" and bar is not None:
            mult = float(p.get("mult", 3))
            lookback = int(p.get("lookback", 20))
            vols = [b["v"] for b in work[:-1]]  # exclude current bar
            if ind.volume_spike(bar["v"], vols, mult, lookback):
                avg = sum(vols[-lookback:]) / lookback
                return True, (f"Volume spike: {bar['v']:,.0f} vs "
                               f"avg {avg:,.0f} ({bar['v']/avg:.1f}x)")
        elif t == "sma_cross":
            fast_n = int(p.get("fast", 20))
            slow_n = int(p.get("slow", 50))
            if len(wcloses) >= slow_n + 1:
                fast = [ind.sma(wcloses[:i], fast_n) for i in range(2, len(wcloses) + 1)]
                slow = [ind.sma(wcloses[:i], slow_n) for i in range(2, len(wcloses) + 1)]
                if ind.crossed_above(fast, slow):
                    return True, f"SMA({fast_n}) crossed above SMA({slow_n})"
                if ind.crossed_below(fast, slow):
                    return True, f"SMA({fast_n}) crossed below SMA({slow_n})"
        elif t == "bollinger_break" and len(wcloses) >= 22:
            n = int(p.get("n", 20))
            k = float(p.get("k", 2.0))
            # bands from the 20 bars before the just-closed one; break = close
            # crosses outside while the prior close was inside
            mid, upper, lower = ind.bollinger_bands(wcloses[:-1], n, k)
            if mid is not None:
                prev_c, last_c = wcloses[-2], wcloses[-1]
                if lower < prev_c < upper and last_c >= upper:
                    return True, (f"Bollinger breakout above ${upper:,.2f} "
                                   f"(close ${last_c:,.2f})")
                if lower < prev_c < upper and last_c <= lower:
                    return True, (f"Bollinger breakdown below ${lower:,.2f} "
                                   f"(close ${last_c:,.2f})")
        elif t == "bollinger_squeeze" and st and len(st.daily_closes) >= 130:
            lookback = int(p.get("lookback_days", 126))
            closes = st.daily_closes
            now_bw = ind.bollinger_bandwidth(closes)
            if now_bw is not None:
                past = [ind.bollinger_bandwidth(closes[:i])
                        for i in range(20, len(closes))]
                past = [b for b in past[:-1] if b is not None]
                if past and now_bw <= min(past[-lookback:]):
                    return True, (f"Bollinger squeeze: bandwidth {now_bw:.3f} "
                                   f"at {lookback}-day low")
        elif t == "fib_cross" and len(work) >= 12:
            lookback = int(p.get("lookback", 50))
            hi, lo = ind.swing_high_low(work, lookback)
            if hi is not None and lo is not None and hi > lo:
                levels = ind.fib_levels(lo, hi)
                prev_c = wcloses[-2]
                last_c = wcloses[-1]
                for ratio in sorted(levels):
                    lvl = levels[ratio]
                    if (prev_c - lvl) * (last_c - lvl) < 0:
                        direction = "up" if last_c > prev_c else "down"
                        return True, (f"Price crossed Fibonacci {ratio:.1%} "
                                       f"retracement ${lvl:,.2f} ({direction})")
        elif t == "atr_stop" and len(work) >= 16:
            # Guide p20: long stop = entry − k×ATR(14); short = entry + k×ATR.
            entry = float(p.get("entry", 0) or 0)
            k = float(p.get("k", 2.0))
            direction = str(p.get("direction", "long")).lower()
            a = ind.atr(work, 14)
            if entry > 0 and a is not None:
                if direction == "short":
                    stop = entry + k * a
                    if price >= stop:
                        return True, (f"ATR stop hit (short): ${price:,.2f} "
                                       f">= stop ${stop:,.2f} (entry ${entry:,.2f}, "
                                       f"k={k:g})")
                else:
                    stop = entry - k * a
                    if price <= stop:
                        return True, (f"ATR stop hit (long): ${price:,.2f} "
                                       f"<= stop ${stop:,.2f} (entry ${entry:,.2f}, "
                                       f"k={k:g})")
        return False, ""


def describe(alert: dict) -> str:
    """Human-readable rule summary for the dashboard."""
    t, p = alert["type"], alert["params"]
    s = alert["symbol"]
    tf = (alert.get("timeframe") or "1m")
    rg = (alert.get("regime") or "any")
    suffix = ""
    if tf != "1m" or rg != "any":
        suffix = f" [{tf}{', ' + rg + ' only' if rg != 'any' else ''}]"
    if t == "price_above":
        return f"{s} ≥ ${float(p['target']):,.2f}{suffix}"
    if t == "price_below":
        return f"{s} ≤ ${float(p['target']):,.2f}{suffix}"
    if t == "day_pct_change":
        return f"{s} {'+' if float(p['pct']) >= 0 else ''}{p['pct']}% on the day{suffix}"
    if t == "rsi_14":
        return f"{s} RSI(14) cross {p.get('below', 30):g}/{p.get('above', 70):g}{suffix}"
    if t == "volume_spike":
        return f"{s} volume > {p.get('mult', 3):g}× {p.get('lookback', 20)}-bar avg{suffix}"
    if t == "high_52w":
        return f"{s} new 52-week high{suffix}"
    if t == "low_52w":
        return f"{s} new 52-week low{suffix}"
    if t == "sma_cross":
        return f"{s} SMA({float(p.get('fast', 20)):g}) × SMA({float(p.get('slow', 50)):g}) cross{suffix}"
    if t == "bollinger_break":
        return f"{s} Bollinger({float(p.get('n', 20)):g}, {float(p.get('k', 2.0)):g}σ) breakout{suffix}"
    if t == "bollinger_squeeze":
        return f"{s} Bollinger squeeze (bandwidth at 6-mo low){suffix}"
    if t == "fib_cross":
        return f"{s} Fibonacci retracement cross{suffix}"
    if t == "atr_stop":
        d = "long" if str(p.get("direction", "long")).lower() != "short" else "short"
        return (f"{s} ATR stop ({d}): entry ${float(p.get('entry', 0)):,.2f}, "
                f"k={float(p.get('k', 2.0)):g}{suffix}")
    if t == "split_announced":
        return f"{s} stock split announced{suffix}"
    if t == "buyback_announced":
        return f"{s} buyback announced{suffix}"
    if t == "news_spike":
        return f"{s} news volume spike{suffix}"
    if t == "sell_the_news":
        return f"{s} sell-the-news (beat then −3% next session){suffix}"
    return f"{s} {t}{suffix}"
