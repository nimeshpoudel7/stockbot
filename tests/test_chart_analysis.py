"""Chart-reading engine: each detector on a hand-built chart, the
indicator math against the existing implementations, and the full
report's shape."""
import datetime as dt
import json
import unittest

import chart_analysis as CA
import indicators as ind

DAY = 86400
T0 = 1_700_000_000


def bar(i, o, h, l, c, v=1_000_000.0):
    return {"ts": T0 + i * DAY, "o": o, "h": h, "l": l, "c": c, "v": v}


def from_closes(closes, vol=1_000_000.0, wiggle=0.005):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(bar(i, o, max(o, c) * (1 + wiggle), min(o, c) * (1 - wiggle), c, vol))
    return out


def zigzag(points, steps=6):
    """Linear legs through the given turning points."""
    out = []
    for a, b in zip(points, points[1:]):
        for k in range(steps):
            out.append(a + (b - a) * k / steps)
    out.append(points[-1])
    return out


def names(items, active=True):
    return {f["name"] for f in items if f["active"] == active}


NOON = dt.datetime(2026, 10, 20, 12, 0, tzinfo=CA.ET)


class TestIndicatorMath(unittest.TestCase):
    closes = [100 + 10 * __import__("math").sin(i / 7) + i * 0.1 for i in range(200)]

    def test_rsi_matches_existing(self):
        a = CA.rsi_s(self.closes)
        b = ind.rsi_wilder(self.closes)
        for x, y in zip(a[20:], b[20:]):
            self.assertAlmostEqual(x, y, places=6)

    def test_ema_and_macd_match_existing(self):
        self.assertAlmostEqual(CA.ema_s(self.closes, 21)[-1], ind.ema(self.closes, 21), places=6)
        m1, s1, h1 = CA.macd_s(self.closes)
        m2, s2, h2 = ind.macd_series(self.closes)
        self.assertAlmostEqual(m1[-1], m2[-1], places=6)
        self.assertAlmostEqual(s1[-1], s2[-1], places=6)

    def test_atr_matches_existing(self):
        bars = from_closes(self.closes)
        ch = CA.Chart(bars)
        self.assertAlmostEqual(ch.atr[-1], ind.atr(bars, 14), places=6)

    def test_oscillators_bounded(self):
        bars = from_closes(self.closes)
        ch = CA.Chart(bars)
        adx, p, m = CA.adx_s(ch.h, ch.l, ch.c)
        k, d = CA.stoch_s(ch.h, ch.l, ch.c)
        for v in [x for x in adx + p + m + k + d if x is not None]:
            self.assertTrue(0 <= v <= 100, v)

    def test_psar_flips_with_trend(self):
        up = [100 + i for i in range(30)] + [130 - 2 * i for i in range(30)]
        ch = CA.Chart(from_closes(up))
        _, tr = CA.psar_s(ch.h, ch.l)
        self.assertTrue(tr[25])
        self.assertFalse(tr[-1])


class TestVolumePace(unittest.TestCase):
    def test_curve(self):
        self.assertEqual(CA.expected_volume_fraction(0), 0.0)
        self.assertAlmostEqual(CA.expected_volume_fraction(150), 0.38)
        self.assertEqual(CA.expected_volume_fraction(390), 1.0)
        self.assertEqual(CA.expected_volume_fraction(999), 1.0)

    def test_noon_ratio_is_paced(self):
        # 0.63x of a full day by 12:00 (150 min) is 1.66x the normal pace
        self.assertAlmostEqual(CA.paced_volume_ratio(630, 1000, 150), 0.63 / 0.38, places=3)
        self.assertEqual(CA.paced_volume_ratio(630, 1000, None), 0.63)
        self.assertIsNone(CA.paced_volume_ratio(1, 0, 10))

    def test_session_minutes(self):
        self.assertEqual(CA.session_minutes(NOON), 150)
        self.assertIsNone(CA.session_minutes(dt.datetime(2026, 10, 18, 12, tzinfo=CA.ET)))  # Sunday
        self.assertIsNone(CA.session_minutes(dt.datetime(2026, 10, 20, 17, tzinfo=CA.ET)))


