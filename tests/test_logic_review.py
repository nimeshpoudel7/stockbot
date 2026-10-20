"""Regression tests for the Oct 20, 2026 logic review fixes.

Each test pins the CORRECT behaviour for a bug that was found and fixed.
"""
import asyncio
import datetime as dt
import json
import random
import unittest
from unittest.mock import MagicMock, patch

import backtest
import event_alerts
import feed
import fundamentals
import predict
import regime
import scanner
from feed import FeedManager
from storage import Store


def bars_from(closes, vol=1_000_000.0, start_ts=1700000000):
    return [{"ts": start_ts + i * 86400, "o": c, "h": c * 1.005,
             "l": c * 0.995, "c": c, "v": vol} for i, c in enumerate(closes)]


def _day_ts(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, 4,
                           tzinfo=dt.timezone.utc).timestamp())


class TestDailyStats(unittest.TestCase):
    def _bars(self, dates_closes):
        return [{"ts": _day_ts(d), "o": c, "h": c + 1, "l": c - 1, "c": c,
                 "v": 1} for d, c in dates_closes]

    def test_prev_close_skips_todays_partial_bar(self):
        today = dt.date(2026, 10, 20)
        bars = self._bars([(dt.date(2026, 10, 16), 99.0),
                           (dt.date(2026, 10, 19), 100.0),
                           (today, 104.0)])  # forming session
        st = feed.daily_stats(bars, today.isoformat())
        self.assertEqual(st["prev_close"], 100.0)
        self.assertEqual(st["high_52w"], 101.0)  # today's 105 excluded
        self.assertEqual(st["daily_closes"], [99.0, 100.0])

    def test_prev_close_before_open(self):
        today = dt.date(2026, 10, 20)
        bars = self._bars([(dt.date(2026, 10, 16), 99.0),
                           (dt.date(2026, 10, 19), 100.0)])
        self.assertEqual(feed.daily_stats(bars, today.isoformat())
                         ["prev_close"], 100.0)

    def test_52w_window_is_252_sessions(self):
        start = dt.date(2025, 1, 1)
        rows = [(start + dt.timedelta(days=i), 100.0) for i in range(300)]
        rows[10] = (rows[10][0], 500.0)  # older than 252 sessions
        st = feed.daily_stats(self._bars(rows), "2030-01-01")
        self.assertEqual(st["high_52w"], 101.0)

    def test_warmup_uses_completed_sessions(self):
        f = MagicMock()
        f.has_credentials.return_value = True
        f.max_symbols, f.name = 30, "alpaca"
        f.get_1m_bars.return_value = []
        today = dt.datetime.now(dt.timezone.utc).date()
        f.get_daily_bars.return_value = self._bars(
            [(today - dt.timedelta(days=1), 100.0), (today, 104.0)])
        m = FeedManager([f])
        m.assign(["AAPL"])
        with patch.object(feed, "_et_today", return_value=today.isoformat()):
            w = asyncio.run(m.warmup("AAPL"))
        self.assertEqual(w["prev_close"], 100.0)


class TestAlpacaBars(unittest.TestCase):
    def test_1m_warmup_requests_newest_bars(self):
        a = feed.AlpacaFeed("k", "s")
        payload = {"bars": {"X": [
            {"t": "2026-10-20T14:02:00Z", "o": 1, "h": 1, "l": 1, "c": 3},
            {"t": "2026-10-20T14:01:00Z", "o": 1, "h": 1, "l": 1, "c": 2}]}}
        with patch.object(a, "_rest", return_value=payload) as rest:
            out = a.get_1m_bars("X", limit=2)
        self.assertEqual(rest.call_args[0][1]["sort"], "desc")
        self.assertEqual([b["c"] for b in out], [2, 3])  # oldest-first

    def test_daily_bars_follow_pagination(self):
        a = feed.AlpacaFeed("k", "s")
        p1 = {"bars": {"X": [{"t": "2026-10-16T04:00:00Z", "o": 1, "h": 1,
                              "l": 1, "c": 1}]}, "next_page_token": "t"}
        p2 = {"bars": {"X": [{"t": "2026-10-19T04:00:00Z", "o": 2, "h": 2,
                              "l": 2, "c": 2}]}}
        with patch.object(a, "_rest", side_effect=[p1, p2]):
            out = a.get_daily_bars("X", 10)
        self.assertEqual([b["c"] for b in out], [1, 2])


