"""Leaders 3 months: top 10 by 3-month return among uptrend S&P stocks,
no cash rule, separate list/log/API/tab/plan."""
import json
import unittest
from unittest.mock import MagicMock, patch

import conviction as CV
import dashboard
import leaders3m as L3
import moderate as M
import scanner
from storage import Store
from tests.test_moderate import Rest, _trend
from tests.test_scanner import _patch_sentiment


class TestLeadersList(unittest.TestCase):
    def test_ranks_by_3_month_return(self):
        # same 12-month trend, different last-3-month strength
        rows = [L3.row("SLOW", _trend(60, last_month_pct=1)),
                L3.row("FAST", _trend(30, last_month_pct=15)),
                L3.row("DOWN", _trend(80, last_month_pct=-45))]
        out = L3.build(rows)
        self.assertEqual([r["symbol"] for r in out["picks"]][:2], ["FAST", "SLOW"])
        self.assertNotIn("DOWN", [r["symbol"] for r in out["picks"]])
        self.assertIn("ret3m", out["picks"][0])
        self.assertFalse(out["risk_off"])                  # no cash rule

    def test_top10_and_no_cash_even_when_spy_weak(self):
        rows = [L3.row(f"S{i}", _trend(20 + 5 * i)) for i in range(12)]
        out = L3.build(rows, [100.0] * 199 + [80.0])
        self.assertEqual(len(out["picks"]), 10)

    def test_backtest_block(self):
        b = L3.BACKTEST["long"]
        self.assertGreater(b["year"], b["spy_year"])
        self.assertEqual(len(L3.BACKTEST["by_year"]), 11)


class TestLeadersJob(unittest.TestCase):
    def test_run_api_plan_and_isolation(self):
        store = Store(":memory:")
        bars = {"AAA": _trend(90), "BBB": _trend(40), "SPY": _trend(15)}
        with patch.object(L3, "sp500_members", return_value=["AAA", "BBB"]):
            res = L3.run(store, Rest(bars))
        self.assertEqual(len(res["picks"]), 2)
        self.assertIsNone(store.poller_get(M.LIST_KEY))
        self.assertIsNone(store.poller_get(CV.LIST_KEY))
        routes = {r.path: r.endpoint for r in dashboard.create_app(store, MagicMock(), {}, {}).routes
                  if hasattr(r, "path")}
        self.assertEqual(len(routes["/api/leaders3m"]()["list"]["picks"]), 2)
        sym = res["picks"][0]["symbol"]
        self.assertEqual(routes["/api/leaders3m/{symbol}"](sym)["plan"]["action"], "buy")

    def test_morning_scan_builds_all_three(self):
        store = Store(":memory:")
        bars = {"AAPL": _trend(80), "SPY": _trend(15)}
        with patch.object(scanner.sessions_mod, "is_trading_day", return_value=True), \
                patch.object(scanner, "get_universe", return_value=["AAPL"]), \
                patch.object(scanner, "_current_regime", return_value=None), \
                patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
                patch.object(scanner, "short_bonus", return_value=(0, [])), \
                patch.object(scanner, "earnings_note", return_value=None), \
                patch.object(M, "sp500_members", return_value=["AAPL"]), \
                patch.object(CV, "sp500_members", return_value=["AAPL"]), \
                patch.object(L3, "sp500_members", return_value=["AAPL"]), \
                patch.object(scanner.time, "sleep", lambda *_: None), _patch_sentiment():
            scanner.run_scan(store=store, rest=Rest(bars), dry_run=False,
                             push_fn=MagicMock(return_value=True))
        for key in (M.LIST_KEY, CV.LIST_KEY, L3.LIST_KEY):
            self.assertIsNotNone(store.poller_get(key), key)
        self.assertEqual(json.loads(store.poller_get(L3.LIST_KEY))["picks"][0]["symbol"], "AAPL")

    def test_pages(self):
        html = open("web/index.html").read()
        self.assertIn('data-view="leaders"', html)
        self.assertIn("Leaders 3 months", html)
        self.assertIn("/api/leaders3m", html)
        self.assertIn("leaders3m", open("web/stock.html").read())


if __name__ == "__main__":
    unittest.main()


class TestPriceGuide(unittest.TestCase):
    def test_plan_has_price_guide_for_all_three(self):
        import datetime as dt
        for mod in (M, CV, L3):
            store = Store(":memory:")
            today = dt.datetime.now(mod.ET).date().isoformat()
            store.poller_set(mod.LIST_KEY, json.dumps({"date": today, "risk_off": False,
                                                       "picks": [{"symbol": "AAA", "rank": 1, "price": 100.0}], "watch": []}))
            mod.record(store, [{"symbol": "AAA", "price": 100.0, "rank": 1, "mom12": 50, "ret3m": 20}], today)
            g = mod.plan(store, "AAA")["price_guide"]
            oc = mod.BACKTEST["outcomes"]
            self.assertEqual(g["median"], round(100 * (1 + oc["median"] / 100), 2))
            self.assertLess(g["p10"], g["p25"])
            self.assertLess(g["p25"], g["median"])
            self.assertLess(g["median"], g["p75"])
            self.assertLess(g["p75"], g["p90"])
