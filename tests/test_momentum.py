"""Momentum leaders (1-3 month list), its scorecard, and the Top-10
earnings filter / exit plans."""
import json
import unittest
from unittest.mock import MagicMock

import momentum
import scanner
import scorecard
import top_picks as TP
from storage import Store
from tests.test_scanner import make_bars
from tests.test_top_picks import EQUAL, _cand, _dip


def _trend(total_pct, n=300, last_month_pct=2.0):
    start = 50.0
    end_m = start * (1 + total_pct / 100)
    closes = [start + (end_m - start) * i / (n - 22) for i in range(n - 21)]
    closes += [end_m * (1 + last_month_pct / 100 * (i + 1) / 21) for i in range(21)]
    return make_bars(closes, vol=1_000_000.0)


class TestMomentum(unittest.TestCase):
    def test_row_measures_12_1_momentum(self):
        bars = _trend(100)
        r = momentum.row("AAA", bars)
        want = (bars[-22]["c"] / bars[-253]["c"] - 1) * 100
        self.assertAlmostEqual(r["mom12"], want, delta=0.1)
        self.assertTrue(r["above200"])
        self.assertIsNone(momentum.row("SHORT", _trend(100, n=200)))
        self.assertIsNone(momentum.row("PENNY", make_bars([2.0] * 300)))

    def test_build_ranks_uptrends_only(self):
        rows = [momentum.row(s, _trend(p, last_month_pct=lm))
                for s, p, lm in (("A", 120, 25), ("B", 60, 1), ("C", 10, 1),
                                 ("DOWN", 80, -45))]
        out = momentum.build(rows, [100.0] * 200)
        self.assertEqual([r["symbol"] for r in out["BUY"]], ["A", "B", "C"])
        self.assertTrue(any("stretched" in n for n in out["BUY"][0]["notes"]))
        self.assertFalse(out["risk_off"])
        self.assertIn("3 months", out["BUY"][0]["exit"])

    def test_cash_when_spy_below_200_day(self):
        rows = [momentum.row("A", _trend(120))]
        out = momentum.build(rows, [100.0] * 199 + [90.0])
        self.assertTrue(out["risk_off"])
        self.assertEqual(out["BUY"], [])
        self.assertEqual([r["symbol"] for r in out["watch"]], ["A"])

    def test_scorecard_dedupes_a_held_position(self):
        store = Store(":memory:")
        lists = {"BUY": [{"symbol": "A", "price": 10.0, "score": 1}]}
        k = scorecard.POSITION_KEY
        self.assertEqual(scorecard.record(store, lists, "2026-10-01", k, dedupe_days=30), 1)
        self.assertEqual(scorecard.record(store, lists, "2026-10-15", k, dedupe_days=30), 0)
        self.assertEqual(scorecard.record(store, lists, "2026-11-20", k, dedupe_days=30), 1)

    def test_morning_scan_persists_position_list(self):
        from tests.test_scanner import _patch_sentiment
        from unittest.mock import patch
        store = Store(":memory:")
        bars = {"AAPL": _trend(80), "MSFT": _trend(40)}

        class R:
            def daily_bars_multi(self, syms, days=300):
                return {s: bars[s] for s in syms if s in bars}
        with patch.object(scanner.sessions_mod, "is_trading_day", return_value=True), \
                patch.object(scanner, "get_universe", return_value=list(bars)), \
                patch.object(scanner, "sp500_members", return_value={"AAPL", "MSFT"}), \
                patch.object(scanner, "_current_regime", return_value=None), \
                patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
                patch.object(scanner, "short_bonus", return_value=(0, [])), \
                patch.object(scanner, "earnings_note", return_value=None), \
                patch.object(scanner.time, "sleep", lambda *_: None), _patch_sentiment():
            scanner.run_scan(store=store, rest=R(), dry_run=False,
                             push_fn=MagicMock(return_value=True))
        data = json.loads(store.poller_get(scanner.SCAN_POSITION_KEY))
        self.assertEqual([r["symbol"] for r in data["BUY"]], ["AAPL", "MSFT"])
        self.assertEqual(len(scorecard._load(store, scorecard.POSITION_KEY)), 2)


class TestTop10Upgrades(unittest.TestCase):
    def test_earnings_keeps_pick_out_of_tested_list(self):
        out = TP.rank([_cand("DIP", _dip())], model=EQUAL,
                      earnings_fn=lambda s: "⚠ earnings Oct 22")
        self.assertEqual(out["BUY"], [])
        near = out["BUY_near"][0]
        self.assertEqual((near["symbol"], near["earnings"]), ("DIP", "⚠ earnings Oct 22"))

    def test_exit_plan_attached(self):
        out = TP.rank([_cand("DIP", _dip())], model=EQUAL)
        self.assertIn("5-day average", out["BUY"][0]["exit"])
        self.assertIn("BUY", out["exits"])


if __name__ == "__main__":
    unittest.main()


class TestMomentumDedupe(unittest.TestCase):
    def test_duplicates_collapsed(self):
        r = momentum.row("D", _trend(60))
        self.assertEqual(len(momentum.build([r, dict(r)], None)["BUY"]), 1)
