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
        self.assertEqual(out["picks"][0]["notes"], [])   # no untested "wait for a pullback" advice
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


class TestModeratePlan(unittest.TestCase):
    def _store(self, picks, risk_off=False, log=()):
        store = Store(":memory:")
        today = dt.datetime.now(M.ET).date().isoformat()
        store.poller_set(M.LIST_KEY, json.dumps({
            "date": today, "risk_off": risk_off,
            "picks": [] if risk_off else picks, "watch": picks if risk_off else []}))
        store.poller_set(M.LOG_KEY, json.dumps(list(log)))
        return store, today

    @staticmethod
    def _ago(days):
        return (dt.datetime.now(M.ET).date() - dt.timedelta(days=days)).isoformat()

    def test_new_pick_is_buy_with_sell_date(self):
        store, today = self._store([{"symbol": "AAA", "rank": 2, "price": 10.0, "sma200": 8.0}])
        M.record(store, [{"symbol": "AAA", "price": 10.0, "rank": 2, "mom12": 50}], today)
        p = M.plan(store, "aaa")
        self.assertEqual((p["action"], p["rank"], p["entry_price"]), ("buy", 2, 10.0))
        self.assertEqual(p["days_left"], M.HOLD_CAL_DAYS)

    def test_hold_then_dropped_then_sell_due(self):
        store, _ = self._store([{"symbol": "AAA", "rank": 1, "price": 10.0}],
                               log=[{"date": self._ago(20), "symbol": "AAA", "price": 9.0}])
        self.assertEqual(M.plan(store, "AAA")["action"], "hold")
        store, _ = self._store([], log=[{"date": self._ago(20), "symbol": "AAA", "price": 9.0}])
        self.assertEqual(M.plan(store, "AAA")["action"], "dropped")
        store, _ = self._store([], log=[{"date": self._ago(100), "symbol": "AAA", "price": 9.0}])
        self.assertEqual(M.plan(store, "AAA")["action"], "sell_due")

    def test_streak_keeps_first_entry_and_extends_sell_date(self):
        log = [{"date": self._ago(60), "symbol": "AAA", "price": 8.0},
               {"date": self._ago(29), "symbol": "AAA", "price": 9.0}]
        store, _ = self._store([{"symbol": "AAA", "rank": 1, "price": 10.0}], log=log)
        p = M.plan(store, "AAA")
        self.assertEqual((p["listed_on"], p["entry_price"]), (self._ago(60), 8.0))
        self.assertEqual(p["days_left"], M.HOLD_CAL_DAYS - 29)

    def test_cash_mode_and_unknown(self):
        store, _ = self._store([{"symbol": "AAA", "rank": 1, "price": 10.0}], risk_off=True)
        self.assertEqual(M.plan(store, "AAA")["action"], "hold_no_new")
        self.assertIsNone(M.plan(store, "ZZZ"))

    def test_api_plan_and_list_enrichment(self):
        store, today = self._store([{"symbol": "AAA", "rank": 1, "price": 10.0}])
        M.record(store, [{"symbol": "AAA", "price": 10.0, "rank": 1, "mom12": 50}], today)
        routes = {r.path: r.endpoint for r in dashboard.create_app(store, MagicMock(), {}, {}).routes
                  if hasattr(r, "path")}
        self.assertEqual(routes["/api/moderate/{symbol}"]("AAA")["plan"]["action"], "buy")
        self.assertEqual(routes["/api/moderate"]()["list"]["picks"][0]["action"], "buy")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            routes["/api/moderate/{symbol}"]("A1")
