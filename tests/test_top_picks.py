"""Live Top-10 BUY / SELL: combined score, overlays, watch-pass wiring,
and the live scorecard."""
import datetime as dt
import json
import unittest
from unittest.mock import MagicMock

import scanner
import scorecard
import top_picks as TP
from storage import Store
from tests.test_scanner import BUY_CLOSES, make_bars
from tests.test_watch_heartbeat import _run

EQUAL = {s: {"weights": {k: 1.0 for k in TP.COMPONENTS}, "min_score": 0.0} for s in ("BUY", "SELL")}


def _dip():
    return make_bars(BUY_CLOSES, last_vol=5_000_000.0)


def _cand(sym, bars):
    return {"symbol": sym, "res": scanner.technical_screen(bars), "bars": bars}


class TestComponents(unittest.TestCase):
    def test_shapes_and_signs(self):
        r = {"tech": 80, "bear": 10, "rsi2": 3, "a200": True, "a50": True, "ema": 1,
             "cpos": 0.1, "rs63": 0.1, "cb": 6, "cr": 2}
        b, s = TP.comps(r, "BUY"), TP.comps(r, "SELL")
        self.assertEqual(set(b), set(TP.COMPONENTS))
        self.assertEqual(b["setup"], 0.8)
        self.assertEqual(b["pullback"], 1.0)
        self.assertEqual(s["pullback"], 0.0)
        self.assertAlmostEqual(b["chart"], 0.5)
        self.assertAlmostEqual(s["chart"], -0.5)
        self.assertAlmostEqual(b["weakclose"], 0.9)
        self.assertGreater(b["trend"], 0)
        self.assertLess(s["trend"], 0)
        self.assertTrue(TP.eligible(b))

    def test_study_uses_the_same_components(self):
        import study_combined_fit as F
        self.assertIs(F.comps, TP.comps)
        self.assertEqual(F.COMPONENTS, TP.COMPONENTS)

    def test_shipped_model_is_valid(self):
        m = TP.load_model()
        for side in ("BUY", "SELL"):
            self.assertEqual(set(m[side]["weights"]), set(TP.COMPONENTS))
            self.assertIn("hit", m[side]["test"])
            self.assertGreater(m[side]["test"]["n"], 100)
        self.assertIn("test_period", m["meta"])


class TestRank(unittest.TestCase):
    def test_ranks_with_breakdown_and_overlays(self):
        news = MagicMock(return_value={"sentiment": 0.5, "label": "bullish", "n": 3, "marketaux_n": 2})
        health = MagicMock(return_value=(0.5, ["Piotroski 8/9"]))
        out = TP.rank([_cand("DIP", _dip()), _cand("FLAT", make_bars([100.0] * 250))],
                      model=EQUAL, sentiment_fn=news, health_fn=health)
        buy = out["BUY"][0]
        self.assertEqual(buy["symbol"], "DIP")
        keys = {p["key"] for p in buy["parts"]}
        self.assertIn("news", keys)
        self.assertIn("health", keys)
        self.assertEqual(buy["news"]["n"], 5)
        self.assertTrue(0 <= buy["score"] <= 100)
        syms = [c.args[0] for c in news.call_args_list]
        self.assertEqual(len(syms), len(set(syms)))   # once per symbol, both sides

    def test_min_score_filters(self):
        strict = {s: {"weights": EQUAL[s]["weights"], "min_score": 0.99} for s in ("BUY", "SELL")}
        out = TP.rank([_cand("DIP", _dip())], model=strict)
        self.assertEqual(out["BUY"], [])
        self.assertEqual(out["BUY_near"][0]["symbol"], "DIP")

    def test_failures_degrade(self):
        boom = MagicMock(side_effect=RuntimeError("api down"))
        out = TP.rank([_cand("DIP", _dip())], model=EQUAL, sentiment_fn=boom,
                      health_fn=boom, budget=0)
        self.assertEqual(out["BUY"][0]["symbol"], "DIP")
        self.assertIsNone(out["BUY"][0]["chart"])

    def test_collector_filters_cheap_and_illiquid(self):
        col = TP.Collector(model=EQUAL)
        col.add("PENNY", scanner.technical_screen(make_bars([2.0] * 250)), make_bars([2.0] * 250))
        col.add("DIP", scanner.technical_screen(_dip()), _dip())
        self.assertEqual([c["symbol"] for c in col.candidates()], ["DIP"])


class TestWatchWiring(unittest.TestCase):
    def test_pass_persists_top10_and_logs_scorecard(self):
        store = Store(":memory:")
        scanner._save_seen(store, {"date": scanner._et_today_str(), "symbols": ["DIP|BUY", "DIP|SELL"]})
        _run(store, {"DIP": _dip(), "FLAT": make_bars([100.0] * 250)})
        data = json.loads(store.poller_get(scanner.SCAN_TOP10_KEY))
        self.assertIn("BUY", data)
        self.assertIn("model", data)
        log = scorecard._load(store)
        self.assertEqual(len(log), len(data["BUY"]) + len(data["SELL"]))


def _recent(closes):
    """Bars ending well before today (the log keeps ~120 days)."""
    bars = make_bars(closes)
    start = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).timestamp())
    for i, b in enumerate(bars):
        b["ts"] = start + i * 86400
    return bars


class TestScorecard(unittest.TestCase):
    def test_record_once_per_day_and_grade(self):
        store = Store(":memory:")
        bars = _recent([100.0 + i for i in range(30)])
        d0 = dt.datetime.fromtimestamp(bars[10]["ts"], dt.timezone.utc).date().isoformat()
        lists = {"BUY": [{"symbol": "AAA", "price": 110.0, "score": 70}],
                 "SELL": [{"symbol": "AAA", "price": 110.0, "score": 40}]}
        self.assertEqual(scorecard.record(store, lists, d0), 2)
        self.assertEqual(scorecard.record(store, lists, d0), 0)
        self.assertEqual(scorecard.pending_symbols(store), {"AAA"})
        self.assertEqual(scorecard.grade(store, {"AAA": bars}), 4)
        s = scorecard.summary(store)
        self.assertEqual((s["BUY"]["hit5"], s["SELL"]["hit5"]), (100.0, 0.0))
        self.assertAlmostEqual(s["BUY"]["avg5"], (115 / 110 - 1) * 100, places=1)
        self.assertEqual(scorecard.pending_symbols(store), set())

    def test_not_graded_before_horizon(self):
        store = Store(":memory:")
        bars = _recent([100.0] * 12)
        d0 = dt.datetime.fromtimestamp(bars[8]["ts"], dt.timezone.utc).date().isoformat()
        scorecard.record(store, {"BUY": [{"symbol": "AAA", "price": 100.0, "score": 60}]}, d0)
        self.assertEqual(scorecard.grade(store, {"AAA": bars}), 0)


if __name__ == "__main__":
    unittest.main()
