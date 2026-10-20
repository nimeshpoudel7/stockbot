"""Live data everywhere: /api/prices for any symbol, today's bar merged
into the stock page / chart reading / daily candles, the scan reference
that explains list-vs-live differences, and snapshot parsing."""
import json
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import dashboard
import feed
from storage import Store
from tests import test_dashboard as td
from tests.test_dashboard import _endpoint

BASE = 1_700_000_000


def _bars(n=226):
    return [{"ts": BASE + i * 86400, "o": 50.0 + i * 0.3, "h": 52.0 + i * 0.3,
             "l": 48.0 + i * 0.3, "c": 50.0 + i * 0.3, "v": 1_000_000} for i in range(n)]


def _snap(price, *, same_day=False, n=226, prev=None):
    last_ts = BASE + (n - 1) * 86400
    ts = last_ts if same_day else last_ts + 86400
    return {"price": price, "prev_close": prev, "day_pct": 1.5, "ts": time.time(),
            "vwap": price * 0.99,
            "bar": {"ts": ts, "o": price - 1, "h": price + 1, "l": price - 2,
                    "c": price, "v": 400_000}}


class TestPrices(unittest.TestCase):
    def test_any_symbol_from_snapshots_cached(self):
        calls = []

        def snaps(syms):
            calls.append(list(syms))
            return {s: {"price": 10.0, "day_pct": -2.0, "ts": 1.0} for s in syms}
        app = dashboard.create_app(Store(":memory:"), MagicMock(), {}, {},
                                   extras={"snapshots": snaps})
        ep = _endpoint(app, "/api/prices")
        out = ep(symbols="abc, XYZ,bad1,")
        self.assertEqual(set(out), {"ABC", "XYZ"})
        self.assertEqual(out["ABC"]["day_pct"], -2.0)
        ep(symbols="ABC,XYZ")
        self.assertEqual(len(calls), 1)   # second call served from cache

    def test_fresh_websocket_tick_wins(self):
        st = MagicMock(price=101.0, prev_close=100.0, ts=time.time())
        snaps = MagicMock(return_value={})
        app = dashboard.create_app(Store(":memory:"), MagicMock(), {"AAA": st}, {},
                                   extras={"snapshots": snaps})
        out = _endpoint(app, "/api/prices")(symbols="AAA")
        self.assertAlmostEqual(out["AAA"]["price"], 101.0)
        self.assertAlmostEqual(out["AAA"]["day_pct"], 1.0)
        snaps.assert_not_called()

    def test_snapshot_failure_degrades_to_empty(self):
        def boom(_):
            raise RuntimeError("down")
        app = dashboard.create_app(Store(":memory:"), MagicMock(), {}, {},
                                   extras={"snapshots": boom})
        self.assertEqual(_endpoint(app, "/api/prices")(symbols="AAA"), {})