class TestRegimeRefresh(unittest.TestCase):
    def test_refetches_spy_once_per_day(self):
        store = Store(":memory:")
        store.candles_save("SPY", "1d",
                           bars_from([100 + i * 0.1 for i in range(400)]))
        fresh = bars_from([100 + i * 0.1 for i in range(399)] + [10.0])
        fetch = MagicMock(return_value=fresh)
        rc = regime.RegimeCache(store, fetch)
        r = rc.refresh()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(r["price"], 10.0)
        # same day, new process: no second fetch
        regime.RegimeCache(store, fetch).refresh()
        self.assertEqual(fetch.call_count, 1)

    def test_cached_never_fetches(self):
        fetch = MagicMock()
        rc = regime.RegimeCache(Store(":memory:"), fetch)
        self.assertIsNone(rc.cached())
        fetch.assert_not_called()


class TestEarningsLatestQuarter(unittest.TestCase):
    def test_latest_quarter_recorded(self):
        store = Store(":memory:")
        p = event_alerts.EventPoller(store, MagicMock(), "k",
                                     daily_bars_fn=lambda s, d: [])
        resp = MagicMock()
        resp.json.return_value = [
            {"period": "2026-03-31", "actual": 0.9, "estimate": 1.0},
            {"period": "2026-09-30", "actual": 2.0, "estimate": 1.5},
            {"period": "2026-06-30", "actual": 1.0, "estimate": 1.2},
        ]
        with patch.object(event_alerts.requests, "get", return_value=resp):
            p._check_earnings("T", [], [])
        meta = store.poller_get("earn_meta:T").split("|")
        self.assertEqual(meta[0], "2026-09-30")
        self.assertEqual(meta[3], "1")  # beat


