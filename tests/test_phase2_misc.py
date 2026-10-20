"""Tests for regime.py, sessions.py, predict.py walk-forward, backtest.py,
and the event poller — all on synthetic data, no network."""
import datetime as dt
import unittest

import regime as R
import sessions as S
from storage import Store


class TestRegime(unittest.TestCase):
    def test_bull(self):
        closes = [100 + i * 0.5 for i in range(250)]  # steady climb
        r = R.compute_regime(closes)
        self.assertEqual(r["regime"], "bull")
        self.assertGreater(r["slope_pct"], 0)

    def test_bear(self):
        closes = [200 - i * 0.5 for i in range(250)]
        r = R.compute_regime(closes)
        self.assertEqual(r["regime"], "bear")

    def test_sideways(self):
        closes = [100 + ((-1) ** i) * 2 for i in range(250)]  # oscillates
        r = R.compute_regime(closes)
        self.assertEqual(r["regime"], "sideways")

    def test_not_enough_data(self):
        self.assertIsNone(R.compute_regime([100.0] * 100))


class TestSessions(unittest.TestCase):
    def test_fetch_marks_absent_days_closed(self):
        # /v2/calendar omits weekends/holidays; they must become is_open=False
        import sessions as S2

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"date": "2026-10-19", "open": "09:30",
                         "close": "16:00"}]

        import requests
        import os
        real_get = requests.get
        real_env = (os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET"))
        os.environ["ALPACA_KEY"] = "x"
        os.environ["ALPACA_SECRET"] = "y"
        requests.get = lambda *a, **k: Resp()
        try:
            days = S2.fetch_calendar(dt.date(2026, 10, 17), dt.date(2026, 10, 19))
        finally:
            requests.get = real_get
            if real_env[0] is None:
                os.environ.pop("ALPACA_KEY", None)
            else:
                os.environ["ALPACA_KEY"] = real_env[0]
            if real_env[1] is None:
                os.environ.pop("ALPACA_SECRET", None)
            else:
                os.environ["ALPACA_SECRET"] = real_env[1]
        by_date = {d["date"]: d for d in days}
        self.assertEqual(len(days), 3)
        self.assertFalse(by_date["2026-10-17"]["is_open"])  # Saturday
        self.assertFalse(by_date["2026-10-18"]["is_open"])  # Sunday
        self.assertTrue(by_date["2026-10-19"]["is_open"])   # Monday

    def test_next_trading_day_skips_weekend(self):
        store = Store(":memory:")
        # 2026-10-19 is a Monday (per nearby-dates context)
        days = [{"date": "2026-10-16", "is_open": True, "open": "09:30",
                 "close": "16:00"},   # Friday
                {"date": "2026-10-17", "is_open": False, "open": None,
                 "close": None},      # Saturday
                {"date": "2026-10-18", "is_open": False, "open": None,
                 "close": None},      # Sunday
                {"date": "2026-10-19", "is_open": True, "open": "09:30",
                 "close": "16:00"}]   # Monday
        store.calendar_save(days)
        self.assertTrue(S.is_trading_day(store, dt.date(2026, 10, 16)))
        self.assertFalse(S.is_trading_day(store, dt.date(2026, 10, 17)))
        self.assertIsNone(S.is_trading_day(store, dt.date(2026, 10, 20)))
        nxt = S.next_trading_day(store, dt.date(2026, 10, 16))
        self.assertEqual(nxt, dt.date(2026, 10, 19))
        prev = S.next_trading_day(store, dt.date(2026, 10, 19), forward=False)
        self.assertEqual(prev, dt.date(2026, 10, 16))
        # gap in cache -> None, never a guess
        self.assertIsNone(S.next_trading_day(store, dt.date(2026, 10, 19)))


class TestWalkForward(unittest.TestCase):
    def test_splits(self):
        import predict as P
        splits = list(P.walk_forward_splits(60, train=30, test=10))
        self.assertEqual(len(splits), 3)
        tr, te = splits[0]
        self.assertEqual((tr[0], tr[-1], te[0], te[-1]), (0, 29, 30, 39))
        tr, te = splits[2]
        self.assertEqual((tr[0], tr[-1], te[0], te[-1]), (20, 49, 50, 59))

    def test_no_splits_when_short(self):
        import predict as P
        self.assertEqual(list(P.walk_forward_splits(35, train=30, test=10)),
                         [])

    def test_walk_forward_separable(self):
        import predict as P
        # parity: every train window sees both classes, trivially separable
        X = [[i % 2] for i in range(200)]
        y = [i % 2 for i in range(200)]
        wf = P.walk_forward(X, y, train=100, test=20)
        self.assertGreater(wf["splits"], 0)
        self.assertGreaterEqual(wf["hit_rate"], 0.9)

    def test_dataset_drops_warmup(self):
        import predict as P
        bars = [{"ts": i * 86400, "o": 100, "h": 101, "l": 99,
                 "c": 100 + (i % 5), "v": 1000 + (i % 7) * 13}
                for i in range(60)]
        X, y = P.dataset(bars)
        self.assertGreater(len(X), 0)
        self.assertEqual(len(X), len(y))
        self.assertEqual(len(X[0]), len(P.FEATURES))
        self.assertTrue(all(v in (0, 1) for v in y))


class TestBacktest(unittest.TestCase):
    def _bars(self, closes):
        return [{"ts": i * 86400, "o": c, "h": c * 1.01, "l": c * 0.99,
                 "c": c, "v": 1000} for i, c in enumerate(closes)]

    def test_rsi_fires_on_spike(self):
        import backtest as B
        # decline (RSI sinks) then sharp rally -> RSI crosses up through 70
        closes = [120 - i for i in range(25)] + [95 + i * 4 for i in range(1, 16)]
        fires = B._fires("rsi_14", self._bars(closes), {})
        self.assertGreater(len(fires), 0)

    def test_outcome_target_first(self):
        import backtest as B
        # fire at idx 10; next bar gaps +6% -> target first
        closes = [100.0] * 11 + [106.0] * 25
        oc = B._outcome(self._bars(closes), 10)
        self.assertTrue(oc["target_first"])
        self.assertAlmostEqual(oc["move"], 6.0)

    def test_outcome_stop_first(self):
        import backtest as B
        closes = [100.0] * 11 + [96.0] * 25
        oc = B._outcome(self._bars(closes), 10)
        self.assertFalse(oc["target_first"])
        self.assertAlmostEqual(oc["move"], -4.0)

    def test_outcome_undecided(self):
        import backtest as B
        closes = [100.0] * 40  # flat: neither target nor stop
        oc = B._outcome(self._bars(closes), 10)
        self.assertIsNone(oc["target_first"])

    def test_run_backtest_shape(self):
        import backtest as B
        closes = [100 + (i % 7) - 3 + i * 0.05 for i in range(300)]
        bars = self._bars(closes)
        res = B.run_backtest(["AAA"], lambda s, d: bars,
                             types=["sma_cross", "day_pct_change"], days=365)
        self.assertIn("sma_cross", res)
        for t, st in res.items():
            self.assertIn("fires", st)
            self.assertIn("target_first_rate", st)
            self.assertIn("avg_move_pct", st)
            self.assertIn("worst_move_pct", st)


class TestEventPoller(unittest.TestCase):
    def _poller(self):
        import event_alerts as E
        store = Store(":memory:")

        class FakeEngine:
            def __init__(self):
                self._alerts = {}
                self.fired = []

            def refresh_alerts(self):
                pass

            def fire_external(self, aid, price, detail, ts, key):
                self.fired.append((aid, key))
                return True

        eng = FakeEngine()
        return E.EventPoller(store, eng, "dummy"), store, eng

    def test_split_keyword(self):
        poller, store, eng = self._poller()
        alerts = [{"id": 1, "type": "split_announced", "symbol": "AAA"}]
        arts = [{"headline": "Acme announces 4-for-1 stock split",
                 "summary": "", "datetime": 1_700_000_000,
                 "url": "http://x/1"},
                {"headline": "Acme earnings beat estimates",
                 "summary": "", "datetime": 1_700_000_000,
                 "url": "http://x/2"}]
        import time
        # make articles "recent"
        for a in arts:
            a["datetime"] = int(time.time()) - 3600
        fired = poller._check_splits("AAA", alerts, arts)
        self.assertEqual(fired, [1])
        self.assertEqual(eng.fired[0][1].split(":")[0], "split")

    def test_buyback_keyword(self):
        poller, store, eng = self._poller()
        alerts = [{"id": 2, "type": "buyback_announced", "symbol": "AAA"}]
        import time
        arts = [{"headline": "Board authorizes $1B share repurchase program",
                 "summary": "", "datetime": int(time.time()) - 3600,
                 "url": "http://x/3"}]
        fired = poller._check_buybacks("AAA", alerts, arts)
        self.assertEqual(fired, [2])

    def test_news_spike(self):
        poller, store, eng = self._poller()
        alerts = [{"id": 3, "type": "news_spike", "symbol": "AAA"}]
        import time
        now = int(time.time())
        arts = []
        # 20 quiet days: 1 article each; today: 10 articles
        for d in range(21):
            n = 10 if d == 20 else 1
            for k in range(n):
                arts.append({"headline": f"news {d}-{k}", "summary": "",
                             "datetime": now - (20 - d) * 86400,
                             "url": f"http://x/{d}-{k}"})
        fired = poller._check_news_spike("AAA", alerts, arts)
        self.assertEqual(fired, [3])

    def test_no_spike_when_flat(self):
        poller, store, eng = self._poller()
        alerts = [{"id": 3, "type": "news_spike", "symbol": "AAA"}]
        import time
        now = int(time.time())
        arts = [{"headline": "x", "summary": "",
                 "datetime": now - d * 86400, "url": f"http://x/{d}"}
                for d in range(21)]
        self.assertEqual(poller._check_news_spike("AAA", alerts, arts), [])

    def test_sell_the_news(self):
        poller, store, eng = self._poller()
        alerts = [{"id": 4, "type": "sell_the_news", "symbol": "AAA"}]
        # report date 2026-10-16 (Fri), next session 2026-10-19 (Mon)
        store.poller_set("earn_meta:AAA", "2026Q3|2.5|2.0|1|2026-10-16")
        for d, open_ in (("2026-10-16", True), ("2026-10-17", False),
                         ("2026-10-18", False), ("2026-10-19", True)):
            store.calendar_save([{"date": d, "is_open": open_,
                                  "open": "09:30", "close": "16:00"}])
        bars = [
            {"ts": int(dt.datetime(2026, 10, 16, 20, tzinfo=dt.timezone.utc)
                       .timestamp()), "o": 100, "h": 101, "l": 99,
             "c": 100, "v": 1},
            {"ts": int(dt.datetime(2026, 10, 19, 20, tzinfo=dt.timezone.utc)
                       .timestamp()), "o": 97, "h": 98, "l": 95,
             "c": 96, "v": 1},  # -4% next session
        ]
        poller.daily_bars_fn = lambda s, d: bars
        fired: list[int] = []
        poller._eval_sell_the_news("AAA", alerts, fired)
        self.assertEqual(fired, [4])
        self.assertEqual(eng.fired[0][0], 4)  # alert id recorded

    def test_sell_the_news_no_beat_no_fire(self):
        poller, store, eng = self._poller()
        alerts = [{"id": 4, "type": "sell_the_news", "symbol": "AAA"}]
        store.poller_set("earn_meta:AAA", "2026Q3|1.5|2.0|0|2026-10-16")
        poller.daily_bars_fn = lambda s, d: []
        fired: list[int] = []
        poller._eval_sell_the_news("AAA", alerts, fired)
        self.assertEqual(fired, [])


if __name__ == "__main__":
    unittest.main()
