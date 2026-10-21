"""High-conviction strategy: top 5 leaders near their highs, separate from
Moderate risk (own keys, API, tab, plan)."""
import datetime as dt
import json
import unittest
from unittest.mock import MagicMock, patch

import conviction as CV
import dashboard
import moderate as M
import scanner
from storage import Store
from tests.test_moderate import Rest, _trend
from tests.test_scanner import _patch_sentiment


class TestConvictionList(unittest.TestCase):
    def test_near_high_and_top5(self):
        rows = [M.row(f"S{i}", _trend(50 + 10 * i)) for i in range(7)]
        rows.append(M.row("FAR", _trend(300, last_month_pct=-12)))   # >10% off its high
        out = CV.build(rows, [100.0] * 200)
        syms = [r["symbol"] for r in out["picks"]]
        self.assertEqual(syms, ["S6", "S5", "S4", "S3", "S2"])
        self.assertNotIn("FAR", syms)
        cash = CV.build(rows, [100.0] * 199 + [90.0])
        self.assertEqual(cash["picks"], [])
        self.assertEqual(len(cash["watch"]), 5)

    def test_separate_keys_and_shared_members(self):
        self.assertNotEqual(CV.LIST_KEY, M.LIST_KEY)
        self.assertNotEqual(CV.LOG_KEY, M.LOG_KEY)
        self.assertIs(CV.sp500_members, M.sp500_members)

    def test_backtest_beats_moderate_on_paper(self):
        b = CV.BACKTEST
        self.assertGreater(b["test"]["hit"], b["moderate_test"]["hit"])
        self.assertGreater(b["test"]["year"], b["moderate_test"]["year"])


class TestConvictionJob(unittest.TestCase):
    def test_run_and_plan(self):
        store = Store(":memory:")
        bars = {"AAA": _trend(90), "BBB": _trend(40), "SPY": _trend(15)}
        with patch.object(CV, "sp500_members", return_value=["AAA", "BBB"]):
            res = CV.run(store, Rest(bars))
        self.assertEqual([r["symbol"] for r in res["picks"]], ["AAA", "BBB"])
        self.assertIsNone(store.poller_get(M.LIST_KEY))       # moderate untouched
        self.assertEqual(CV.plan(store, "AAA")["action"], "buy")
        routes = {r.path: r.endpoint for r in dashboard.create_app(store, MagicMock(), {}, {}).routes
                  if hasattr(r, "path")}
        self.assertEqual(routes["/api/conviction"]()["list"]["picks"][0]["symbol"], "AAA")
        self.assertEqual(routes["/api/conviction/{symbol}"]("AAA")["plan"]["rank"], 1)
        self.assertIsNone(routes["/api/moderate"]()["list"])

    def test_morning_scan_builds_both(self):
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
                patch.object(scanner.time, "sleep", lambda *_: None), _patch_sentiment():
            scanner.run_scan(store=store, rest=Rest(bars), dry_run=False,
                             push_fn=MagicMock(return_value=True))
        self.assertIsNotNone(store.poller_get(M.LIST_KEY))
        self.assertEqual(json.loads(store.poller_get(CV.LIST_KEY))["picks"][0]["symbol"], "AAPL")

    def test_pages(self):
        html = open("web/index.html").read()
        self.assertIn('data-view="conviction"', html)
        self.assertIn("/api/conviction", html)
        self.assertIn("/api/${STRAT}/", open("web/stock.html").read())


if __name__ == "__main__":
    unittest.main()