class TestNewsSpikeToday(unittest.TestCase):
    def _poller(self):
        engine = MagicMock()
        engine.fire_external.return_value = True
        return event_alerts.EventPoller(Store(":memory:"), engine, "k")

    def _arts(self, per_day: dict):
        out = []
        for d, n in per_day.items():
            ts = int(dt.datetime.combine(d, dt.time(12),
                                         dt.timezone.utc).timestamp())
            out += [{"datetime": ts}] * n
        return out

    def test_old_spike_does_not_fire_today(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        days = {today - dt.timedelta(days=k): 1 for k in range(3, 12)}
        days[today - dt.timedelta(days=2)] = 9  # spike, two days ago
        p = self._poller()
        fired = p._check_news_spike(
            "T", [{"id": 1, "type": "news_spike"}], self._arts(days))
        self.assertEqual(fired, [])

    def test_today_spike_fires(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        days = {today - dt.timedelta(days=k): 1 for k in range(1, 12)}
        days[today] = 6
        p = self._poller()
        fired = p._check_news_spike(
            "T", [{"id": 1, "type": "news_spike"}], self._arts(days))
        self.assertEqual(fired, [1])


class TestSideRatings(unittest.TestCase):
    def test_perfect_buy_reads_ten(self):
        self.assertEqual(
            scanner.side_rating(scanner.BULL_TECH_MAX, "BUY", "watch"),
            "10.0/10")

    def test_sides_comparable(self):
        # half of each side's scale reads the same
        self.assertEqual(scanner.side_rating(14, "BUY", "watch"),
                         scanner.side_rating(50, "SELL", "watch"))

    def test_bad_news_strengthens_sell(self):
        base = float(scanner.side_rating(60, "SELL", "morning").split("/")[0])
        bad = float(scanner.side_rating(60, "SELL", "morning", -1.0)
                    .split("/")[0])
        good = float(scanner.side_rating(60, "SELL", "morning", 1.0)
                     .split("/")[0])
        self.assertGreater(bad, base)
        self.assertLess(good, base)

    def test_buy_bars(self):
        # uptrend + breakout (20) reaches the morning push...
        self.assertLessEqual(scanner._morning_bar("BUY", None), 20)
        # ...but a single setup (max 11) no longer pushes in watch mode
        self.assertGreater(scanner._watch_bar("BUY", None),
                           max(scanner.WEIGHTS.values()))


class TestPiotroskiCoverage(unittest.TestCase):
    def test_missing_data_is_neutral(self):
        pio = {"score": 2, "evaluated": 3}
        with patch.object(fundamentals, "get_scores", return_value={
                "piotroski": pio, "altman": None, "dcf": {}}):
            self.assertEqual(scanner.fundamental_bonus(
                Store(":memory:"), "T", 10.0), (0, []))
            self.assertEqual(scanner.short_bonus(
                Store(":memory:"), "T", 10.0), (0, []))

    def test_full_data_still_scores(self):
        pio = {"score": 2, "evaluated": 9}
        with patch.object(fundamentals, "get_scores", return_value={
                "piotroski": pio, "altman": None, "dcf": {}}):
            bonus, _ = scanner.fundamental_bonus(Store(":memory:"), "T", 10.0)
        self.assertEqual(bonus, -10)


class TestWatchSideEffects(unittest.TestCase):
    def _run(self, store, dry_run, push):
        closes = [100.0] * 245 + [160.0] * 5
        bars = bars_from(closes)
        bars[-1]["v"] = 5_000_000.0
        rest = MagicMock()
        rest.daily_bars_multi.return_value = {"TESTA": bars}
        neutral = {"sentiment": 0.0, "n": 0, "analyst_counts": {"total": 0}}
        with patch.object(scanner, "_in_market_hours", return_value=True), \
                patch.object(scanner, "get_universe", return_value=["TESTA"]), \
                patch.object(scanner, "_current_regime", return_value=None), \
                patch.object(scanner, "earnings_note", return_value=None), \
                patch.object(scanner.sentiment, "get_sentiment",
                             return_value=neutral):
            return scanner.watch_scan(store=store, rest=rest,
                                      dry_run=dry_run, push_fn=push)

    def _seen(self, store):
        raw = store.poller_get(scanner.WATCH_SEEN_KEY)
        return json.loads(raw)["symbols"] if raw else []

    def test_dry_run_leaves_seen_list(self):
        store = Store(":memory:")
        self.assertTrue(self._run(store, True, None))
        self.assertEqual(self._seen(store), [])

    def test_failed_push_retries_next_pass(self):
        store = Store(":memory:")
        self._run(store, False, MagicMock(return_value=False))
        self.assertEqual(self._seen(store), [])
        push = MagicMock(return_value=True)
        self.assertTrue(self._run(store, False, push))
        push.assert_called_once()
        self.assertEqual(self._seen(store), ["TESTA|BUY"])


class TestMorningOneSidePerSymbol(unittest.TestCase):
    def test_symbol_not_pushed_on_both_sides(self):
        def screen(bars, volume_scale=1.0):
            # strong on both sides; bull fraction 28/28 > bear 60/100
            return {"price": 50.0, "tech_score": 28,
                    "setups": [("uptrend", 28, "u")], "bear_score": 60,
                    "bear_setups": [("x", 60, "x")], "indicators": {}}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = {"T": bars_from([50.0] * 250)}
        neutral = {"sentiment": 0.0, "n": 0, "analyst_counts": {"total": 0}}
        with patch.object(scanner.sessions_mod, "is_trading_day",
                          return_value=True), \
                patch.object(scanner, "get_universe", return_value=["T"]), \
                patch.object(scanner, "technical_screen", side_effect=screen), \
                patch.object(scanner, "passes_liquidity", return_value=True), \
                patch.object(scanner, "fundamental_bonus",
                             return_value=(0, [])), \
                patch.object(scanner, "short_bonus", return_value=(0, [])), \
                patch.object(scanner, "_current_regime", return_value=None), \
                patch.object(scanner, "earnings_note", return_value=None), \
                patch.object(scanner.sentiment, "get_sentiment",
                             return_value=neutral), \
                patch.object(scanner.time, "sleep", lambda *_: None):
            out = scanner.run_scan(store=Store(":memory:"), rest=rest,
                                   dry_run=False,
                                   push_fn=MagicMock(return_value=True))
        self.assertEqual([c["symbol"] for c in out["buys"]], ["T"])
        self.assertEqual(out["sells"], [])


class TestBacktestSmaCross(unittest.TestCase):
    def test_single_cross_fires_once(self):
        closes = [100.0] * 60 + [100.0 + i for i in range(1, 61)]
        fires = backtest._fires("sma_cross", bars_from(closes),
                                {"fast": 5, "slow": 20})
        self.assertFalse(any(b == a + 1 for a, b in zip(fires, fires[1:])),
                         fires)


class TestPredictLatest(unittest.TestCase):
    def _bars(self, n=80):
        random.seed(1)
        closes, c = [], 100.0
        for _ in range(n):
            c *= 1 + random.uniform(-0.02, 0.02)
            closes.append(c)
        bars = bars_from(closes)
        for i, b in enumerate(bars):
            b["v"] = 1_000_000 + (i % 7) * 50_000
        return bars

    def test_latest_features_are_last_bar(self):
        bars = self._bars()
        feats = predict.build_features(bars)
        self.assertEqual(predict.latest_features(bars),
                         [float(feats[-1][f]) for f in predict.FEATURES])

    def test_walk_forward_embargo(self):
        tr, te = next(predict.walk_forward_splits(400))
        self.assertGreaterEqual(te[0] - tr[-1], predict.HORIZON + 1)


class TestDilutionLikeForLike(unittest.TestCase):
    def test_piotroski_uses_statement_shares(self):
        def ann(year, shares):
            return {"year": year, "endDate": f"{year}-12-31",
                    "form": "10-K", "report": {"bs": [], "ic": [
                        {"concept": "us-gaap_WeightedAverageNumberOf"
                                    "DilutedSharesOutstanding",
                         "label": "Diluted", "value": shares}], "cf": []}}
        store = Store(":memory:")
        with patch.object(fundamentals, "fetch_annuals",
                          return_value=[ann(2025, 100), ann(2024, 100)]), \
                patch.object(fundamentals, "fetch_profile",
                             return_value={"shareOutstanding": 0.0002}):
            out = fundamentals.get_scores(store, "T", price=10.0, force=True)
        # profile says 200 shares today vs 100 weighted last year; the
        # statement figures (100 vs 100) are the like-for-like comparison
        self.assertTrue(out["piotroski"]["tests"]["no_dilution"])
        self.assertEqual(out["shares_outstanding"], 200.0)


class TestDcfNetDebt(unittest.TestCase):
    def test_subtracts_debt_not_operating_liabilities(self):
        cur = {"fcf": 100.0, "shares": 10.0, "total_liab": 1000.0,
               "lt_debt": 200.0, "cash": 50.0}
        a = fundamentals.dcf_fair_value(cur, None)
        b = fundamentals.dcf_fair_value(dict(cur, total_liab=5000.0), None)
        self.assertAlmostEqual(a["fair_value"], b["fair_value"])


class TestNoBlockingNotify(unittest.TestCase):
    def test_engine_tick_path_uses_cached_regime(self):
        import inspect
        import main
        src = inspect.getsource(main.amain)
        self.assertIn("engine.regime_fn = regime_cache.cached", src)
        self.assertIn("push_pool.submit(send_ntfy", src)


if __name__ == "__main__":
    unittest.main()
