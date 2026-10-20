"""Unit tests for indicators.py — pure math, verified against hand-computed
values (Wilder's RSI example worked out step by step)."""
import unittest

import indicators as ind


class TestSMA(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(ind.sma([1, 2, 3, 4, 5], 3), 4.0)

    def test_not_enough_data(self):
        self.assertIsNone(ind.sma([1, 2], 3))

    def test_uses_last_n(self):
        self.assertAlmostEqual(ind.sma([10, 20, 30, 40], 2), 35.0)


class TestEMA(unittest.TestCase):
    def test_hand_computed(self):
        # values=[10,11,12,13,14], n=3: seed=(10+11+12)/3=11, k=0.5
        # after 13: 13*.5+11*.5=12 ; after 14: 14*.5+12*.5=13
        self.assertAlmostEqual(ind.ema([10, 11, 12, 13, 14], 3), 13.0)

    def test_not_enough_data(self):
        self.assertIsNone(ind.ema([1, 2], 3))


class TestRSI(unittest.TestCase):
    def test_wilder_hand_computed(self):
        # closes=[10,11,12,11,10,9], period=3
        # changes: +1,+1,-1,-1,-1 -> gains [1,1,0,0,0], losses [0,0,1,1,1]
        # idx3: avgG=2/3, avgL=1/3 -> RS=2 -> RSI=66.6667
        # idx4: avgG=0.4444, avgL=0.5556 -> RS=0.8 -> RSI=44.4444
        # idx5: avgG=0.2963, avgL=0.7037 -> RS=0.4211 -> RSI=29.6296
        r = ind.rsi_wilder([10, 11, 12, 11, 10, 9], 3)
        self.assertEqual(r[:3], [None, None, None])
        self.assertAlmostEqual(r[3], 66.6667, places=3)
        self.assertAlmostEqual(r[4], 44.4444, places=3)
        self.assertAlmostEqual(r[5], 29.6296, places=3)

    def test_all_gains_is_100(self):
        r = ind.rsi_wilder([1, 2, 3, 4, 5, 6], 3)
        self.assertAlmostEqual(r[3], 100.0)
        self.assertAlmostEqual(r[5], 100.0)

    def test_all_losses_is_0(self):
        r = ind.rsi_wilder([6, 5, 4, 3, 2, 1], 3)
        self.assertAlmostEqual(r[3], 0.0)

    def test_warmup_nones(self):
        r = ind.rsi_wilder([1, 2, 3], 14)
        self.assertTrue(all(v is None for v in r))
        self.assertEqual(len(r), 3)

    def test_default_period_bounds(self):
        # mixed data stays within (0, 100)
        closes = [100 + (i % 7) - 3 + (0.5 if i % 2 else -0.5) for i in range(40)]
        r = ind.rsi_wilder(closes)
        vals = [v for v in r if v is not None]
        self.assertTrue(vals)
        self.assertTrue(all(0 < v < 100 for v in vals))


class TestATR(unittest.TestCase):
    def test_hand_computed(self):
        bars = [
            {"h": 10, "l": 9, "c": 9.5},
            {"h": 11, "l": 9.5, "c": 10.5},   # TR = max(1.5, 1.5, 0) = 1.5
            {"h": 12, "l": 10, "c": 11},      # TR = max(2, 1.5, 0.5) = 2
        ]
        self.assertAlmostEqual(ind.atr(bars, 2), 1.75)

    def test_not_enough_data(self):
        self.assertIsNone(ind.atr([{"h": 1, "l": 1, "c": 1}], 14))


class TestCrosses(unittest.TestCase):
    def test_crossed_above(self):
        self.assertTrue(ind.crossed_above([1.0, 3.0], [2.0, 2.0]))
        self.assertFalse(ind.crossed_above([3.0, 4.0], [2.0, 2.0]))  # already above
        self.assertFalse(ind.crossed_above([1.0, 1.5], [2.0, 2.0]))  # still below

    def test_crossed_below(self):
        self.assertTrue(ind.crossed_below([3.0, 1.0], [2.0, 2.0]))
        self.assertFalse(ind.crossed_below([1.0, 0.5], [2.0, 2.0]))

    def test_none_safe(self):
        self.assertFalse(ind.crossed_above([None, 3.0], [2.0, 2.0]))
        self.assertFalse(ind.crossed_below([3.0], [2.0]))


class TestVolumeSpike(unittest.TestCase):
    def test_spike(self):
        self.assertTrue(ind.volume_spike(350, [100.0] * 20, 3.0, 20))

    def test_no_spike(self):
        self.assertFalse(ind.volume_spike(250, [100.0] * 20, 3.0, 20))

    def test_needs_full_lookback(self):
        self.assertFalse(ind.volume_spike(9999, [100.0] * 10, 3.0, 20))


class TestDayPct(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(ind.day_pct_change(110, 100), 10.0)
        self.assertAlmostEqual(ind.day_pct_change(95, 100), -5.0)

    def test_missing(self):
        self.assertIsNone(ind.day_pct_change(100, 0))
        self.assertIsNone(ind.day_pct_change(100, None))


if __name__ == "__main__":
    unittest.main()
