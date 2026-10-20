"""Market-data feeds with a provider abstraction.

Primary: Alpaca free Basic plan — real-time US stock trades over websocket
(IEX feed, max 30 streaming symbols), plus REST for bar history backfill.
Overflow: Finnhub free websocket (real-time US trades) for symbols 31..39.

The FeedManager assigns symbols across providers so the engine never needs
to know which provider a tick came from. Total cap: 39 symbols.
"""
from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import requests

from wsproxy import ws_connect

log = logging.getLogger("stockbot.feed")

Tick = namedtuple("Tick", ["symbol", "price", "size", "ts"])  # ts = epoch seconds

SESSIONS_52W = 252  # trading sessions in one year


def _et_today() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def calendar_days_for(sessions: int) -> int:
    """Calendar-day window that holds ``sessions`` trading days (+ slack
    for holidays): ~252 sessions per 365 calendar days."""
    return int(sessions * 365 / 252) + 15


def daily_stats(daily: list[dict], today_et: str) -> dict:
    """prev_close / 52-week high+low / closes from COMPLETED daily bars.

    A bar dated today (ET) is the still-forming session — during market
    hours Alpaca returns it — so it is dropped: prev_close must be the
    LAST COMPLETED session's close, and the 52-week extremes must not
    already include today's move (or a new high could never be "new").
    """
    # Daily-bar timestamps mark the session's date at 00:00 UTC (Finnhub)
    # or 04:00/05:00 UTC = midnight ET (Alpaca); the UTC date is the
    # session date for both.
    done = [b for b in daily
            if datetime.fromtimestamp(b["ts"], timezone.utc).date()
            .isoformat() < today_et]
    year = done[-SESSIONS_52W:]
    return {
        "prev_close": done[-1]["c"] if done else None,
        "high_52w": max(b["h"] for b in year) if year else None,
        "low_52w": min(b["l"] for b in year) if year else None,
        "daily_closes": [b["c"] for b in done],
    }


def _parse_alpaca_ts(s: str) -> float:
    # "2026-10-19T14:00:00.123456789Z" — trim to microseconds for fromisoformat
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".")
        frac, tz = tail[:-6], tail[-6:]
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


class DataFeed(ABC):
    """One market-data provider. Implementations stream ticks onto a queue."""

    name: str = "base"
    max_symbols: int = 0

    @abstractmethod
    def has_credentials(self) -> bool: ...

    @abstractmethod
    async def run(self, symbols: list[str], queue: asyncio.Queue) -> None:
        """Stream Tick objects for ``symbols`` onto ``queue`` forever.

        Must reconnect with backoff on disconnect; only return when the
        symbol list is empty or credentials are missing.
        """

    @abstractmethod
    def get_1m_bars(self, symbol: str, limit: int = 100) -> list[dict]:
        """Recent 1-minute bars, oldest-first: {ts,o,h,l,c,v} (ts = epoch min)."""

    @abstractmethod
    def get_daily_bars(self, symbol: str, days: int = 400) -> list[dict]:
        """Recent daily bars, oldest-first: {ts,o,h,l,c,v} (ts = epoch sec)."""