class TestCandles(unittest.TestCase):
    def _hits(self, bars):
        ch = CA.Chart(bars)
        return {n for n, _, _ in CA.candle_hits(ch, ch.n - 1, False, False)}

    def base(self):
        return from_closes([110 - i for i in range(10)])  # a decline

    def test_hammer(self):
        b = self.base() + [bar(10, 100, 100.6, 95, 100.5)]
        self.assertIn("Hammer", self._hits(b))

    def test_shooting_star(self):
        b = from_closes([90 + i for i in range(10)]) + [bar(10, 100, 105, 99.8, 99.6)]
        self.assertIn("Shooting star", self._hits(b))

    def test_engulfing(self):
        b = self.base() + [bar(10, 101, 101.5, 99, 99.5), bar(11, 99.2, 102.5, 99, 102)]
        self.assertIn("Bullish engulfing", self._hits(b))
        b = from_closes([90 + i for i in range(10)]) + [bar(10, 99, 101, 98.8, 100.5), bar(11, 100.8, 101, 97, 98.5)]
        self.assertIn("Bearish engulfing", self._hits(b))

    def test_doji_inside_outside(self):
        b = self.base() + [bar(10, 100, 103, 97, 100.05)]
        self.assertIn("Doji", self._hits(b))
        b = self.base() + [bar(10, 100, 104, 96, 101), bar(11, 101, 102, 99, 100.5)]
        self.assertIn("Inside bar", self._hits(b))
        b = self.base() + [bar(10, 100, 101, 99, 100.5), bar(11, 100, 103, 97, 102.5)]
        self.assertIn("Outside bar", self._hits(b))

    def test_three_soldiers_and_crows(self):
        b = self.base() + [bar(10, 100, 102.2, 99.9, 102), bar(11, 102, 104.2, 101.9, 104), bar(12, 104, 106.2, 103.9, 106)]
        self.assertIn("Three white soldiers", self._hits(b))
        b = self.base() + [bar(10, 100, 100.1, 97.8, 98), bar(11, 98, 98.1, 95.8, 96), bar(12, 96, 96.1, 93.8, 94)]
        self.assertIn("Three black crows", self._hits(b))

    def test_morning_star(self):
        b = self.base() + [bar(10, 101, 101.2, 96.8, 97), bar(11, 96.5, 97, 96, 96.6), bar(12, 97, 100.5, 96.9, 100.2)]
        self.assertIn("Morning star", self._hits(b))


class TestStructure(unittest.TestCase):
    def test_uptrend_hh_hl(self):
        ch = CA.Chart(from_closes(zigzag([100, 110, 105, 118, 112, 126, 120, 125], 6)))
        st = CA.structure(ch)
        self.assertEqual(st["trend"], "up")
        self.assertEqual(st["label"], "HH + HL")

    def test_downtrend_and_choch(self):
        pts = [130, 118, 124, 110, 116, 102, 108, 100, 112]
        ch = CA.Chart(from_closes(zigzag(pts, 6)))
        st = CA.structure(ch)
        self.assertEqual(st["trend"], "down")
        self.assertTrue(any(e[1] == "CHoCH" and e[0] == "bull" for e in st["events"]))


