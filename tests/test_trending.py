"""Top-10 trending: mover metrics, candidate filter, score, list build and
its place in the 15-minute watch pass."""
import json
import unittest

import scanner
import trending
from storage import Store
from tests.test_scanner import make_bars
from tests.test_watch_heartbeat import _run


def _mover(pct=6.0, vol_mult=4.0, n=250):
    closes = [100.0 + i * 0.05 for i in range(n - 1)]
    last = closes[-1] * (1 + pct / 100)
    bars = make_bars(closes + [last], last_vol=5_000_000.0 * vol_mult)
    # day's range: close near the high for an up move
    bars[-1]["h"] = max(last, bars[-1]["h"]) * 1.001
    bars[-1]["l"] = min(bars[-2]["c"], last) * 0.999
    return bars


class TestTrending(unittest.TestCase):
    def test_metrics_and_candidate(self):
        bars = _mover()
        m = trending.mover_metrics(bars, None)
        self.assertAlmostEqual(m["pct"], 6.0, places=4)
        self.assertTrue(m["new_high20"])
        self.assertTrue(trending.is_candidate(m))
        flat = trending.mover_metrics(make_bars([100.0] * 250), None)
        self.assertFalse(trending.is_candidate(flat))

    def test_penny_and_thin_names_excluded(self):
        self.assertIsNone(trending.mover_metrics(make_bars([2.0] * 250), None))
        self.assertIsNone(trending.mover_metrics(make_bars([100.0] * 30), None))

    def test_score_bounds_and_reasons(self):
        m = trending.mover_metrics(_mover(), None)
        sc, why = trending.score(m, {"bull": 5, "bear": 1})
        self.assertTrue(0 <= sc <= 100)
        self.assertTrue(any("today" in w for w in why))
        weak, _ = trending.score(trending.mover_metrics(_mover(pct=3.1, vol_mult=1.6), None), None)
        self.assertLess(weak, sc)

    def test_build_splits_up_and_down(self):
        cands = {}
        for s, pct in (("UPA", 7.0), ("UPB", 4.0), ("DNA", -6.0)):
            b = _mover(pct=pct)
            cands[s] = (trending.mover_metrics(b, None), b)
        out = trending.build(cands, None, None)
        self.assertEqual([r["symbol"] for r in out["up"]][:1], ["UPA"])
        self.assertEqual([r["symbol"] for r in out["down"]], ["DNA"])
        r = out["up"][0]
        self.assertLess(r["stop"], r["price"])
        self.assertGreater(r["target"], r["price"])
        self.assertIn(r["rating"], ("Hot", "Strong", "Active"))

    def test_watch_pass_persists_trending_even_for_seen_symbols(self):
        store = Store(":memory:")
        scanner._save_seen(store, {"date": scanner._et_today_str(),
                                   "symbols": ["MOVE|BUY", "MOVE|SELL"]})
        _run(store, {"MOVE": _mover(), "FLAT": make_bars([100.0] * 250)})
        data = json.loads(store.poller_get(scanner.SCAN_TRENDING_KEY))
        self.assertEqual([r["symbol"] for r in data["up"]], ["MOVE"])
        self.assertIn("ts", data)


if __name__ == "__main__":
    unittest.main()
