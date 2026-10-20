"""Tests for the Phase-2 indicator additions: Bollinger, Fibonacci, MACD,
volume z-score, ATR%, and 1m -> N-minute bar aggregation. Pure math."""
import unittest

import indicators as ind


def bars_from_closes(closes, start_ts=1_000_000):
    return [{"ts": start_ts + i * 60, "o": c, "h": c + 0.5, "l": c - 0.5,
             "c": c, "v": 1000} for i, c in enumerate(closes)]


class TestBollinger(unittest.TestCase):
    def test_bands_hand_computed(self):
        closes = [10.0] * 20
        mid, upper, lower = ind.bollinger_bands(closes, 20, 2.0)
        self.assertAlmostEqual(mid, 10.0)
        self.assertAlmostEqual(upper, 10.0)  # zero variance
        self.assertAlmostEqual(lower, 10.0)

    def test_bands_spread(self):
        closes = list(range(1, 21))  # 1..20, mean 10.5
        mid, upper, lower = ind.bollinger_bands(closes, 20, 2.0)
        self.assertAlmostEqual(mid, 10.5)
        var = sum((v - 10.5) ** 2 for v in closes) / 20
        sd = var ** 0.5
        self.assertAlmostEqual(upper, 10.5 + 2 * sd)
        self.assertAlmostEqual(lower, 10.5 - 2 * sd)

    def test_not_enough_data(self):
        self.assertEqual(ind.bollinger_bands([1, 2, 3], 20, 2.0),
                         (None, None, None))

    def test_bandwidth(self):
        closes = list(range(1, 21))
        bw = ind.bollinger_bandwidth(closes, 20, 2.0)
        mid, upper, lower = ind.bollinger_bands(closes, 20, 2.0)
        self.assertAlmostEqual(bw, (upper - lower) / mid)

    def test_pctb(self):
        self.assertAlmostEqual(ind.bollinger_pctb(10, 12, 8), 0.5)
        self.assertAlmostEqual(ind.bollinger_pctb(12, 12, 8), 1.0)
        self.assertIsNone(ind.bollinger_pctb(10, 8, 8))


class TestFibonacci(unittest.TestCase):
    def test_levels(self):
        lv = ind.fib_levels(100.0, 200.0)
        self.assertAlmostEqual(lv[0.5], 150.0)
        self.assertAlmostEqual(lv[0.236], 200 - 100 * 0.236)
        self.assertAlmostEqual(lv[0.786], 200 - 100 * 0.786)
        self.assertEqual(set(lv), {0.236, 0.382, 0.5, 0.618, 0.786})

    def test_swing_high_low(self):
        bars = bars_from_closes([10, 12, 9, 15, 11])
        hi, lo = ind.swing_high_low(bars, 5)
        self.assertEqual(hi, 15.5)   # bar highs are close+0.5
        self.assertEqual(lo, 8.5)
        self.assertEqual(ind.swing_high_low([], 5), (None, None))


class TestMACD(unittest.TestCase):
    def test_flat_series(self):
        closes = [50.0] * 60
        macd, sig, hist = ind.macd_series(closes)
        self.assertEqual(len(macd), 60)
        # flat input -> macd line converges to ~0 once warmed
        self.assertAlmostEqual(macd[-1], 0.0, places=6)
        self.assertAlmostEqual(hist[-1], 0.0, places=6)

    def test_warmup_nones(self):
        closes = [float(i) for i in range(40)]
        macd, sig, hist = ind.macd_series(closes)
        self.assertIsNone(macd[24])   # slow=26 needs 26 values
        self.assertIsNotNone(macd[25])
        self.assertIsNone(sig[25])    # signal needs 9 macd values
        self.assertIsNotNone(sig[33])

    def test_uptrend_positive_hist(self):
        closes = [100 + i * i * 0.05 for i in range(60)]  # accelerating
        _, _, hist = ind.macd_series(closes)
        self.assertGreater(hist[-1], 0)

    def test_macd_line_positive_in_uptrend(self):
        closes = [100 + i * 2 for i in range(60)]  # linear: line > 0
        macd, _, _ = ind.macd_series(closes)
        self.assertGreater(macd[-1], 0)


class TestVolumeZScore(unittest.TestCase):
    def test_spike(self):
        vols = [100.0 + (i % 3) for i in range(20)]  # slight variance
        z = ind.volume_zscore(500.0, vols, 20)
        self.assertIsNotNone(z)
        self.assertGreater(z, 5)
        self.assertIsNone(ind.volume_zscore(100.0, [100.0] * 5, 20))

    def test_zero_variance_none(self):
        # constant history -> z-score undefined, not a crash
        self.assertIsNone(ind.volume_zscore(500.0, [100.0] * 20, 20))

    def test_atr_pct(self):
        bars = bars_from_closes([100.0] * 20)
        self.assertAlmostEqual(ind.atr_pct(bars), 0.01)  # TR=1, close=100
        self.assertIsNone(ind.atr_pct(bars[:5]))


class TestAggregateBars(unittest.TestCase):
    def test_5m_fold(self):
        bars = bars_from_closes([10, 11, 12, 13, 14], start_ts=999_900)
        # 999900 is 5m-bucket aligned: all five bars share one bucket
        out = ind.aggregate_bars(bars, 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["o"], 10)
        self.assertEqual(out[0]["c"], 14)
        self.assertEqual(out[0]["h"], 14.5)
        self.assertEqual(out[0]["l"], 9.5)
        self.assertEqual(out[0]["v"], 5000)

    def test_bucket_boundary(self):
        b1 = {"ts": 299, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
        b2 = {"ts": 300, "o": 2, "h": 2, "l": 2, "c": 2, "v": 1}
        out = ind.aggregate_bars([b1, b2], 5)
        self.assertEqual(len(out), 2)

    def test_1m_passthrough(self):
        bars = bars_from_closes([1, 2, 3])
        self.assertEqual(ind.aggregate_bars(bars, 1), bars)


if __name__ == "__main__":
    unittest.main()