# ---------------------------------------------------------------- Alpaca ---
class AlpacaFeed(DataFeed):
    name = "alpaca"
    max_symbols = 30
    WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
    REST = "https://data.alpaca.markets/v2"

    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def has_credentials(self) -> bool:
        return bool(self.key and self.secret)

    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    async def run(self, symbols: list[str], queue: asyncio.Queue) -> None:
        if not symbols or not self.has_credentials():
            return
        backoff = 1
        while symbols:
            try:
                async with await ws_connect(
                    self.WS_URL, ping_interval=20, ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    await ws.send(json.dumps(
                        {"action": "auth", "key": self.key, "secret": self.secret}))
                    # wait for the auth confirmation
                    async for raw in ws:
                        for msg in json.loads(raw):
                            if msg.get("T") == "error":
                                log.error("alpaca auth/sub error: %s", msg.get("msg"))
                                raise RuntimeError(msg.get("msg"))
                            if msg.get("T") == "success" and \
                               "authenticated" in str(msg.get("msg", "")):
                                break
                        else:
                            continue
                        break
                    await ws.send(json.dumps(
                        {"action": "subscribe", "trades": symbols}))
                    log.info("alpaca subscribed to %d symbols (IEX)", len(symbols))
                    backoff = 1
                    async for raw in ws:
                        for msg in json.loads(raw):
                            if msg.get("T") != "t":
                                continue  # subscription acks, etc.
                            try:
                                queue.put_nowait(Tick(
                                    symbol=msg["S"],
                                    price=float(msg["p"]),
                                    size=int(msg.get("s", 0)),
                                    ts=_parse_alpaca_ts(msg["t"]),
                                ))
                            except (KeyError, ValueError) as e:
                                log.debug("alpaca bad tick skipped: %s", e)
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                log.warning("alpaca disconnected (%s); retry in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _rest(self, path: str, params: dict) -> dict:
        r = requests.get(f"{self.REST}{path}", headers=self._headers(),
                         params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def get_1m_bars(self, symbol: str, limit: int = 100) -> list[dict]:
        # Free Basic plan cannot fetch the latest 15 minutes; end before that.
        end = datetime.now(timezone.utc) - timedelta(minutes=16)
        start = end - timedelta(days=3)
        # sort=desc: Alpaca's `limit` keeps the FIRST N bars in sort order,
        # so asc + limit returned the oldest bars of the 3-day window.
        data = self._rest("/stocks/bars", {
            "symbols": symbol, "timeframe": "1Min",
            "start": start.isoformat(), "end": end.isoformat(),
            "limit": min(limit, 1000), "feed": "iex", "sort": "desc",
        })
        out = []
        for b in data.get("bars", {}).get(symbol, []):
            ts = int(_parse_alpaca_ts(b["t"]) // 60 * 60)
            out.append({"ts": ts, "o": b["o"], "h": b["h"],
                        "l": b["l"], "c": b["c"], "v": b.get("v", 0)})
        out.sort(key=lambda b: b["ts"])  # engine expects oldest-first
        return out[-limit:]

    def get_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Latest trade + today's and yesterday's daily bars for up to 100
        symbols per call (IEX feed) — live prices for ANY stock, not just
        the 30 on the websocket."""
        out: dict[str, dict] = {}
        syms = sorted({s.upper() for s in symbols if s})
        for i in range(0, len(syms), 100):
            data = self._rest("/stocks/snapshots",
                              {"symbols": ",".join(syms[i:i + 100]),
                               "feed": "iex"})
            out.update(parse_snapshots(data))
        return out

    def get_daily_bars(self, symbol: str, days: int = 400) -> list[dict]:
        """The last ``days`` daily bars (trading sessions), oldest-first."""
        end = datetime.now(timezone.utc) - timedelta(minutes=16)
        # ~252 sessions per 365 calendar days: size the window in calendar
        # days so `days` really means sessions
        start = end - timedelta(days=calendar_days_for(days))
        params = {
            "symbols": symbol, "timeframe": "1Day",
            "start": start.isoformat(), "end": end.isoformat(),
            "limit": 1000, "feed": "iex", "sort": "asc",
            "adjustment": "split",
        }
        out = []
        while True:
            data = self._rest("/stocks/bars", params)
            for b in data.get("bars", {}).get(symbol, []):
                out.append({"ts": int(_parse_alpaca_ts(b["t"])),
                            "o": b["o"], "h": b["h"], "l": b["l"],
                            "c": b["c"], "v": b.get("v", 0)})
            tok = data.get("next_page_token")
            if not tok:
                break
            params = {**params, "page_token": tok}
        return out[-days:]


def parse_snapshots(data: dict) -> dict[str, dict]:
    """Alpaca /v2/stocks/snapshots -> {SYM: {price, prev_close, day_pct,
    ts, bar}} where bar is today's daily bar {ts,o,h,l,c,v} (or None)."""
    raw = data.get("snapshots", data) if isinstance(data, dict) else {}
    out: dict[str, dict] = {}
    for sym, snap in (raw or {}).items():
        if not isinstance(snap, dict):
            continue
        lt = snap.get("latestTrade") or {}
        db = snap.get("dailyBar") or {}
        pb = snap.get("prevDailyBar") or {}
        price = lt.get("p") or db.get("c")
        if not price:
            continue
        prev = pb.get("c")
        bar = None
        if db.get("t"):
            bar = {"ts": int(_parse_alpaca_ts(db["t"])), "o": db.get("o"),
                   "h": max(db.get("h") or price, price),
                   "l": min(db.get("l") or price, price),
                   "c": price, "v": db.get("v", 0)}
        out[sym.upper()] = {
            "price": float(price), "prev_close": prev,
            "day_pct": ((price - prev) / prev * 100) if prev else None,
            "ts": _parse_alpaca_ts(lt["t"]) if lt.get("t") else None,
            "vwap": db.get("vw"),
            "bar": bar}
    return out


# --------------------------------------------------------------- Finnhub ---
class FinnhubFeed(DataFeed):
    """Free-tier overflow provider: real-time US trades over websocket."""
    name = "finnhub"
    max_symbols = 50
    WS_URL = "wss://ws.finnhub.io"

    def __init__(self, key: str):
        self.key = key

    def has_credentials(self) -> bool:
        return bool(self.key)

    async def run(self, symbols: list[str], queue: asyncio.Queue) -> None:
        if not symbols or not self.has_credentials():
            return
        backoff = 1
        while symbols:
            try:
                async with await ws_connect(
                    f"{self.WS_URL}?token={self.key}",
                    ping_interval=20, ping_timeout=20,
                ) as ws:
                    for s in symbols:
                        await ws.send(json.dumps({"type": "subscribe", "symbol": s}))
                    log.info("finnhub subscribed to %d symbols", len(symbols))
                    backoff = 1
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "trade" or not msg.get("data"):
                            continue
                        for t in msg["data"]:
                            try:
                                queue.put_nowait(Tick(
                                    symbol=t["s"],
                                    price=float(t["p"]),
                                    size=float(t.get("v", 0)),
                                    ts=float(t["t"]) / 1000.0,
                                ))
                            except (KeyError, ValueError) as e:
                                log.debug("finnhub bad tick skipped: %s", e)
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                log.warning("finnhub disconnected (%s); retry in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def get_1m_bars(self, symbol: str, limit: int = 100) -> list[dict]:
        to_ = int(datetime.now(timezone.utc).timestamp())
        frm = to_ - 3 * 86400
        r = requests.get("https://finnhub.io/api/v1/stock/candle", params={
            "symbol": symbol, "resolution": "1", "from": frm, "to": to_,
            "token": self.key}, timeout=20)
        r.raise_for_status()
        d = r.json()
        if d.get("s") != "ok":
            return []
        return [{"ts": int(t // 60 * 60), "o": o, "h": h, "l": l, "c": c, "v": v}
                for t, o, h, l, c, v in
                zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])][-limit:]

    def get_daily_bars(self, symbol: str, days: int = 400) -> list[dict]:
        to_ = int(datetime.now(timezone.utc).timestamp())
        frm = to_ - calendar_days_for(days) * 86400
        r = requests.get("https://finnhub.io/api/v1/stock/candle", params={
            "symbol": symbol, "resolution": "D", "from": frm, "to": to_,
            "token": self.key}, timeout=20)
        r.raise_for_status()
        d = r.json()
        if d.get("s") != "ok":
            return []
        return [{"ts": int(t), "o": o, "h": h, "l": l, "c": c, "v": v}
                for t, o, h, l, c, v in
                zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])][-days:]


# ---------------------------------------------------------- Feed manager ---
class FeedManager:
    """Assigns symbols across providers and merges their ticks.

    Fill order: Alpaca (up to 30) first, then Finnhub for the overflow.
    Hard cap: 39 symbols total.
    """

    def __init__(self, feeds: list[DataFeed], max_symbols: int = 39):
        self.feeds = [f for f in feeds if f.has_credentials()]
        if not self.feeds:
            raise RuntimeError("no feed has credentials — check .env")
        self.max_symbols = max_symbols
        self.assignment: dict[str, DataFeed] = {}  # symbol -> feed

    def capacity(self) -> int:
        return min(sum(f.max_symbols for f in self.feeds), self.max_symbols)

    def assign(self, symbols: list[str]) -> dict[str, list[str]]:
        """(Re)assign symbols to feeds. Returns {feed_name: [symbols]}.

        Raises ValueError if the request exceeds total capacity.
        """
        symbols = [s.upper() for s in symbols]
        if len(symbols) > self.capacity():
            raise ValueError(
                f"{len(symbols)} symbols requested but providers cap at "
                f"{self.capacity()} (engine hard cap {self.max_symbols})")
        self.assignment = {}
        plan: dict[str, list[str]] = {}
        remaining = list(symbols)
        for feed in self.feeds:
            take = remaining[:feed.max_symbols]
            remaining = remaining[feed.max_symbols:]
            plan[feed.name] = take
            for s in take:
                self.assignment[s] = feed
            if not remaining:
                break
        if remaining:  # pragma: no cover - guarded by capacity check
            raise ValueError(f"could not place symbols: {remaining}")
        return plan

    def feed_for(self, symbol: str) -> DataFeed | None:
        return self.assignment.get(symbol.upper())

    async def warmup(self, symbol: str, bars_1m: int = 100,
                     daily_days: int = 400) -> dict:
        """Fetch history for indicator warm-up. Runs in a thread (blocking IO)."""
        feed = self.feed_for(symbol)
        if feed is None:
            raise ValueError(f"{symbol} is not assigned to any feed")
        bars = await asyncio.to_thread(feed.get_1m_bars, symbol, bars_1m)
        stats = await self.daily_refresh(symbol, daily_days)
        return {"bars_1m": bars, **stats}

    async def daily_refresh(self, symbol: str, daily_days: int = 400) -> dict:
        """prev_close / 52-week range / daily closes from completed sessions
        (see daily_stats). Run at warm-up and again every new trading day."""
        feed = self.feed_for(symbol)
        if feed is None:
            raise ValueError(f"{symbol} is not assigned to any feed")
        daily = await asyncio.to_thread(feed.get_daily_bars, symbol, daily_days)
        return daily_stats(daily, _et_today())

    async def run(self, plan: dict[str, list[str]], queue: asyncio.Queue) -> None:
        """Run every feed's websocket loop concurrently (each reconnects)."""
        by_name = {f.name: f for f in self.feeds}
        await asyncio.gather(*[
            by_name[name].run(symbols, queue)
            for name, symbols in plan.items() if symbols
        ])