class TestGaps(unittest.TestCase):
    def test_breakaway_gap_and_fill(self):
        closes = [100 + (i % 3) * 0.3 for i in range(40)]
        bars = from_closes(closes)
        bars.append(bar(40, 106, 108, 105.5, 107.5, 3_000_000))
        bars += [bar(41 + k, 107.5, 108.5, 106.5, 108) for k in range(5)]
        ch = CA.Chart(bars)
        items, chart = CA.gaps_section(ch, "range")
        bk = next(f for f in items if f["name"] == "Breakaway gap")
        self.assertTrue(bk["active"])
        # the gap day itself dipped to 105.5, 10% into the 101.1-106 gap
        self.assertIn("10% filled", bk["detail"])
        self.assertTrue(any(z["kind"] == "gap" for z in chart["zones"]))
        # now fill it
        bars.append(bar(46, 104, 104.5, 99, 100))
        items, _ = CA.gaps_section(CA.Chart(bars), "range")
        self.assertIn("fully filled", next(f for f in items if f["name"] == "Breakaway gap")["detail"])

    def test_small_gaps_ignored(self):
        bars = from_closes([100.0] * 40) + [bar(40, 100.8, 101, 100.6, 100.9)]
        items, _ = CA.gaps_section(CA.Chart(bars), "range")
        self.assertFalse(any(f["active"] and "gap" in f["name"].lower() and f["name"] != "Unfilled gap"
                             for f in items))

    def test_fair_value_gap(self):
        bars = from_closes([100.0] * 40, wiggle=0.002)
        bars += [bar(40, 100, 100.2, 99.8, 100.1), bar(41, 100.5, 106, 100.4, 105.8), bar(42, 106, 108, 104, 107.5),
                 bar(43, 107.5, 109, 106.5, 108.5)]
        items, _ = CA.gaps_section(CA.Chart(bars), "range")
        fvg = next(f for f in items if f["name"] == "Fair value gap (FVG)")
        self.assertTrue(fvg["active"])
        self.assertTrue(fvg["status"].startswith("Bullish"))


class TestPatterns(unittest.TestCase):
    def _act(self, closes):
        ch = CA.Chart(from_closes(closes))
        items, lines, _ = CA.patterns_section(ch)
        return {f["name"]: f for f in items if f["active"]}

    def test_double_top_confirmed(self):
        pts = [80, 90, 100, 92, 100.5, 88]
        found = self._act([80.0] * 40 + zigzag(pts, 7))
        self.assertIn("Double top", found)
        self.assertEqual(found["Double top"]["status"], "Confirmed")

    def test_double_bottom_forming(self):
        pts = [120, 110, 100, 108, 100.4, 105]
        found = self._act([120.0] * 40 + zigzag(pts, 7))
        self.assertIn("Double bottom", found)
        self.assertEqual(found["Double bottom"]["status"], "Forming")

    def test_head_and_shoulders(self):
        pts = [80, 95, 88, 105, 88.5, 95.5, 86]
        found = self._act([80.0] * 40 + zigzag(pts, 6))
        self.assertIn("Head and shoulders", found)
        self.assertEqual(found["Head and shoulders"]["status"], "Confirmed")

    def test_bull_flag(self):
        closes = [100.0] * 50 + [100 + 3 * k for k in range(1, 6)] + [114.5, 114, 113.6, 114.2, 113.8, 114.1]
        found = self._act(closes)
        self.assertTrue({"Bull flag", "Bullish pennant"} & set(found), found)

    def test_ascending_triangle(self):
        pts = [90, 100, 92, 100.2, 95, 100.1, 97, 100, 98.5]
        found = self._act([90.0] * 30 + zigzag(pts, 6))
        self.assertIn("Ascending triangle", found)

    def test_flat_market_has_no_patterns(self):
        found = self._act([100.0] * 120)
        self.assertFalse(found)


class TestVolumeSection(unittest.TestCase):
    def test_paced_relative_volume(self):
        bars = from_closes([100.0] * 60)
        bars[-1]["v"] = 630_000  # 0.63x of a full day so far
        ch = CA.Chart(bars)
        items, vp = CA.volume_section(ch, minutes=150, partial=True)
        rv = next(f for f in items if f["name"].startswith("Relative volume"))
        self.assertIn("1.7× normal pace", rv["status"])
        self.assertIn("0.63×", rv["detail"])
        self.assertIsNotNone(vp)

    def test_volume_profile_poc(self):
        bars = from_closes([100.0] * 60 + [120.0] * 10, vol=1_000_000)
        vp = CA.volume_profile(CA.Chart(bars))
        self.assertLess(abs(vp["poc"] - 100), 3)
        self.assertLessEqual(vp["val"], vp["poc"])
        self.assertLessEqual(vp["poc"], vp["vah"])


