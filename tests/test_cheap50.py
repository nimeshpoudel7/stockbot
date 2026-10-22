"""Under $50: top 5 S&P stocks priced $3-$50 by 6-1 momentum; separate
list/log/API/tab; no trend filter, no cash rule."""
import json
import unittest
from unittest.mock import MagicMock, patch

import cheap50 as C5
import dashboard
import leaders3m as L3
import moderate as M
from storage import Store
from tests.test_moderate import Rest, _trend


def _scaled(total, last_month=2.0, price=None):
    bars = _trend(total, last_month_pct=last_month)
    if price:
        f = price / bars[-1]["c"]
        for b in bars:
            for k in ("o", "h", "l", "c"):
                b[k] *= f
    return bars


class TestCheapList(unittest.TestCase):
    def test_price_cap_and_6m_rank(self):
        rows = [C5.row("EXP", _scaled(80, price=120.0)),      # over $50: out
                C5.row("A", _scaled(80, price=40.0)),
                C5.row("B", _scaled(30, price=20.0)),
                C5.row("DIP", _scaled(60, last_month=-30, price=25.0))]   # no trend filter
        self.assertIsNone(rows[0])
        out = C5.build(rows, [100.0] * 199 + [80.0])            # SPY weak: still invested
        syms = [r["symbol"] for r in out["picks"]]
        self.assertEqual(syms[0], "A")
        self.assertIn("DIP", syms)
        self.assertFalse(out["risk_off"])
        self.assertTrue(all("mom6" in r for r in out["picks"]))

    def test_top5(self):
        rows = [C5.row(f"S{i}", _scaled(20 + 5 * i, price=30.0)) for i in range(8)]
        self.assertEqual(len(C5.build(rows)["picks"]), 5)

    def test_backtest_numbers(self):
        b = C5.BACKTEST
        self.assertEqual(len(b["by_year"]), 11)
        self.assertLess(b["outcomes"]["p10"], b["outcomes"]["median"])


class TestCheapJob(unittest.TestCase):
    def test_run_api_plan_isolated(self):
        store = Store(":memory:")
        bars = {"AAA": _scaled(90, price=30.0), "BIG": _scaled(90, price=300.0), "SPY": _trend(15)}
        with patch.object(C5, "sp500_members", return_value=["AAA", "BIG"]):
            res = C5.run(store, Rest(bars))
        self.assertEqual([r["symbol"] for r in res["picks"]], ["AAA"])
        for key in (M.LIST_KEY, L3.LIST_KEY):
            self.assertIsNone(store.poller_get(key))
        routes = {r.path: r.endpoint for r in dashboard.create_app(store, MagicMock(), {}, {}).routes
                  if hasattr(r, "path")}
        self.assertEqual(routes["/api/cheap50"]()["list"]["picks"][0]["symbol"], "AAA")
        self.assertEqual(routes["/api/cheap50/{symbol}"]("AAA")["plan"]["action"], "buy")
        self.assertIsNotNone(json.loads(store.poller_get(C5.LIST_KEY)))

    def test_pages(self):
        html = open("web/index.html").read()
        self.assertIn('data-view="cheap"', html)
        self.assertIn("/api/cheap50", html)
        self.assertIn("cheap50", open("web/stock.html").read())


if __name__ == "__main__":
    unittest.main()
