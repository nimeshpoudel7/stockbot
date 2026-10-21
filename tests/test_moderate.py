"""Moderate-risk strategy: separate list, cash rule, log, grading, API,
morning-scan hook — and it leaves the existing picks untouched."""
import datetime as dt
import json
import unittest
from unittest.mock import MagicMock, patch

import dashboard
import moderate as M
import scanner
from storage import Store
from tests.test_scanner import _patch_sentiment, make_bars


def _trend(total_pct, n=300, last_month_pct=2.0, vol=1_000_000.0):
    start, end_m = 50.0, 50.0 * (1 + total_pct / 100)
    closes = [start + (end_m - start) * i / (n - 22) for i in range(n - 21)]
    closes += [end_m * (1 + last_month_pct / 100 * (i + 1) / 21) for i in range(21)]
    bars = make_bars(closes, vol=vol)
    start_ts = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=n + 5)).timestamp())
    for i, b in enumerate(bars):
        b["ts"] = start_ts + i * 86400
    return bars


class Rest:
    def __init__(self, bars):
        self.bars = bars
        self.calls = 0

    def daily_bars_multi(self, syms, days=300):
        self.calls += 1
        return {s: self.bars[s] for s in syms if s in self.bars}


class TestModerateList(unittest.TestCase):
    def test_row_and_filters(self):
        b = _trend(100)
        r = M.row("A", b)
        self.assertAlmostEqual(r["mom12"], (b[-22]["c"] / b[-253]["c"] - 1) * 100, delta=0.1)
        self.assertIsNone(M.row("SHORT", _trend(100, n=200)))
        self.assertIsNone(M.row("THIN", _trend(100, vol=1000.0)))

    def test_ranks_uptrends_and_cash_rule(self):
        rows = [M.row("A", _trend(120, last_month_pct=25)), M.row("B", _trend(60)),
                M.row("DOWN", _trend(80, last_month_pct=-45))]
        out = M.build(rows, [100.0] * 200)
        self.assertEqual([r["symbol"] for r in out["picks"]], ["A", "B"])
        self.assertTrue(out["picks"][0]["notes"])
        cash = M.build(rows, [100.0] * 199 + [90.0])
        self.assertTrue(cash["risk_off"])
        self.assertEqual(cash["picks"], [])
        self.assertEqual([r["symbol"] for r in cash["watch"]], ["A", "B"])


class TestModerateJob(unittest.TestCase):
    def test_run_persists_logs_and_grades(self):
        store = Store(":memory:")
        bars = {"AAA": _trend(90), "BBB": _trend(40), "SPY": _trend(15)}
        with patch.object(M, "sp500_members", return_value=["AAA", "BBB"]):
            res = M.run(store, Rest(bars))
        self.assertEqual([r["symbol"] for r in res["picks"]], ["AAA", "BBB"])
        data = json.loads(store.poller_get(M.LIST_KEY))
        self.assertIn("backtest", data)
        self.assertEqual(len(M._load(store)), 2)
        # re-running the same day logs nothing new
        with patch.object(M, "sp500_members", return_value=["AAA", "BBB"]):
            M.run(store, Rest(bars))
        self.assertEqual(len(M._load(store)), 2)

    def test_grade_after_horizon(self):
        store = Store(":memory:")
        b = _trend(50, n=300)
        d0 = dt.datetime.fromtimestamp(b[200]["ts"], dt.timezone.utc).date().isoformat()
        M.record(store, [{"symbol": "AAA", "price": b[200]["c"], "rank": 1, "mom12": 50}], d0)
        self.assertEqual(M.grade(store, {"AAA": b}), 2)
        s = M.summary(store)
        self.assertEqual((s["n21"], s["n63"], s["hit63"]), (1, 1, 100.0))

    def test_run_never_raises(self):
        store = Store(":memory:")
        bad = MagicMock()
        bad.daily_bars_multi.side_effect = RuntimeError("api down")
        with patch.object(M, "sp500_members", return_value=["AAA"]):
            self.assertIsNone(M.run(store, bad))


class TestModerateIntegration(unittest.TestCase):
    def test_api(self):
        store = Store(":memory:")
        ep = next(r.endpoint for r in dashboard.create_app(store, MagicMock(), {}, {}).routes
                  if getattr(r, "path", "") == "/api/moderate")
        out = ep()
        self.assertIsNone(out["list"])
        self.assertEqual(out["scorecard"]["picks"], 0)
        self.assertIn("test", out["backtest"])

    def test_morning_scan_builds_it_without_touching_picks(self):
        store = Store(":memory:")
        bars = {"AAPL": _trend(80), "SPY": _trend(15)}
        with patch.object(scanner.sessions_mod, "is_trading_day", return_value=True), \
                patch.object(scanner, "get_universe", return_value=["AAPL"]), \
                patch.object(scanner, "_current_regime", return_value=None), \
                patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
                patch.object(scanner, "short_bonus", return_value=(0, [])), \
                patch.object(scanner, "earnings_note", return_value=None), \
                patch.object(M, "sp500_members", return_value=["AAPL"]), \
                patch.object(scanner.time, "sleep", lambda *_: None), _patch_sentiment():
            scanner.run_scan(store=store, rest=Rest(bars), dry_run=False,
                             push_fn=MagicMock(return_value=True))
        self.assertIsNotNone(store.poller_get(scanner.SCAN_MORNING_KEY))
        self.assertEqual([r["symbol"] for r in json.loads(store.poller_get(M.LIST_KEY))["picks"]], ["AAPL"])

    def test_page_has_tab(self):
        html = open("web/index.html").read()
        self.assertIn('data-view="moderate"', html)
        self.assertIn("/api/moderate", html)


if __name__ == "__main__":
    unittest.main()
