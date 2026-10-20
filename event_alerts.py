"""Hourly event poller for EVENT_TYPES alerts (Finnhub-backed).

- split_announced / buyback_announced: Finnhub's /stock/split is NOT on the
  free tier (403), so these are detected from /company-news headlines via
  keyword matching instead. Zero extra API calls beyond the news poll.
- news_spike: daily article count vs the 20-day median, from the same poll.
- sell_the_news: /stock/earnings beat (actual > estimate) + next-session
  close down >3% (Alpaca daily bars), using the calendar for session stepping.

Budget: at most ONE /company-news + ONE /stock/earnings call per symbol per
day, and only for symbols that actually have an ARMED event alert. With the
39-symbol cap that is at most 78 calls/day in the pathological case; in
practice only a handful of symbols carry event alerts. Fundamentals refreshes
are user-triggered and share the same ~50/day discipline.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import requests

log = logging.getLogger("stockbot.events")

NEWS_API = "https://finnhub.io/api/v1/company-news"
EARN_API = "https://finnhub.io/api/v1/stock/earnings"

SPLIT_KEYWORDS = ("stock split", "share split", "split ratio",
                  "forward split", "reverse split")
BUYBACK_KEYWORDS = ("buyback", "share repurchase", "repurchase program",
                    "repurchase authoriz", "buy back")


class EventPoller:
    def __init__(self, store, engine, finnhub_key: str,
                 daily_bars_fn=None):
        """
        daily_bars_fn(symbol, days) -> [bar dicts oldest-first, tf 1d]
        (Alpaca-backed; injected so tests can stub it)
        """
        self.store = store
        self.engine = engine
        self.key = finnhub_key
        self.daily_bars_fn = daily_bars_fn

    # ------------------------------------------------------------------ run
    async def run_forever(self, interval_s: int = 3600):
        while True:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 - poller must never die
                log.warning("event poller error: %s", e)
            await asyncio.sleep(interval_s)

    def run_once(self) -> dict:
        """One sweep. Returns {symbol: [fired alert ids]} for observability."""
        from alerts import EVENT_TYPES
        # self-heal: if the boot-time calendar refresh failed, retry here
        # (hourly is plenty; the cache covers 3 months ahead)
        if self.store.calendar_is_open(dt.date.today().isoformat()) is None:
            try:
                import sessions as sess_mod
                sess_mod.refresh_cache(self.store)
            except Exception as e:  # noqa: BLE001
                log.warning("calendar re-refresh failed: %s", e)
        self.engine.refresh_alerts()
        armed = [a for a in self.engine._alerts.values()
                 if a["type"] in EVENT_TYPES and not a["paused"]
                 and self.store.state_get(a["id"])["state"] == "ARMED"]
        by_symbol: dict[str, list[dict]] = {}
        for a in armed:
            by_symbol.setdefault(a["symbol"], []).append(a)
        fired: dict[str, list[int]] = {}
        for symbol, alerts in sorted(by_symbol.items()):
            try:
                got = self._poll_symbol(symbol, alerts)
            except Exception as e:  # noqa: BLE001
                log.warning("%s event poll failed: %s", symbol, e)
                continue
            if got:
                fired[symbol] = got
        return fired

    # ---------------------------------------------------------------- symbol
    def _poll_symbol(self, symbol: str, alerts: list[dict]) -> list[int]:
        fired: list[int] = []
        types = {a["type"] for a in alerts}
        today = dt.date.today().isoformat()

        articles: list[dict] | None = None
        if types & {"split_announced", "buyback_announced", "news_spike"}:
            if self.store.poller_get(f"poll_news:{symbol}") != today:
                articles = self._fetch_news(symbol)
                self.store.poller_set(f"poll_news:{symbol}", today)
            else:
                articles = []  # already polled today; dedupe via event keys

        if articles:
            fired += self._check_splits(symbol, alerts, articles)
            fired += self._check_buybacks(symbol, alerts, articles)
            fired += self._check_news_spike(symbol, alerts, articles)

        if "sell_the_news" in types:
            if self.store.poller_get(f"poll_earn:{symbol}") != today:
                self._check_earnings(symbol, alerts, fired)
                self.store.poller_set(f"poll_earn:{symbol}", today)
            else:
                # earnings data cached from today's earlier poll; the
                # next-session move may only be knowable now
                self._eval_sell_the_news(symbol, alerts, fired)
        return fired

    # ------------------------------------------------------------------ news
    def _fetch_news(self, symbol: str) -> list[dict]:
        to = dt.date.today()
        frm = to - dt.timedelta(days=25)
        r = requests.get(NEWS_API, params={
            "symbol": symbol, "from": frm.isoformat(), "to": to.isoformat(),
            "token": self.key}, timeout=30)
        r.raise_for_status()
        arts = r.json()
        log.info("%s: %d news articles in last 25d", symbol, len(arts))
        return arts if isinstance(arts, list) else []

    @staticmethod
    def _day_counts(articles: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in articles:
            day = dt.datetime.fromtimestamp(
                a.get("datetime", 0), dt.timezone.utc).date().isoformat()
            counts[day] = counts.get(day, 0) + 1
        return counts

    def _recent(self, articles: list[dict], days: int = 3) -> list[dict]:
        cutoff = time.time() - days * 86400
        return [a for a in articles if a.get("datetime", 0) >= cutoff]

    def _fire(self, alert: dict, price: float, detail: str,
              event_key: str, fired: list[int]) -> None:
        if self.engine.fire_external(alert["id"], price, detail,
                                     time.time(), event_key):
            log.info("event alert %d fired: %s", alert["id"], detail)
            fired.append(alert["id"])

    def _check_splits(self, symbol: str, alerts: list[dict],
                      articles: list[dict]) -> list[int]:
        fired: list[int] = []
        for a in self._recent(articles):
            text = f"{a.get('headline', '')} {a.get('summary', '')}".lower()
            if not any(k in text for k in SPLIT_KEYWORDS):
                continue
            key = f"split:{a.get('datetime')}:{a.get('url', '')[:60]}"
            for alert in alerts:
                if alert["type"] == "split_announced":
                    self._fire(alert, 0, f"Split headline: {a.get('headline')}",
                               key, fired)
        return fired

    def _check_buybacks(self, symbol: str, alerts: list[dict],
                        articles: list[dict]) -> list[int]:
        fired: list[int] = []
        for a in self._recent(articles):
            text = f"{a.get('headline', '')} {a.get('summary', '')}".lower()
            if not any(k in text for k in BUYBACK_KEYWORDS):
                continue
            key = f"buyback:{a.get('datetime')}:{a.get('url', '')[:60]}"
            for alert in alerts:
                if alert["type"] == "buyback_announced":
                    self._fire(alert, 0, f"Buyback headline: {a.get('headline')}",
                               key, fired)
        return fired

    def _check_news_spike(self, symbol: str, alerts: list[dict],
                          articles: list[dict]) -> list[int]:
        fired: list[int] = []
        counts = self._day_counts(articles)
        if not counts:
            return fired
        days = sorted(counts)
        if len(days) < 5:
            return fired
        today_count = counts[days[-1]]
        baseline = sorted(counts[d] for d in days[-21:-1])
        median = baseline[len(baseline) // 2]
        if today_count >= 3 and today_count >= 3 * max(median, 1):
            key = f"newsspike:{days[-1]}"
            for alert in alerts:
                if alert["type"] == "news_spike":
                    self._fire(alert, 0,
                               f"News spike: {today_count} articles today vs "
                               f"20-day median {median}", key, fired)
        return fired

    # --------------------------------------------------------------- earnings
    def _check_earnings(self, symbol: str, alerts: list[dict],
                        fired: list[int]) -> None:
        """Detect newly-reported quarters; record beat + report date."""
        r = requests.get(EARN_API, params={"symbol": symbol, "token": self.key},
                         timeout=30)
        r.raise_for_status()
        quarters = r.json() if isinstance(r.json(), list) else []
        for q in quarters:
            if q.get("actual") is None or q.get("estimate") is None:
                continue
            period = str(q.get("period", ""))
            seen_key = f"earn_seen:{symbol}"
            if self.store.poller_get(seen_key) == f"{period}:{q['actual']}":
                continue  # already processed this report
            beat = q["actual"] > q["estimate"]
            # record the report date as today (first day we see actuals)
            self.store.poller_set(seen_key, f"{period}:{q['actual']}")
            self.store.poller_set(f"earn_meta:{symbol}",
                                  f"{period}|{q['actual']}|{q['estimate']}|"
                                  f"{int(beat)}|{dt.date.today().isoformat()}")
            log.info("%s earnings %s: actual %s vs est %s (beat=%s)",
                     symbol, period, q["actual"], q["estimate"], beat)
        self._eval_sell_the_news(symbol, alerts, fired)

    def _eval_sell_the_news(self, symbol: str, alerts: list[dict],
                            fired: list[int]) -> None:
        """If the last report was a beat, check next-session move <= -3%."""
        import sessions as sess_mod
        meta = self.store.poller_get(f"earn_meta:{symbol}")
        if not meta:
            return
        period, actual, estimate, beat, report_date = meta.split("|")
        if beat != "1":
            return
        if self.daily_bars_fn is None:
            return
        try:
            bars = self.daily_bars_fn(symbol, 10)
        except Exception as e:  # noqa: BLE001
            log.warning("%s daily bars for sell-the-news failed: %s", symbol, e)
            return
        if not bars:
            return
        by_date = {dt.datetime.fromtimestamp(b["ts"], dt.timezone.utc)
                   .date().isoformat(): b for b in bars}
        nxt = sess_mod.next_trading_day(self.store, report_date, forward=True)
        if nxt is None or report_date not in by_date or \
                nxt.isoformat() not in by_date:
            return  # next session not in yet (or calendar gap)
        c0 = by_date[report_date]["c"]
        c1 = by_date[nxt.isoformat()]["c"]
        move = (c1 - c0) / c0 * 100 if c0 else 0
        if move <= -3.0:
            key = f"sellthenews:{period}"
            for alert in alerts:
                if alert["type"] == "sell_the_news":
                    self._fire(alert, c1,
                               f"Sell-the-news: beat {actual} vs {estimate}, "
                               f"next session {move:.1f}%", key, fired)
