"""Tests for dashboard.py: /api/scanner serves the persisted scanner picks,
/api/candles honors the chart tf param (daily bars via the extras fn, DB
cached), and /api/quotes falls back to the last close when no live tick.

No httpx in the venv, so instead of TestClient the routes' endpoint
functions are invoked directly — they are plain functions of the store.
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import dashboard
from storage import Store


def _endpoint(app, path):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"no {path} route")


def _scanner_endpoint(app):
    return _endpoint(app, "/api/scanner")


def _daily_bars(n, start=70.0, step=10.0):
    base = 1_700_000_000
    return [{"ts": base + i * 86400, "o": start + i * step,
             "h": start + i * step + 1, "l": start + i * step - 1,
             "c": start + i * step, "v": 1000} for i in range(n)]


class TestApiScanner(unittest.TestCase):
    def _app(self, store):
        return dashboard.create_app(store, MagicMock(), MagicMock(), {})

    def test_returns_stored_payloads(self):
        store = Store(":memory:")
        morning = {"kind": "morning", "ts": "2026-10-19T08:00:00-04:00",
                   "buys": [{"symbol": "TESTA", "price": 160.0, "score": 55,
                             "rating": "5.5/10", "breakdown": "golden cross",
                             "reasons": ["golden cross"], "sentiment": 0.0}],
                   "sells": []}
        watch = {"kind": "watch", "date": "2026-10-19",
                 "entries": [{"symbol": "TESTA", "side": "BUY",
                              "price": 160.0, "score": 55,
                              "rating": "5.5/10",
                              "breakdown": "golden cross",
                              "reasons": ["golden cross"],
                              "time": "2026-10-19T10:15:00-04:00"}]}
        store.poller_set("scanner_morning", json.dumps(morning))
        store.poller_set("scanner_watch", json.dumps(watch))
        out = _scanner_endpoint(self._app(store))()
        self.assertEqual((out["morning"], out["watch"], out["watch_last"], out["top10"]),
                         (morning, watch, None, None))

    def test_null_when_nothing_stored(self):
        store = Store(":memory:")
        out = _scanner_endpoint(self._app(store))()
        self.assertEqual({k: out[k] for k in ("morning", "watch", "watch_last", "top10", "position")},
                         {"morning": None, "watch": None, "watch_last": None,
                          "top10": None, "position": None})
        self.assertEqual(out["scorecard"]["picks"], 0)
        self.assertEqual(out["position_scorecard"]["picks"], 0)

    def test_bad_json_yields_null(self):
        store = Store(":memory:")
        store.poller_set("scanner_morning", "{not json")
        out = _scanner_endpoint(self._app(store))()
        self.assertIsNone(out["morning"])
        self.assertIsNone(out["watch"])


class TestApiCandlesTimeframes(unittest.TestCase):
    def _app(self, store, extras=None, market=None):
        return dashboard.create_app(store, MagicMock(),
                                    market if market is not None else {},
                                    {}, extras=extras)

    def test_daily_tf_slices_and_caches(self):
        store = Store(":memory:")
        calls = []

        def fake_daily(symbol, days):
            calls.append((symbol, days))
            return _daily_bars(260, start=100.0, step=1.0)

        ep = _endpoint(self._app(store, extras={"daily_bars": fake_daily}),
                       "/api/candles")
        out = ep(symbol="TESTA", tf="1y")
        self.assertEqual(out["tf"], "1y")
        self.assertEqual(len(out["bars"]), 250)          # last 250 sessions
        self.assertEqual(out["bars"][0]["close"], 110.0)  # 260 fetched, 10 cut
        self.assertEqual(out["bars"][-1]["close"], 359.0)
        self.assertEqual(calls, [("TESTA", dashboard.DAILY_BARS_CACHE_DAYS)])
        # cached in the DB under tf='1d' and fresh: no second REST fetch
        self.assertTrue(store.candles_load("TESTA", "1d", 300))
        out2 = ep(symbol="TESTA", tf="1m")
        self.assertEqual(len(out2["bars"]), 30)
        out3 = ep(symbol="TESTA", tf="6m")
        self.assertEqual(len(out3["bars"]), 130)
        self.assertEqual(len(calls), 1)

    def test_daily_tf_503_without_extra_and_no_cache(self):
        store = Store(":memory:")
        ep = _endpoint(self._app(store), "/api/candles")
        with self.assertRaises(HTTPException) as cm:
            ep(symbol="TESTA", tf="1y")
        self.assertEqual(cm.exception.status_code, 503)

    def test_intraday_default_unchanged(self):
        store = Store(":memory:")
        bars1m = [{"ts": 1_700_000_000 + i * 60, "o": 10.0, "h": 10.5,
                   "l": 9.5, "c": 10.2, "v": 5} for i in range(3)]
        store.candles_save("TESTA", "1m", bars1m)
        ep = _endpoint(self._app(store), "/api/candles")
        out = ep(symbol="TESTA")
        self.assertEqual(out["tf"], "1d")
        self.assertEqual(len(out["bars"]), 3)
        self.assertEqual(out["bars"][0]["close"], 10.2)

    def test_unknown_tf_rejected(self):
        store = Store(":memory:")
        ep = _endpoint(self._app(store), "/api/candles")
        with self.assertRaises(HTTPException) as cm:
            ep(symbol="TESTA", tf="3y")
        self.assertEqual(cm.exception.status_code, 400)


class TestApiQuotesFallback(unittest.TestCase):
    def _app(self, store, market, extras=None):
        return dashboard.create_app(store, MagicMock(), market, {},
                                    extras=extras)

    def test_fallback_to_last_close_with_stale_flag(self):
        store = Store(":memory:")
        store.watchlist_add("TESTA")
        calls = []

        def fake_daily(symbol, days):
            calls.append(symbol)
            return _daily_bars(5)  # closes 70,80,90,100,110

        ep = _endpoint(self._app(store, {}, {"daily_bars": fake_daily}),
                       "/api/quotes")
        out = ep()
        self.assertEqual(out["TESTA"]["price"], 110.0)
        self.assertAlmostEqual(out["TESTA"]["day_pct"], 10.0)
        self.assertIs(out["TESTA"]["stale"], True)
        # second call same day: served from the per-symbol close cache,
        # no new REST fetch
        out2 = ep()
        self.assertEqual(out2["TESTA"]["price"], 110.0)
        self.assertEqual(calls, ["TESTA"])

    def test_live_tick_wins_and_skips_fallback(self):
        store = Store(":memory:")
        store.watchlist_add("TESTB")
        st = SimpleNamespace(price=50.0, prev_close=40.0, ts=123.0,
                             forming=None)
        calls = []
        ep = _endpoint(
            self._app(store, {"TESTB": st},
                      {"daily_bars": lambda s, d: calls.append(s) or []}),
            "/api/quotes")
        out = ep()
        self.assertEqual(out["TESTB"]["price"], 50.0)
        self.assertAlmostEqual(out["TESTB"]["day_pct"], 25.0)
        self.assertIs(out["TESTB"]["stale"], False)
        self.assertEqual(calls, [])

    def test_no_tick_no_bars_stays_waiting(self):
        store = Store(":memory:")
        store.watchlist_add("TESTC")
        ep = _endpoint(
            self._app(store, {}, {"daily_bars": lambda s, d: []}),
            "/api/quotes")
        out = ep()
        self.assertIsNone(out["TESTC"]["price"])
        self.assertIs(out["TESTC"]["stale"], False)


class TestApiStock(unittest.TestCase):
    """GET /api/stock/{symbol}: price/levels/verdict/fundamentals/sentiment/
    profile bundle — all from cached bars and the 24h pipelines (mocked)."""

    _SENT = {"sentiment": 0.2, "news": 0.1, "analyst": 0.3, "n": 4,
             "analyst_counts": {"strongBuy": 1, "buy": 5, "hold": 3,
                                "sell": 1, "strongSell": 0, "total": 10,
                                "period": "0-3M"},
             "label": "mildly bullish", "fetched_at": "2026-10-20T00:00:00Z"}
    _FUND = {"symbol": "TESTA",
             "piotroski": {"score": 7, "of": 9, "evaluated": 9, "tests": {}},
             "altman": {"z": 3.2, "zone": "safe", "parts": {}},
             "dcf": {"fair_value": 150.0}, "data_year": 2024, "notes": [],
             "price": 117.5, "fetched_at": "2026-10-20T00:00:00Z"}

    @staticmethod
    def _bars226(symbol, days):
        # closes climb 50.0 -> 117.5 by 0.3/session; h=c+2, l=c-2, vol const
        base = 1_700_000_000
        return [{"ts": base + i * 86400, "o": 50.0 + i * 0.3,
                 "h": 52.0 + i * 0.3, "l": 48.0 + i * 0.3,
                 "c": 50.0 + i * 0.3, "v": 1_000_000} for i in range(226)]

    def _app(self, store, bars_fn=None):
        extras = {"daily_bars": bars_fn or self._bars226}
        return dashboard.create_app(store, MagicMock(), {}, {},
                                    extras=extras)

    def _patched(self):
        return (patch.object(dashboard.sentiment_mod, "get_sentiment",
                             return_value=dict(self._SENT)),
                patch.object(dashboard.fundamentals, "get_scores",
                             return_value=dict(self._FUND)),
                patch.dict("os.environ", {"FINNHUB_KEY": "testkey"}))

    @staticmethod
    def _profile_resp():
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        # Real profile2 shape: industry is 'finnhubIndustry'; there are
        # no 'sector'/'industry' keys.
        resp.json.return_value = {
            "ticker": "TESTA", "name": "Test Inc",
            "finnhubIndustry": "Software", "exchange": "NASDAQ",
            "currency": "USD", "marketCapitalization": 123456.0}
        return resp

    def test_full_bundle_shape_and_levels(self):
        store = Store(":memory:")
        p_sent, p_fund, p_env = self._patched()
        with p_sent, p_fund, p_env, patch.object(
                dashboard.requests, "get",
                return_value=self._profile_resp()):
            ep = _endpoint(self._app(store), "/api/stock/{symbol}")
            out = ep(symbol="TESTA")
        self.assertEqual(out["symbol"], "TESTA")
        # price block: last close of the synthetic series, not live
        self.assertEqual(out["price"]["last"], 117.5)
        self.assertIs(out["price"]["live"], False)
        self.assertAlmostEqual(out["price"]["day_pct"],
                               (117.5 - 117.2) / 117.2 * 100)
        # stats over the 52w window of the series
        self.assertEqual(out["stats"]["high_52w"], 119.5)
        self.assertEqual(out["stats"]["low_52w"], 48.0)
        self.assertEqual(out["stats"]["bars"], 226)
        self.assertAlmostEqual(
            out["stats"]["avg_dollar_volume_20d"], 114_650_000.0)
        # levels: exact definitions
        lv = out["levels"]
        self.assertEqual(lv["support"]["level"], 97.8)      # min low, last 60
        self.assertEqual(lv["resistance"]["level"], 119.5)  # max high, last 60
        self.assertEqual(lv["major_resistance"]["level"], 119.5)  # 52w high
        self.assertIsNotNone(lv["breakout"]["level"])       # BB upper
        self.assertIsNotNone(lv["breakdown"]["level"])      # BB lower
        self.assertGreater(lv["breakout"]["level"], lv["breakdown"]["level"])
        self.assertEqual(lv["buy_zone"]["level"], lv["breakout"]["level"])
        self.assertIn("breakout", lv["buy_zone"]["note"])
        closes = [50.0 + i * 0.3 for i in range(226)]
        self.assertAlmostEqual(lv["sma50"], sum(closes[-50:]) / 50)
        self.assertAlmostEqual(lv["sma200"], sum(closes[-200:]) / 200)
        self.assertIsNotNone(lv["rsi14"])
        self.assertAlmostEqual(lv["vol_ratio"], 1.0)  # constant volume
        # scanner verdict: real technical_screen on the same bars
        v = out["scanner"]
        self.assertIn(v["side"], ("BUY", "SELL"))
        self.assertEqual(v["composite"],
                         v["tech_score"] + v["fundamental_bonus"])
        self.assertTrue(v["rating"].endswith("/10"))
        names = {s["name"] for s in v["setups"]}
        self.assertIn("uptrend", names)  # rising series
        # fundamentals + sentiment passthrough from the cached pipelines
        self.assertEqual(out["fundamentals"]["piotroski"]["score"], 7)
        self.assertEqual(out["fundamentals"]["data_year"], 2024)
        self.assertEqual(out["sentiment"]["analyst_counts"]["total"], 10)
        # company profile from Finnhub (industry via finnhubIndustry;
        # profile2 carries no sector field)
        self.assertIsNone(out["company"]["sector"])
        self.assertEqual(out["company"]["industry"], "Software")
        self.assertEqual(out["company"]["market_cap_musd"], 123456.0)

    def test_profile_cached_24h(self):
        store = Store(":memory:")
        p_sent, p_fund, p_env = self._patched()
        with p_sent, p_fund, p_env, patch.object(
                dashboard.requests, "get",
                return_value=self._profile_resp()) as mock_get:
            ep = _endpoint(self._app(store), "/api/stock/{symbol}")
            ep(symbol="TESTA")
            ep(symbol="TESTA")
        # The profile itself is fetched once, then served from the 24h
        # cache. (The stock bundle also makes one 24h-cached earnings-
        # calendar call on the first render — not a profile call.)
        profile_calls = [c for c in mock_get.call_args_list
                         if "profile2" in str(c.args[0])]
        self.assertEqual(len(profile_calls), 1)

    def test_invalid_symbol_400(self):
        store = Store(":memory:")
        ep = _endpoint(self._app(store), "/api/stock/{symbol}")
        for bad in ("AA$1", "TOOLONG7", "A B", ""):
            with self.assertRaises(HTTPException) as cm:
                ep(symbol=bad)
            self.assertEqual(cm.exception.status_code, 400)

    def test_no_bars_404(self):
        store = Store(":memory:")
        app = self._app(store, bars_fn=lambda s, d: [])
        ep = _endpoint(app, "/api/stock/{symbol}")
        with self.assertRaises(HTTPException) as cm:
            ep(symbol="ZZZZ")
        self.assertEqual(cm.exception.status_code, 404)

    def test_few_bars_partial_levels_and_no_verdict(self):
        store = Store(":memory:")

        def few(symbol, days):
            return [{"ts": 1_700_000_000 + i * 86400, "o": 10.0, "h": 10.5,
                     "l": 9.5, "c": 10.0, "v": 5000} for i in range(30)]

        p_sent, p_fund, p_env = self._patched()
        with p_sent, p_fund, p_env:
            ep = _endpoint(self._app(store, bars_fn=few),
                           "/api/stock/{symbol}")
            out = ep(symbol="TESTB")
        self.assertEqual(out["price"]["last"], 10.0)
        self.assertIsNone(out["levels"]["support"]["level"])   # needs 60
        self.assertIsNone(out["levels"]["resistance"]["level"])  # needs 60
        self.assertIsNone(out["levels"]["sma200"])        # needs 200
        self.assertIsNone(out["levels"]["sma50"])          # needs 50
        self.assertIsNotNone(out["levels"]["breakout"])   # BB needs only 20
        self.assertIsNone(out["scanner"])                 # screen needs 60
        self.assertIsNone(out["company"])                 # no FINNHUB profile

    def test_fundamentals_failure_degrades_not_500(self):
        store = Store(":memory:")
        p_sent, _p_fund, p_env = self._patched()
        with p_sent, p_env, patch.object(
                dashboard.fundamentals, "get_scores",
                side_effect=RuntimeError("FINNHUB_KEY not set")):
            ep = _endpoint(self._app(store), "/api/stock/{symbol}")
            out = ep(symbol="TESTA")
        self.assertIn("error", out["fundamentals"])
        self.assertIn("FINNHUB_KEY", out["fundamentals"]["error"])
        # the rest of the bundle still renders (verdict bonuses degrade to 0)
        self.assertEqual(out["price"]["last"], 117.5)
        self.assertIsNotNone(out["scanner"])
        self.assertEqual(out["scanner"]["fundamental_bonus"], 0)

    def test_verdict_reasons_include_earnings_flag(self):
        # When earnings are imminent, the scanner's earnings note is
        # attached to the verdict row and surfaces in its reasons (the
        # same _candidate_reasons path as the push/dashboard entries).
        store = Store(":memory:")
        p_sent, p_fund, p_env = self._patched()
        with p_sent, p_fund, p_env, patch.object(
                dashboard.requests, "get",
                return_value=self._profile_resp()), \
             patch.object(dashboard.scanner_mod, "earnings_note",
                          return_value="⚠ earnings Oct 22"):
            ep = _endpoint(self._app(store), "/api/stock/{symbol}")
            out = ep(symbol="TESTA")
        self.assertIn("⚠ earnings Oct 22", out["scanner"]["reasons"])

    @staticmethod
    def _bars_from_closes(closes):
        base = 1_700_000_000
        return [{"ts": base + i * 86400, "o": c, "h": c + 1.0,
                 "l": c - 1.0, "c": c, "v": 1_000_000}
                for i, c in enumerate(closes)]

    def _levels_for(self, closes):
        # Offline bundle fetch for crafted close series: profile via the
        # mocked requests.get, earnings note neutralised, sentiment and
        # fundamentals via the class fixtures.
        store = Store(":memory:")
        bars = self._bars_from_closes(closes)
        p_sent, p_fund, p_env = self._patched()
        with p_sent, p_fund, p_env, patch.object(
                dashboard.requests, "get",
                return_value=self._profile_resp()), patch.object(
                dashboard.scanner_mod, "earnings_note", return_value=None):
            ep = _endpoint(self._app(store, bars_fn=lambda s, d: bars),
                           "/api/stock/{symbol}")
            out = ep(symbol="TESTA")
        return out["levels"]

    def test_pct_b_positions(self):
        # Bands are symmetric around the 20-session mean, so a last close
        # exactly on the mean is %B 0.5. Window sums to 2000 (mean 100)
        # and ends on 100.
        win = [90, 110, 92, 108, 94, 106, 96, 104, 98, 102,
               102, 98, 104, 96, 106, 94, 108, 92, 100, 100]
        lv = self._levels_for([100.0] * 40 + win)
        self.assertEqual(lv["pct_b"], 0.5)
        # Window 16x100 + 4x90: mean 98, sd 4, lower band 90 == last close.
        lv = self._levels_for([100.0] * 40 + [100, 100, 100, 100, 90] * 4)
        self.assertEqual(lv["pct_b"], 0.0)
        # Mirror image: mean 102, sd 4, upper band 110 == last close.
        lv = self._levels_for([100.0] * 40 + [100, 100, 100, 100, 110] * 4)
        self.assertEqual(lv["pct_b"], 1.0)
        # A spike close past the upper band -> %B above 1.
        lv = self._levels_for([100.0] * 59 + [120.0])
        self.assertGreater(lv["pct_b"], 1.0)
        # Flat series: bands degenerate (upper == lower) -> %B is None.
        lv = self._levels_for([100.0] * 60)
        self.assertIsNone(lv["pct_b"])

    def test_pct_b_matches_indicator_math(self):
        closes = [50.0 + i * 0.3 for i in range(226)]
        lv = self._levels_for(closes)
        _mid, up, lo = dashboard.ind.bollinger_bands(closes)
        expect = round(dashboard.ind.bollinger_pctb(closes[-1], up, lo), 2)
        self.assertEqual(lv["pct_b"], expect)
        self.assertEqual(lv["pct_b"], 0.91)  # pinned for this fixture
        bw = dashboard.ind.bollinger_bandwidth(closes)
        self.assertEqual(lv["bandwidth"], round(bw, 4))

    def test_bandwidth_context_coiled(self):
        # Amplitude decays every session, so the current bandwidth is the
        # tightest of its own last-126-session history -> coiled.
        closes = [100.0 + (10 * 0.97 ** i) * (1 if i % 2 == 0 else -1)
                  for i in range(200)]
        lv = self._levels_for(closes)
        self.assertIsNotNone(lv["bandwidth"])
        self.assertEqual(lv["bandwidth_context"], "coiled")

    def test_bandwidth_context_wide(self):
        # Amplitude grows every session -> current bandwidth is at the
        # stretched end of its history -> wide.
        closes = [100.0 + (0.05 * 1.025 ** i) * (1 if i % 2 == 0 else -1)
                  for i in range(180)]
        lv = self._levels_for(closes)
        self.assertEqual(lv["bandwidth_context"], "wide")

    def test_bandwidth_context_normal(self):
        # Stepped amplitudes (quiet, wild, middling): the current
        # bandwidth sits mid-range of its history -> normal.
        closes = []
        for amp, n in ((2, 70), (8, 70), (5, 70)):
            closes += [100.0 + amp * (1 if i % 2 == 0 else -1)
                       for i in range(n)]
        lv = self._levels_for(closes)
        self.assertEqual(lv["bandwidth_context"], "normal")

    def test_bandwidth_context_none_without_history(self):
        # 100 sessions: bandwidth exists (needs 20) but the 126-session
        # percentile history does not -> context stays None.
        closes = [100.0 + (3 if i % 2 == 0 else -3) for i in range(100)]
        lv = self._levels_for(closes)
        self.assertIsNotNone(lv["bandwidth"])
        self.assertIsNone(lv["bandwidth_context"])

    def test_breakout_note_states_confirmation_rule(self):
        lv = self._levels_for([50.0 + i * 0.3 for i in range(226)])
        note = lv["buy_zone"]["note"]
        self.assertIn("confirmed only with volume ≥1.5× the 20-day "
                      "average", note)
        self.assertIn("second daily close above the level", note)
        self.assertIn("possible fake-out", note)
        self.assertNotIn("entry idea", note)

    def test_stock_page_uses_breakout_level_label(self):
        store = Store(":memory:")
        ep = _endpoint(self._app(store), "/stock")
        html = ep()
        self.assertIn("Breakout level", html)
        self.assertIn("Breakout level:", html)  # note paragraph prefix
        self.assertNotIn("Buy zone", html)
        self.assertNotIn("buy zone", html)
        self.assertNotIn("Breakout point", html)
        # %B / bandwidth rows and their plain-meaning text ship in the JS
        self.assertIn("%B", html)
        self.assertIn("Bandwidth", html)
        self.assertIn("hugging the lower band", html)
        self.assertIn("coiled", html)

    def test_page_route_renders(self):
        store = Store(":memory:")
        ep = _endpoint(self._app(store), "/stock")
        html = ep()
        for marker in ("Scanner verdict", "levelsTable", "fundTable",
                       "sentCard", "/api/stock/", "Levels"):
            self.assertIn(marker, html)


from datetime import datetime
from zoneinfo import ZoneInfo

import dashboard as _dash_mod


class TestDistanceHelpers(unittest.TestCase):
    def test_above(self):
        # The CRM case from the bug report: level $246.48, price $242.06.
        self.assertEqual(_dash_mod._distance_text(246.48, 242.06),
                         "+1.8% above")
        self.assertAlmostEqual(_dash_mod._distance_pct(246.48, 242.06),
                               1.826, places=2)

    def test_below(self):
        self.assertEqual(_dash_mod._distance_text(175.97, 242.06),
                         "-27.3% below")

    def test_at_price_and_missing(self):
        self.assertEqual(_dash_mod._distance_text(100.0, 100.0), "+0.0% above")
        self.assertEqual(_dash_mod._distance_text(None, 100.0), "—")
        self.assertEqual(_dash_mod._distance_text(100.0, None), "—")
        self.assertIsNone(_dash_mod._distance_pct(100.0, 0))


class TestLevelRows(unittest.TestCase):
    def _levels(self, major=150.0):
        return {"support": {"level": 100.0, "kind": "60-session swing low"},
                "resistance": {"level": 150.0,
                               "kind": "60-session swing high"},
                "major_resistance": {"level": major, "kind": "52-week high"},
                "breakout": {"level": 155.0,
                             "kind": "Bollinger upper band (20d, 2σ)"},
                "breakdown": {"level": 95.0,
                              "kind": "Bollinger lower band (20d, 2σ)"},
                "sma50": 140.0, "sma200": 120.0}

    def test_resistance_equal_52w_high_collapses(self):
        rows = _dash_mod._level_rows(self._levels(), 145.0)
        merged = [r for r in rows if r["value"] == 150.0]
        self.assertEqual(len(merged), 1)
        self.assertIn("Resistance", merged[0]["name"])
        self.assertIn("52w high", merged[0]["name"])
        self.assertEqual(len(rows), 6)  # 5 price rows - 1 merged + 2 SMA

    def test_distinct_52w_high_stays_separate(self):
        rows = _dash_mod._level_rows(self._levels(major=160.0), 145.0)
        self.assertEqual(len(rows), 7)
        self.assertFalse(any("/" in r["name"] for r in rows))

    def test_all_rows_use_shared_distance(self):
        rows = _dash_mod._level_rows(self._levels(), 145.0)
        for r in rows:
            self.assertEqual(r["distance_text"],
                             _dash_mod._distance_text(r["value"], 145.0))


class _FakeDT(datetime):
    """datetime stand-in with a pinned now() (fromtimestamp etc. inherit)."""
    _now = None

    @classmethod
    def now(cls, tz=None):
        base = cls._now
        return base if tz is None else base.astimezone(tz)


class TestApiStockFixes(unittest.TestCase):
    _SENT = TestApiStock._SENT
    _FUND = TestApiStock._FUND

    def _app(self, store, bars):
        extras = {"daily_bars": lambda s, d: bars}
        return dashboard.create_app(store, MagicMock(), {}, {},
                                    extras=extras)

    def _patched(self):
        return (patch.object(dashboard.sentiment_mod, "get_sentiment",
                             return_value=dict(self._SENT)),
                patch.object(dashboard.fundamentals, "get_scores",
                             return_value=dict(self._FUND)),
                patch.object(dashboard.scanner_mod, "earnings_note",
                             return_value=None),
                patch.dict("os.environ", {"FINNHUB_KEY": "testkey"}))

    def _bundle(self, bars):
        store = Store(":memory:")
        p_sent, p_fund, p_note, p_env = self._patched()
        with p_sent, p_fund, p_note, p_env, patch.object(
                dashboard.requests, "get",
                return_value=TestApiStock._profile_resp()):
            ep = _endpoint(self._app(store, bars), "/api/stock/{symbol}")
            return ep(symbol="TESTA")

    def test_rows_collapse_and_breakout_distance_agrees(self):
        out = self._bundle(TestApiStock._bars226(None, 226))
        lv = out["levels"]
        # Rising series: the 60-session high IS the 52w high -> one row.
        merged = [r for r in lv["rows"] if r["value"] == 119.5]
        self.assertEqual(len(merged), 1)
        self.assertIn("Resistance", merged[0]["name"])
        self.assertIn("52w high", merged[0]["name"])
        # Table cell and breakout note carry the SAME distance string,
        # computed off the one snapshot price (the header price).
        brow = [r for r in lv["rows"] if "Breakout level" in r["name"]]
        self.assertEqual(len(brow), 1)
        self.assertEqual(brow[0]["distance_text"],
                         lv["buy_zone"]["distance_text"])
        self.assertEqual(
            brow[0]["distance_text"],
            _dash_mod._distance_text(lv["breakout"]["level"],
                                     out["price"]["last"]))

    def test_verdict_reasons_not_duplicated(self):
        out = self._bundle(TestApiStock._bars226(None, 226))
        v = out["scanner"]
        setup_reasons = {s["reason"] for s in v["setups"]}
        self.assertTrue(setup_reasons)  # the fixture series fires setups
        # No setup reason appears a second time in the reasons list...
        self.assertFalse(setup_reasons & set(v["reasons"]))
        # ...while the fundamental-overlay reasons do show (once).
        self.assertTrue(any("Piotroski" in r for r in v["reasons"]))

    def _bars_ending(self, end_ts, n=226):
        return [{"ts": end_ts - (n - 1 - i) * 86400, "o": 50.0 + i * 0.3,
                 "h": 52.0 + i * 0.3, "l": 48.0 + i * 0.3,
                 "c": 50.0 + i * 0.3, "v": 1_000_000} for i in range(n)]

    def test_vol_ratio_partial_during_session(self):
        et = ZoneInfo("America/New_York")
        end_ts = datetime(2026, 10, 20, 12, 0, tzinfo=et).timestamp()
        _FakeDT._now = datetime(2026, 10, 20, 11, 0, tzinfo=et)
        with patch.object(dashboard, "datetime", _FakeDT):
            out = self._bundle(self._bars_ending(end_ts))
        self.assertTrue(out["levels"]["vol_ratio_partial"])

    def test_vol_ratio_not_partial_after_close_or_next_day(self):
        et = ZoneInfo("America/New_York")
        end_ts = datetime(2026, 10, 20, 12, 0, tzinfo=et).timestamp()
        _FakeDT._now = datetime(2026, 10, 20, 17, 30, tzinfo=et)  # after 4pm
        with patch.object(dashboard, "datetime", _FakeDT):
            out = self._bundle(self._bars_ending(end_ts))
        self.assertFalse(out["levels"]["vol_ratio_partial"])
        _FakeDT._now = datetime(2026, 10, 21, 11, 0, tzinfo=et)  # next day
        with patch.object(dashboard, "datetime", _FakeDT):
            out = self._bundle(self._bars_ending(end_ts))
        self.assertFalse(out["levels"]["vol_ratio_partial"])


if __name__ == "__main__":
    unittest.main()