class TestLiveDaily(unittest.TestCase):
    def _app(self, snap, store=None):
        return dashboard.create_app(store or Store(":memory:"), MagicMock(), {}, {},
                                    extras={"daily_bars": lambda s, d: _bars(),
                                            "snapshots": lambda syms: {s: snap for s in syms}})

    def test_candles_append_today(self):
        out = _endpoint(self._app(_snap(130.0)), "/api/candles")(symbol="abc", tf="1m")
        self.assertEqual(out["bars"][-1]["close"], 130.0)
        self.assertEqual(out["bars"][-2]["close"], _bars()[-1]["c"])

    def test_candles_replace_same_day(self):
        out = _endpoint(self._app(_snap(130.0, same_day=True)), "/api/candles")(symbol="abc", tf="1m")
        self.assertEqual(out["bars"][-1]["close"], 130.0)
        self.assertEqual(out["bars"][-1]["time"], _bars()[-1]["ts"])
        self.assertEqual(out["bars"][-2]["close"], _bars()[-2]["c"])

    def test_analysis_uses_live_bar_and_flags_live(self):
        out = _endpoint(self._app(_snap(130.0)), "/api/analysis/{symbol}")("abc")
        self.assertTrue(out["live"])
        self.assertIn("updated", out)
        self.assertEqual(out["risk"]["price"], 130.0)

    def test_stock_price_from_snapshot_and_scan_ref(self):
        store = Store(":memory:")
        store.poller_set("scanner_morning", json.dumps({
            "ts": "2026-10-20T08:00:00-04:00",
            "buys": [{"symbol": "TESTA", "price": 117.0, "rating": "6.5/10",
                      "side": "BUY", "playbook": "dip", "tech_score": 65, "rsi": 27.0}],
            "sells": []}))
        t = td.TestApiStock()
        p_sent, p_fund, p_env = t._patched()
        with p_sent, p_fund, p_env, patch.object(dashboard.requests, "get",
                                                  return_value=t._profile_resp()):
            out = _endpoint(self._app(_snap(130.0), store), "/api/stock/{symbol}")(symbol="TESTA")
        self.assertEqual(out["price"]["last"], 130.0)
        self.assertTrue(out["price"]["live"])
        self.assertEqual(out["price"]["day_pct"], 1.5)
        ref = out["scan_ref"]
        self.assertEqual((ref["kind"], ref["side"], ref["playbook"], ref["rsi"]),
                         ("morning", "BUY", "dip", 27.0))
        self.assertIn("rsi", out["scanner"])
        self.assertIn("updated", out)

    def test_watch_entry_preferred_over_morning(self):
        store = Store(":memory:")
        store.poller_set("scanner_morning", json.dumps({"ts": "x", "buys": [{"symbol": "ABC", "side": "BUY"}], "sells": []}))
        store.poller_set("scanner_watch", json.dumps({"entries": [
            {"symbol": "ABC", "side": "SELL", "time": "2026-10-20T11:00:00-04:00"}]}))
        app = self._app(_snap(130.0), store)
        with patch.object(dashboard.sentiment_mod, "get_sentiment",
                          return_value=dict(td.TestApiStock._SENT)), \
                patch.object(dashboard.fundamentals, "get_scores", return_value={}), \
                patch.object(dashboard.requests, "get", side_effect=RuntimeError):
            out = _endpoint(app, "/api/stock/{symbol}")(symbol="ABC")
        self.assertEqual((out["scan_ref"]["kind"], out["scan_ref"]["side"]), ("watch", "SELL"))

    def test_no_snapshot_extra_keeps_cached_bars(self):
        app = dashboard.create_app(Store(":memory:"), MagicMock(), {}, {},
                                   extras={"daily_bars": lambda s, d: _bars()})
        out = _endpoint(app, "/api/candles")(symbol="abc", tf="1m")
        self.assertEqual(out["bars"][-1]["close"], _bars()[-1]["c"])
        out = _endpoint(app, "/api/analysis/{symbol}")("abc")
        self.assertFalse(out["live"])


class TestParseSnapshots(unittest.TestCase):
    def test_shape(self):
        data = {"NVDA": {"latestTrade": {"p": 217.0, "t": "2026-10-20T15:30:00.123Z"},
                         "dailyBar": {"t": "2026-10-20T04:00:00Z", "o": 216, "h": 219, "l": 215,
                                      "c": 216.9, "v": 1000, "vw": 217.4},
                         "prevDailyBar": {"c": 210.0}},
                "BAD": {"latestTrade": None}}
        out = feed.parse_snapshots(data)
        self.assertEqual(set(out), {"NVDA"})
        n = out["NVDA"]
        self.assertAlmostEqual(n["day_pct"], (217 - 210) / 210 * 100)
        self.assertEqual(n["vwap"], 217.4)
        self.assertEqual(n["bar"]["c"], 217.0)
        self.assertEqual(datetime.fromtimestamp(n["bar"]["ts"], timezone.utc).date().isoformat(), "2026-10-20")


if __name__ == "__main__":
    unittest.main()