class TestAnalyze(unittest.TestCase):
    def test_short_history(self):
        self.assertIsNone(CA.analyze(from_closes([100.0] * 59)))

    def test_full_report_shape(self):
        import math
        closes = [100 + 15 * math.sin(i / 11) + i * 0.15 for i in range(260)]
        r = CA.analyze(from_closes(closes), spy_closes=[400 + i * 0.3 for i in range(260)], now=NOON)
        json.dumps(r)  # API-serializable
        keys = [s["key"] for s in r["sections"]]
        self.assertEqual(keys, ["trend", "levels", "gaps", "candles", "patterns", "volume",
                                "indicators", "liquidity", "context"])
        all_names = {f["name"] for s in r["sections"] for f in s["items"]}
        for must in ("Market structure", "Break of structure (BOS)", "Hammer", "Head and shoulders",
                     "Cup and handle", "Fair value gap (FVG)", "Ichimoku cloud", "ADX (14)",
                     "Parabolic SAR", "Liquidity sweep", "Bullish order block", "Premium / discount",
                     "Volume profile", "Multi-timeframe", "Relative strength vs SPY"):
            self.assertIn(must, all_names)
        for s in r["sections"]:
            flags = [f["active"] for f in s["items"]]
            self.assertEqual(flags, sorted(flags, reverse=True))  # found first
            for f in s["items"]:
                self.assertIn(f["bias"], ("bull", "bear", "neutral"))
        self.assertIn(r["summary"]["bias"], ("bullish", "bearish", "mixed"))
        self.assertTrue(r["summary"]["headline"].endswith("."))
        rk = r["risk"]
        self.assertTrue(all(s_["price"] < rk["price"] for s_ in rk["stops"]))
        ch = r["chart"]
        self.assertTrue(ch["overlays"]["sma50"] and ch["rsi"] and ch["macd"]["hist"] and ch["volume"])

    def test_intraday_vwap_and_opening_range(self):
        import math
        closes = [100 + 5 * math.sin(i / 9) for i in range(120)]
        daily = from_closes(closes)
        day0 = int(dt.datetime(2026, 10, 20, 9, 30, tzinfo=CA.ET).timestamp())
        intra = [{"ts": day0 + 60 * k, "o": 100, "h": 101 if k < 30 else 103, "l": 99, "c": 100 if k < 30 else 102.5,
                  "v": 1000} for k in range(60)]
        r = CA.analyze(daily, intraday=intra, now=NOON)
        items = {f["name"]: f for s in r["sections"] for f in s["items"]}
        self.assertTrue(items["VWAP (today)"]["active"])
        self.assertEqual(items["Opening range (first 30 min)"]["bias"], "bull")


if __name__ == "__main__":
    unittest.main()


class TestAnalysisApi(unittest.TestCase):
    def _app(self, bars):
        from unittest.mock import MagicMock
        import dashboard
        from storage import Store
        store = Store(":memory:")
        app = dashboard.create_app(store, MagicMock(), {}, {},
                                   extras={"daily_bars": lambda s, d: bars})
        return {r.path: r.endpoint for r in app.routes if hasattr(r, "path")}

    def test_endpoint_returns_full_read(self):
        import math
        bars = from_closes([100 + 10 * math.sin(i / 9) for i in range(260)])
        out = self._app(bars)["/api/analysis/{symbol}"]("abc")
        self.assertEqual(out["symbol"], "ABC")
        self.assertEqual(len(out["sections"]), 9)
        self.assertIn("risk", out)
        json.dumps(out)

    def test_short_history_404_and_bad_symbol_400(self):
        from fastapi import HTTPException
        ep = self._app(from_closes([100.0] * 30))["/api/analysis/{symbol}"]
        with self.assertRaises(HTTPException) as e:
            ep("ABC")
        self.assertEqual(e.exception.status_code, 404)
        with self.assertRaises(HTTPException) as e:
            ep("A1!")
        self.assertEqual(e.exception.status_code, 400)

    def test_stock_page_ships_chart_reading_ui(self):
        import dashboard
        html = dashboard.STOCK_PAGE
        for marker in ("Chart reading", "/api/analysis/", "Risk planner", "rsiChart", "macdChart",
                       "data-layer=\"fib\"", "Volume vs normal pace"):
            self.assertIn(marker, html)
