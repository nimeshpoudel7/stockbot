"""Tests for scanner.py: setup flags, weights, trading-day guard, dry-run."""
import re
import unittest
from unittest.mock import MagicMock, patch

import scanner
import sentiment
import sessions as sessions_mod
from storage import Store


# A clean dip-buy chart: flat, then a 10-day slide to RSI < 30 on
# capitulation volume (when last_vol is high) -> BUY dip score 100.
BUY_CLOSES = [100.0] * 240 + [100.0 - 3 * i for i in range(1, 11)]
# A clean sell-the-rip chart: long downtrend, then a sharp rally into it
# -> SELL score 100.
SELL_CLOSES = [160.0 - i * 0.3 for i in range(240)] + \
    [88.0 + i * 2.5 for i in range(1, 11)]


def make_bars(closes, vol=1_000_000.0, last_vol=None):
    """Synthetic daily bars from a close series. o/h/l derived simply."""
    bars = []
    for i, c in enumerate(closes):
        v = last_vol if (last_vol is not None and i == len(closes) - 1) else vol
        bars.append({"ts": 1700000000 + i * 86400, "o": c, "h": c * 1.005,
                     "l": c * 0.995, "c": c, "v": v})
    return bars


def setup_names(res):
    return {name for name, _, _ in res["setups"]}


# Neutral sentiment stub: keeps scan/watch tests offline (no Finnhub calls).
# With n=0 no news line is appended and the rating is unchanged.
_NEUTRAL_SENTIMENT = {"sentiment": 0.0, "n": 0, "label": "neutral",
                      "fetched_at": "2026-01-01T00:00:00+00:00"}


def _patch_sentiment():
    from unittest.mock import patch
    return patch("scanner.sentiment.get_sentiment",
                 return_value=_NEUTRAL_SENTIMENT)


def bear_setup_names(res):
    return {name for name, _, _ in res["bear_setups"]}





class TestFundamentalBonus(unittest.TestCase):
    def test_fetch_failure_scores_zero(self):
        store = Store(":memory:")
        with patch.object(scanner.fundamentals, "get_scores",
                          side_effect=RuntimeError("no key")):
            bonus, reasons = scanner.fundamental_bonus(store, "ZZZ", 10.0)
        self.assertEqual((bonus, reasons), (0, []))

    def test_strong_fundamentals_add_bonus(self):
        store = Store(":memory:")
        fake = {"piotroski": {"score": 8}, "altman": {"zone": "safe"},
                "dcf": {"fair_value": 150.0, "base_fcf": 100.0}}
        with patch.object(scanner.fundamentals, "get_scores",
                          return_value=fake):
            bonus, reasons = scanner.fundamental_bonus(store, "AAA", 100.0)
        # Piotroski 8 -> +10, Altman safe -> +5, DCF 33% MOS -> +10
        self.assertEqual(bonus, 25)
        self.assertEqual(len(reasons), 3)

    def test_weak_fundamentals_subtract(self):
        store = Store(":memory:")
        fake = {"piotroski": {"score": 1}, "altman": {"zone": "distress"},
                "dcf": {"fair_value": 50.0, "base_fcf": 100.0}}
        with patch.object(scanner.fundamentals, "get_scores",
                          return_value=fake):
            bonus, reasons = scanner.fundamental_bonus(store, "BBB", 100.0)
        self.assertEqual(bonus, -25)  # -10 -10 -5

    def test_dcf_leg_ignored_without_positive_base_fcf(self):
        # DCF on zero/negative/missing base FCF is meaningless: the leg
        # must neither add nor subtract, on either side.
        store = Store(":memory:")
        for dcf in ({"fair_value": 150.0, "base_fcf": 0},
                    {"fair_value": 150.0, "base_fcf": -40.0},
                    {"fair_value": 150.0}):  # base_fcf missing entirely
            fake = {"piotroski": {"score": 8}, "altman": {"zone": "safe"},
                    "dcf": dcf}
            with patch.object(scanner.fundamentals, "get_scores",
                              return_value=fake):
                bonus, reasons = scanner.fundamental_bonus(
                    store, "AAA", 100.0)
            self.assertEqual(bonus, 15)  # Piotroski +10, Altman +5, no DCF
            self.assertEqual(len(reasons), 2)

    def test_short_bonus_dcf_leg_ignored_without_positive_base_fcf(self):
        store = Store(":memory:")
        fake = {"piotroski": {"score": 1}, "altman": {"zone": "distress"},
                "dcf": {"fair_value": 50.0, "base_fcf": 0}}
        with patch.object(scanner.fundamentals, "get_scores",
                          return_value=fake):
            bonus, reasons = scanner.short_bonus(store, "BBB", 100.0)
        # Piotroski weak +10, Altman distress +10, DCF leg skipped
        self.assertEqual(bonus, 20)
        self.assertEqual(len(reasons), 2)
        # same payload WITH a positive base FCF: DCF +10 applies
        fake["dcf"] = {"fair_value": 50.0, "base_fcf": 10.0}
        with patch.object(scanner.fundamentals, "get_scores",
                          return_value=fake):
            bonus, _reasons = scanner.short_bonus(store, "BBB", 100.0)
        self.assertEqual(bonus, 30)


class TestLiquidity(unittest.TestCase):
    def test_penny_stock_with_volume_passes(self):
        # $0.50 stock, 2M shares/day -> $1M/day average dollar volume
        bars = make_bars([0.50] * 60, vol=2_000_000.0)
        self.assertTrue(scanner.passes_liquidity(bars))
        self.assertAlmostEqual(scanner.avg_dollar_volume_20d(bars), 1_000_000.0)

    def test_dead_ticker_excluded(self):
        bars = make_bars([50.0] * 60, vol=100.0)  # $5k/day
        self.assertFalse(scanner.passes_liquidity(bars))

    def test_high_price_low_volume_excluded(self):
        bars = make_bars([500.0] * 60, vol=100.0)  # $50k/day
        self.assertFalse(scanner.passes_liquidity(bars))

    def test_zero_volume_excluded(self):
        bars = make_bars([10.0] * 60, vol=0.0)
        self.assertFalse(scanner.passes_liquidity(bars))

    def test_insufficient_bars_excluded(self):
        self.assertIsNone(scanner.avg_dollar_volume_20d(make_bars([5.0] * 19)))
        self.assertFalse(scanner.passes_liquidity(make_bars([5.0] * 19)))

    def test_no_price_floor_documented(self):
        # sub-penny price with huge volume still passes: no price floor
        bars = make_bars([0.004] * 60, vol=100_000_000.0)
        self.assertTrue(scanner.passes_liquidity(bars))


class TestUniverseExpansion(unittest.TestCase):
    def _assets_response(self, assets):
        m = MagicMock()
        m.json.return_value = assets
        m.raise_for_status.return_value = None
        return m

    def test_alpaca_all_equity_universe_tradable_only(self):
        store = Store(":memory:")
        assets = [
            {"symbol": "AAPL", "tradable": True},
            {"symbol": "PENNY", "tradable": True},
            {"symbol": "DEADQ", "tradable": False},
            {"no_symbol": True},
        ]
        rest = scanner.AlpacaREST("k", "s")
        with patch.object(scanner.requests, "get",
                          return_value=self._assets_response(assets)):
            syms = scanner.refresh_universe(store, rest=rest)
        self.assertEqual(syms, ["AAPL", "PENNY"])  # sorted, tradable only

    def test_alpaca_failure_uses_sp500_fallback_chain(self):
        store = Store(":memory:")
        rest = scanner.AlpacaREST("k", "s")
        with patch.object(scanner.requests, "get",
                          side_effect=ConnectionError("blocked")):
            syms = scanner.refresh_universe(store, rest=rest)
        self.assertEqual(set(syms), set(scanner.SP500_FALLBACK))

    def test_universe_cache_key_versioned(self):
        store = Store(":memory:")
        with patch.object(scanner.requests, "get",
                          side_effect=ConnectionError("blocked")):
            scanner.refresh_universe(store, rest=None)
        self.assertIsNotNone(store.poller_get("scanner_universe_v2"))

    def test_cached_universe_reused(self):
        store = Store(":memory:")
        with patch.object(scanner.requests, "get",
                          side_effect=ConnectionError("blocked")):
            first = scanner.get_universe(store)
        with patch.object(scanner, "refresh_universe",
                          side_effect=AssertionError("should not refresh")):
            second = scanner.get_universe(store)
        self.assertEqual(first, second)


class TestBarsPagination(unittest.TestCase):
    """Regression: daily_bars_multi must follow Alpaca's next_page_token.

    The multi-symbol bars endpoint shares ONE page budget across all
    symbols in the request; with the old limit=1000 and no pagination,
    only ~5 of every 100 chunk symbols ever got bars back and the rest
    were silently never screened.
    """

    @staticmethod
    def _page(bars_by_sym, token=None):
        resp = MagicMock()
        payload = {"bars": bars_by_sym}
        if token:
            payload["next_page_token"] = token
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        return resp

    @staticmethod
    def _bar(day, close):
        return {"t": f"2026-01-{day:02d}T05:00:00Z", "o": close, "h": close,
                "l": close, "c": close, "v": 1_000_000}

    def test_follows_next_page_token_and_merges(self):
        page1 = self._page({"AAA": [self._bar(2, 10.0)]}, token="tok2")
        page2 = self._page({"AAA": [self._bar(3, 11.0)],
                            "BBB": [self._bar(3, 20.0)]})
        rest = scanner.AlpacaREST("k", "s")
        with patch.object(scanner.requests, "get",
                          side_effect=[page1, page2]) as mock_get,                 patch.object(scanner.time, "sleep", lambda *_: None):
            out = rest.daily_bars_multi(["AAA", "BBB"], days=300)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[1].kwargs["params"]
                         .get("page_token"), "tok2")
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]
                         ["limit"], 10000)
        self.assertEqual([b["c"] for b in out["AAA"]], [10.0, 11.0])
        self.assertEqual([b["c"] for b in out["BBB"]], [20.0])


class TestApiBudget(unittest.TestCase):
    def test_calls_per_pass(self):
        self.assertEqual(scanner.api_calls_per_pass(100), 1)
        self.assertEqual(scanner.api_calls_per_pass(101), 2)
        self.assertEqual(scanner.api_calls_per_pass(10_000), 100)

    def test_watch_cadence_within_free_limit(self):
        # worst realistic case: 10k symbols -> 100 calls per 15-min pass,
        # far under Alpaca's 200 req/min free limit -> no cadence change
        self.assertLessEqual(scanner.api_calls_per_pass(10_000),
                             scanner.ALPACA_REQ_PER_MIN)
        self.assertEqual(scanner.api_calls_per_pass(7_000), 70)


class TestRunScan(unittest.TestCase):
    def _store(self):
        return Store(":memory:")

    def test_trading_day_skip_no_push(self):
        push = MagicMock()
        with patch.object(sessions_mod, "is_trading_day", return_value=False):
            out = scanner.run_scan(store=self._store(), dry_run=False,
                                   push_fn=push)
        self.assertEqual(out, {"buys": [], "sells": []})
        push.assert_not_called()

    def test_dry_run_prints_without_push(self):
        closes = BUY_CLOSES
        bars = {"TESTA": make_bars(closes, last_vol=5_000_000.0)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        push = MagicMock()
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             _patch_sentiment():
            out = scanner.run_scan(store=self._store(), rest=rest,
                                   dry_run=True, push_fn=push)
        self.assertEqual(len(out["buys"]), 1)
        self.assertEqual(out["buys"][0]["symbol"], "TESTA")
        self.assertGreaterEqual(out["buys"][0]["composite"],
                                scanner._morning_bar("BUY", None))
        self.assertEqual(out["sells"], [])
        push.assert_not_called()

    def test_live_push_called_once(self):
        closes = BUY_CLOSES
        bars = {"TESTA": make_bars(closes, last_vol=5_000_000.0)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        push = MagicMock(return_value=True)
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             _patch_sentiment():
            out = scanner.run_scan(store=self._store(), rest=rest,
                                   dry_run=False, push_fn=push)
        self.assertEqual(len(out["buys"]), 1)
        push.assert_called_once()
        title, body = push.call_args[0]
        self.assertIn("Morning scan", title)
        self.assertIn("buy candidate", title)
        self.assertIn("TESTA", body)
        self.assertIn("BUY:", body)
        self.assertRegex(body, r"\d\.\d/10")  # rating, not raw score

    def test_two_sided_push(self):
        bull_bars = {"BULLA": make_bars(BUY_CLOSES,
                                        last_vol=5_000_000.0)}
        bear_bars = {"BEARA": make_bars(SELL_CLOSES)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = {**bull_bars, **bear_bars}
        push = MagicMock(return_value=True)
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe",
                          return_value=["BULLA", "BEARA"]), \
             patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             _patch_sentiment():
            out = scanner.run_scan(store=self._store(), rest=rest,
                                   dry_run=False, push_fn=push)
        self.assertEqual([r["symbol"] for r in out["buys"]], ["BULLA"])
        self.assertEqual([r["symbol"] for r in out["sells"]], ["BEARA"])
        push.assert_called_once()  # one push, two sections
        title, body = push.call_args[0]
        self.assertIn("buy candidate", title)
        self.assertIn("sell candidate", title)
        self.assertIn("BUY CANDIDATES", body)
        self.assertIn("SELL CANDIDATES", body)
        self.assertIn("BUY: BULLA", body)
        self.assertIn("SELL: BEARA", body)
        self.assertRegex(body, r"\d\.\d/10")  # both sections rated out of 10

    def test_illiquid_setup_filtered_out(self):
        # strong setup (score 55, above the 45 bar) but only $176k/day
        # average dollar volume -> dropped by the liquidity gate alone
        closes = BUY_CLOSES
        bars = {"THIN": make_bars(closes, vol=1_000.0, last_vol=3_000.0)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["THIN"]):
            out = scanner.run_scan(store=self._store(), rest=rest,
                                   dry_run=True)
        self.assertEqual(out, {"buys": [], "sells": []})


class TestWikipediaParser(unittest.TestCase):
    def test_parse_constituents_table(self):
        html = (
            '<table class="wikitable sortable" id="constituents">'
            "<tr><th>Symbol</th></tr>"
            '<tr><td><a href="/wiki/Apple_Inc.">AAPL</a></td></tr>'
            "<tr><td>BRK.B</td></tr>"
            "<tr><td>not a ticker!!!</td></tr>"
            "</table>"
        )
        self.assertEqual(scanner._parse_sp500_wikipedia(html),
                         ["AAPL", "BRK-B"])

    def test_parse_missing_table_returns_none(self):
        self.assertIsNone(scanner._parse_sp500_wikipedia("<html></html>"))


class TestWatchMode(unittest.TestCase):
    def _store(self):
        return Store(":memory:")

    def _watch_bars(self):
        closes = BUY_CLOSES
        return {"TESTA": make_bars(closes, last_vol=5_000_000.0)}

    def _run_watch(self, store, bars, push):
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             _patch_sentiment():
            return scanner.watch_scan(store=store, rest=rest,
                                      dry_run=False, push_fn=push)

    def test_new_match_alerts_then_dedupes(self):
        store, push = self._store(), MagicMock(return_value=True)
        first = self._run_watch(store, self._watch_bars(), push)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["symbol"], "TESTA")
        self.assertEqual(first[0]["side"], "BUY")
        push.assert_called_once()
        title, body = push.call_args[0]
        self.assertIn("BUY SETUP", title)
        self.assertIn("TESTA", title)  # single match -> named in the title
        self.assertRegex(title, r"\d\.\d/10")  # rating in title
        self.assertIn("BUY: TESTA", body)
        self.assertRegex(body, r"\d\.\d/10")  # rating in body line

        push.reset_mock()
        second = self._run_watch(store, self._watch_bars(), push)
        self.assertEqual(second, [])
        push.assert_not_called()

    def test_sell_setup_labeled(self):
        store, push = self._store(), MagicMock(return_value=True)
        bars = {"BEARA": make_bars(SELL_CLOSES)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["BEARA"]), \
             _patch_sentiment():
            out = scanner.watch_scan(store=store, rest=rest,
                                     dry_run=False, push_fn=push)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["side"], "SELL")
        push.assert_called_once()
        title, body = push.call_args[0]
        self.assertIn("SELL SETUP", title)
        self.assertIn("BEARA", title)
        self.assertRegex(title, r"\d\.\d/10")
        self.assertIn("SELL: BEARA", body)
        self.assertRegex(body, r"\d\.\d/10")
        self.assertNotIn("BUY", title)

    def test_mixed_watch_labels(self):
        store, push = self._store(), MagicMock(return_value=True)
        bars = {"BULLA": make_bars(BUY_CLOSES,
                                   last_vol=5_000_000.0),
                "BEARA": make_bars(SELL_CLOSES)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe",
                          return_value=["BULLA", "BEARA"]), \
             _patch_sentiment():
            out = scanner.watch_scan(store=store, rest=rest,
                                     dry_run=False, push_fn=push)
        self.assertEqual({r["symbol"]: r["side"] for r in out},
                         {"BULLA": "BUY", "BEARA": "SELL"})
        push.assert_called_once()
        title, body = push.call_args[0]
        self.assertIn("BUY SETUP", title)
        self.assertIn("SELL SETUP", title)
        self.assertIn("BUY: BULLA", body)
        self.assertIn("SELL: BEARA", body)
        self.assertRegex(body, r"\d\.\d/10")

    def test_off_hours_exits_quietly(self):
        store, push = self._store(), MagicMock()
        rest = MagicMock()
        with patch.object(scanner, "_in_market_hours", return_value=False):
            out = scanner.watch_scan(store=store, rest=rest,
                                     dry_run=False, push_fn=push)
        self.assertEqual(out, [])
        push.assert_not_called()
        rest.daily_bars_multi.assert_not_called()

    def test_seen_list_resets_each_day(self):
        import datetime as dt, json
        store, push = self._store(), MagicMock(return_value=True)
        yesterday = (scanner._et_now() - dt.timedelta(days=1)).date().isoformat()
        store.poller_set(scanner.WATCH_SEEN_KEY,
                         json.dumps({"date": yesterday, "symbols": ["TESTA"]}))
        out = self._run_watch(store, self._watch_bars(), push)
        self.assertEqual(len(out), 1)  # yesterday's seen doesn't carry over
        push.assert_called_once()

    def test_watch_dry_run_no_push(self):
        store, push = self._store(), MagicMock()
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._watch_bars()
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             _patch_sentiment():
            out = scanner.watch_scan(store=store, rest=rest,
                                     dry_run=True, push_fn=push)
        self.assertEqual(len(out), 1)
        push.assert_not_called()

    def test_morning_scan_resets_seen(self):
        import json
        store = self._store()
        store.poller_set(scanner.WATCH_SEEN_KEY,
                         json.dumps({"date": scanner._et_today_str(),
                                     "symbols": ["TESTA"]}))
        rest = MagicMock()
        rest.daily_bars_multi.return_value = {}
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=[]):
            scanner.run_scan(store=store, rest=rest, dry_run=False,
                             push_fn=MagicMock(return_value=True))
        seen = json.loads(store.poller_get(scanner.WATCH_SEEN_KEY))
        self.assertEqual(seen["symbols"], [])

    def test_morning_dry_run_keeps_seen(self):
        # a dry run must not re-arm today's already-sent watch alerts
        import json
        store = self._store()
        store.poller_set(scanner.WATCH_SEEN_KEY,
                         json.dumps({"date": scanner._et_today_str(),
                                     "symbols": ["TESTA|BUY"]}))
        rest = MagicMock()
        rest.daily_bars_multi.return_value = {}
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=[]):
            scanner.run_scan(store=store, rest=rest, dry_run=True)
        seen = json.loads(store.poller_get(scanner.WATCH_SEEN_KEY))
        self.assertEqual(seen["symbols"], ["TESTA|BUY"])


class TestScannerPersistence(unittest.TestCase):
    """Ranked lists persist to poller_state so the dashboard can show them."""

    _RATING_RE = r"^\d{1,2}\.\d/10$"

    def _store(self):
        return Store(":memory:")

    def _bars_for(self, sym="TESTA"):
        closes = BUY_CLOSES
        return {sym: make_bars(closes, last_vol=5_000_000.0)}

    def _run_live_scan(self, store):
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._bars_for("TESTA")
        push = MagicMock(return_value=True)
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             _patch_sentiment():
            out = scanner.run_scan(store=store, rest=rest,
                                   dry_run=False, push_fn=push)
        return out, push

    def _run_watch(self, store, sym, push):
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._bars_for(sym)
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=[sym]), \
             _patch_sentiment():
            return scanner.watch_scan(store=store, rest=rest,
                                      dry_run=False, push_fn=push)

    def test_run_scan_persists_morning_payload(self):
        import datetime as dt, json
        store = self._store()
        out, _push = self._run_live_scan(store)
        self.assertEqual(len(out["buys"]), 1)
        raw = store.poller_get(scanner.SCAN_MORNING_KEY)
        self.assertIsNotNone(raw)
        data = json.loads(raw)
        self.assertEqual(data["kind"], "morning")
        # ts is an ISO timestamp carrying a UTC offset (America/Toronto)
        ts = dt.datetime.fromisoformat(data["ts"])
        self.assertIsNotNone(ts.tzinfo)
        self.assertEqual(len(data["buys"]), 1)
        entry = data["buys"][0]
        self.assertEqual(entry["symbol"], "TESTA")
        self.assertAlmostEqual(entry["price"], 70.0)
        self.assertGreaterEqual(entry["score"],
                                scanner._morning_bar("BUY", None, "dip"))
        self.assertRegex(entry["rating"], self._RATING_RE)
        self.assertIn("dip buy", entry["breakdown"])
        self.assertIn("stop $", entry["breakdown"])
        self.assertIn("capitulation volume", "; ".join(entry["reasons"]))
        self.assertEqual(data["sells"], [])

    def test_dry_run_does_not_persist(self):
        store = self._store()
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._bars_for("TESTA")
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             _patch_sentiment():
            scanner.run_scan(store=store, rest=rest, dry_run=True,
                             push_fn=MagicMock())
        self.assertIsNone(store.poller_get(scanner.SCAN_MORNING_KEY))

    def test_persist_failure_does_not_break_push(self):
        store = self._store()
        real_set = store.poller_set

        def flaky(key, value):
            if key == scanner.SCAN_MORNING_KEY:
                raise RuntimeError("db down")
            return real_set(key, value)

        store.poller_set = flaky
        out, push = self._run_live_scan(store)
        self.assertEqual(len(out["buys"]), 1)
        push.assert_called_once()
        self.assertIsNone(store.poller_get(scanner.SCAN_MORNING_KEY))

    def test_watch_accumulates_same_day(self):
        import json
        store, push = self._store(), MagicMock(return_value=True)
        self._run_watch(store, "TESTA", push)
        data = json.loads(store.poller_get(scanner.SCAN_WATCH_KEY))
        self.assertEqual(data["kind"], "watch")
        self.assertEqual(data["date"], scanner._et_today_str())
        self.assertEqual([e["symbol"] for e in data["entries"]], ["TESTA"])
        entry = data["entries"][0]
        self.assertEqual(entry["side"], "BUY")
        self.assertAlmostEqual(entry["price"], 70.0)
        self.assertRegex(entry["rating"], self._RATING_RE)
        self.assertIn("deeply oversold", entry["breakdown"])
        self.assertIn("target $", entry["breakdown"])
        self.assertIn("time", entry)

        # a later pass the same day appends instead of replacing
        self._run_watch(store, "TESTB", push)
        data = json.loads(store.poller_get(scanner.SCAN_WATCH_KEY))
        self.assertEqual([e["symbol"] for e in data["entries"]],
                         ["TESTA", "TESTB"])

    def test_watch_entries_reset_on_new_day(self):
        import datetime as dt, json
        store, push = self._store(), MagicMock(return_value=True)
        yesterday = (scanner._et_now()
                     - dt.timedelta(days=1)).date().isoformat()
        store.poller_set(scanner.SCAN_WATCH_KEY, json.dumps(
            {"kind": "watch", "date": yesterday,
             "entries": [{"symbol": "OLDCO", "side": "BUY"}]}))
        self._run_watch(store, "TESTB", push)
        data = json.loads(store.poller_get(scanner.SCAN_WATCH_KEY))
        self.assertEqual(data["date"], scanner._et_today_str())
        self.assertEqual([e["symbol"] for e in data["entries"]], ["TESTB"])

    def test_watch_dry_run_does_not_persist(self):
        store = self._store()
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._bars_for("TESTA")
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             _patch_sentiment():
            out = scanner.watch_scan(store=store, rest=rest,
                                     dry_run=True, push_fn=MagicMock())
        self.assertEqual(len(out), 1)
        self.assertIsNone(store.poller_get(scanner.SCAN_WATCH_KEY))

    def test_watch_persist_failure_does_not_break_push(self):
        store = self._store()
        real_set = store.poller_set

        def flaky(key, value):
            if key == scanner.SCAN_WATCH_KEY:
                raise RuntimeError("db down")
            return real_set(key, value)

        store.poller_set = flaky
        push = MagicMock(return_value=True)
        out = self._run_watch(store, "TESTA", push)
        self.assertEqual(len(out), 1)
        push.assert_called_once()


class TestRating(unittest.TestCase):
    def test_converts_score_to_out_of_ten(self):
        self.assertEqual(scanner._rating(68), "6.8/10")
        self.assertEqual(scanner._rating(45), "4.5/10")
        self.assertEqual(scanner._rating(100), "10.0/10")
        self.assertEqual(scanner._rating(0), "0.0/10")
        self.assertEqual(scanner._rating(5), "0.5/10")

    def test_one_decimal_format(self):
        self.assertRegex(scanner._rating(67), r"^\d\.\d/10$")
        self.assertEqual(scanner._rating(67), "6.7/10")

    def test_clamps_out_of_range(self):
        # fundamental bonus can push composite above 100; display caps at 10
        self.assertEqual(scanner._rating(125), "10.0/10")
        self.assertEqual(scanner._rating(-10), "0.0/10")

    def test_push_bars_pinned(self):
        # technical-score bars, identical for morning and watch
        self.assertEqual(scanner.BUY_BARS, {"dip": 60, "momentum": 75})
        self.assertEqual(scanner.SELL_BAR, 40)
        for fn in (scanner._morning_bar, scanner._watch_bar):
            self.assertEqual(fn("BUY", None, "dip"), 60)
            self.assertEqual(fn("BUY", None, "momentum"), 75)
            self.assertEqual(fn("SELL", None), 40)


class TestAnalystSentiment(unittest.TestCase):
    def _store(self):
        return Store(":memory:")

    def _mock_resp(self, payload):
        m = MagicMock()
        m.json.return_value = payload
        return m

    # --- score math ------------------------------------------------------
    def test_score_math_aapl_like(self):
        p = {"symbol": "AAPL", "period": "2026-10-01", "strongBuy": 11,
             "buy": 20, "hold": 14, "sell": 3, "strongSell": 1}
        score, counts = sentiment.analyst_score_from_period(p)
        self.assertAlmostEqual(score, 37 / 49, places=3)
        self.assertEqual(counts["total"], 49)
        self.assertEqual(counts["period"], "2026-10-01")

    def test_all_strong_sell_is_minus_one(self):
        score, _ = sentiment.analyst_score_from_period({"strongSell": 5})
        self.assertEqual(score, -1.0)

    def test_all_strong_buy_is_plus_one(self):
        score, _ = sentiment.analyst_score_from_period({"strongBuy": 4})
        self.assertEqual(score, 1.0)

    def test_empty_period_is_neutral(self):
        score, counts = sentiment.analyst_score_from_period({})
        self.assertEqual(score, 0.0)
        self.assertEqual(counts["total"], 0)

    def test_missing_keys_tolerated(self):
        score, counts = sentiment.analyst_score_from_period({"buy": 3})
        self.assertAlmostEqual(score, 1.0)
        self.assertEqual(counts["total"], 3)

    # --- combine ----------------------------------------------------------
    def test_combine_weighted(self):
        # 0.65 news / 0.35 analyst
        self.assertAlmostEqual(sentiment.combine(0.6, 0.2), 0.46)
        self.assertAlmostEqual(sentiment.combine(-1.0, 1.0), -0.3)

    def test_combine_clamps(self):
        self.assertEqual(sentiment.combine(1.0, 1.0), 1.0)
        self.assertEqual(sentiment.combine(-1.0, -1.0), -1.0)

    # --- formatting --------------------------------------------------------
    def test_format_combined(self):
        p = {"news": 0.64,
             "analyst_counts": {"strongBuy": 11, "buy": 20, "hold": 14,
                                "sell": 3, "strongSell": 1, "total": 49,
                                "period": "2026-10-01"}}
        self.assertEqual(
            sentiment.format_combined(p),
            "news: +0.6 | analysts: 31 buy / 14 hold / 4 sell")

    def test_format_combined_negative(self):
        p = {"news": -0.83,
             "analyst_counts": {"strongBuy": 0, "buy": 2, "hold": 5,
                                "sell": 6, "strongSell": 2, "total": 15,
                                "period": "2026-09-01"}}
        self.assertEqual(
            sentiment.format_combined(p),
            "news: -0.8 | analysts: 2 buy / 5 hold / 8 sell")

    def test_has_signal(self):
        self.assertFalse(sentiment.has_signal(_NEUTRAL_SENTIMENT))
        self.assertFalse(sentiment.has_signal({}))
        self.assertFalse(sentiment.has_signal(None))
        self.assertTrue(
            sentiment.has_signal({"n": 3, "analyst_counts": {"total": 0}}))
        self.assertTrue(
            sentiment.has_signal({"n": 0, "analyst_counts": {"total": 12}}))

    # --- get_analyst: fetch, cache, failure ---------------------------------
    def test_get_analyst_caches_24h(self):
        store = self._store()
        payload = [{"symbol": "T", "period": "2026-10-01", "strongBuy": 2,
                    "buy": 3, "hold": 4, "sell": 1, "strongSell": 0}]
        with patch.object(sentiment.requests, "get",
                          return_value=self._mock_resp(payload)) as rg:
            a1 = sentiment.get_analyst(store, "T")
            a2 = sentiment.get_analyst(store, "T")
            self.assertEqual(rg.call_count, 1)  # second hit from cache
        self.assertAlmostEqual(a1["score"], (4 + 3 - 1) / 10, places=3)
        self.assertEqual(a2["score"], a1["score"])
        self.assertEqual(a1["counts"]["period"], "2026-10-01")

    def test_get_analyst_empty_is_neutral(self):
        store = self._store()
        with patch.object(sentiment.requests, "get",
                          return_value=self._mock_resp([])):
            a = sentiment.get_analyst(store, "ZZZ")
        self.assertEqual(a["score"], 0.0)
        self.assertEqual(a["counts"]["total"], 0)

    def test_get_analyst_failure_is_neutral(self):
        store = self._store()
        with patch.object(sentiment.requests, "get",
                          side_effect=Exception("boom")):
            a = sentiment.get_analyst(store, "T")
        self.assertEqual(a["score"], 0.0)

    def test_get_analyst_latest_period_wins(self):
        store = self._store()
        payload = [
            {"symbol": "T", "period": "2026-09-01", "strongBuy": 0, "buy": 0,
             "hold": 1, "sell": 9, "strongSell": 0},
            {"symbol": "T", "period": "2026-10-01", "strongBuy": 5, "buy": 5,
             "hold": 0, "sell": 0, "strongSell": 0},
        ]
        with patch.object(sentiment.requests, "get",
                          return_value=self._mock_resp(payload)):
            a = sentiment.get_analyst(store, "T")
        self.assertEqual(a["score"], 1.0)
        self.assertEqual(a["counts"]["period"], "2026-10-01")

    # --- combined get_sentiment ----------------------------------------------
    def test_get_sentiment_combines_both_legs(self):
        store = self._store()
        news_payload = {"sentiment": 0.6, "n": 5, "label": "positive",
                        "fetched_at": "2026-01-01T00:00:00+00:00"}
        analyst_payload = {"score": 0.2,
                           "counts": {"strongBuy": 1, "buy": 2, "hold": 3,
                                      "sell": 1, "strongSell": 0, "total": 7,
                                      "period": "2026-10-01"},
                           "fetched_at": "2026-01-01T00:00:00+00:00"}
        marketaux_payload = {"score": None, "n": 0,
                             "fetched_at": "2026-01-01T00:00:00+00:00"}
        with patch.object(sentiment, "get_news",
                          return_value=news_payload), \
             patch.object(sentiment, "get_analyst",
                         return_value=analyst_payload), \
             patch.object(sentiment, "get_marketaux",
                         return_value=marketaux_payload):
            s = sentiment.get_sentiment(store, "T")
        self.assertAlmostEqual(s["sentiment"], 0.46)  # 0.65*0.6 + 0.35*0.2
        self.assertEqual(s["news"], 0.6)
        self.assertEqual(s["analyst"], 0.2)
        self.assertEqual(s["n"], 5)
        self.assertEqual(s["analyst_counts"]["total"], 7)

    # --- scanner integration shows both components ------------------------------
    def test_morning_line_payload_has_both_components(self):
        closes = BUY_CLOSES
        bars = {"TESTA": make_bars(closes, last_vol=5_000_000.0)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        full = {"sentiment": 0.4, "news": 0.6, "analyst": 0.2, "n": 4,
                "analyst_counts": {"strongBuy": 11, "buy": 20, "hold": 14,
                                   "sell": 3, "strongSell": 1, "total": 49,
                                   "period": "2026-10-01"},
                "label": "positive", "fetched_at": "2026-01-01T00:00:00+00:00"}
        with patch.object(sessions_mod, "is_trading_day",
                          return_value=True), \
             patch.object(scanner, "get_universe",
                          return_value=["TESTA"]), \
             patch.object(scanner, "fundamental_bonus",
                          return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             patch("scanner.sentiment.get_sentiment", return_value=full):
            out = scanner.run_scan(store=self._store(), rest=rest,
                                   dry_run=True, push_fn=MagicMock())
        self.assertEqual(len(out["buys"]), 1)
        c = out["buys"][0]
        self.assertTrue(sentiment.has_signal(c["sentiment_payload"]))
        self.assertEqual(
            sentiment.format_combined(c["sentiment_payload"]),
            "news: +0.6 | analysts: 31 buy / 14 hold / 4 sell")
        # rating shifted by total*1.5 = +0.6 on the /10 scale
        base = c["composite"] / 10.0
        adj = sentiment.adjusted_rating_str(c["composite"], 0.4)
        self.assertEqual(adj, f"{min(10.0, base + 0.6):.1f}/10")


class TestEarningsFlag(unittest.TestCase):
    """Earnings proximity flag: Finnhub /calendar/earnings, 24h-cached,
    fail-open, appended to reasons everywhere they are rendered."""

    def _store(self):
        return Store(":memory:")

    @staticmethod
    def _cal_resp(dates):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "earningsCalendar": [{"date": d, "symbol": "TESTA"}
                                 for d in dates]}
        return resp

    @staticmethod
    def _expected(d):
        return f"⚠ earnings {d.strftime('%b')} {d.day}"

    def test_note_within_window(self):
        import datetime as dt
        store = self._store()
        today = scanner._et_now().date()
        d = today + dt.timedelta(days=1)
        with patch.object(scanner.requests, "get",
                          return_value=self._cal_resp([d.isoformat()])):
            note = scanner.earnings_note(store, "TESTA", key="k",
                                         today=today)
        self.assertEqual(note, self._expected(d))

    def test_note_today_and_day2_flag_day3_does_not(self):
        # superseded by trading-day counting (see below); calendar deltas
        # 0 and 2 still flag on ordinary weekdays
        import datetime as dt
        today = scanner._et_now().date()
        for delta, flagged in ((0, True), (2, True)):
            store = self._store()
            d = today + dt.timedelta(days=delta)
            with patch.object(scanner.requests, "get",
                              return_value=self._cal_resp([d.isoformat()])):
                note = scanner.earnings_note(store, "TESTA", key="k",
                                             today=today)
            if flagged:
                self.assertEqual(note, self._expected(d))
            else:
                self.assertIsNone(note)

    def test_note_counts_trading_days_friday_flags_monday(self):
        # 2026-10-16 is a Friday. The window is 3 TRADING days: Monday
        # (1) flags — the old 2-calendar-day rule missed it — Wednesday
        # (3) still flags, Thursday (4) does not.
        import datetime as dt
        fri = dt.date(2026, 10, 16)
        self.assertEqual(fri.weekday(), 4)  # really a Friday
        cases = ((dt.date(2026, 10, 16), True),    # today itself
                 (dt.date(2026, 10, 19), True),    # Monday: 1 trading day
                 (dt.date(2026, 10, 21), True),    # Wednesday: 3
                 (dt.date(2026, 10, 22), False))   # Thursday: 4
        for d, flagged in cases:
            store = self._store()
            with patch.object(scanner.requests, "get",
                              return_value=self._cal_resp([d.isoformat()])):
                note = scanner.earnings_note(store, "TESTA", key="k",
                                             today=fri)
            if flagged:
                self.assertEqual(note, self._expected(d), f"date {d}")
            else:
                self.assertIsNone(note, f"date {d}")

    def test_note_holiday_extends_the_trading_day_window(self):
        # With Monday 2026-10-19 a holiday (seeded calendar), Thursday
        # 2026-10-22 is only 3 trading days after Friday — flagged.
        import datetime as dt
        store = self._store()
        days = []
        d = dt.date(2026, 10, 16)
        while d <= dt.date(2026, 10, 23):
            days.append({"date": d.isoformat(),
                         "is_open": d != dt.date(2026, 10, 19)
                         and d.weekday() < 5,
                         "open": "09:30", "close": "16:00"})
            d += dt.timedelta(days=1)
        store.calendar_save(days)
        thu = dt.date(2026, 10, 22)
        with patch.object(scanner.requests, "get",
                          return_value=self._cal_resp([thu.isoformat()])):
            note = scanner.earnings_note(store, "TESTA", key="k",
                                         today=dt.date(2026, 10, 16))
        self.assertEqual(note, self._expected(thu))

    def test_trading_days_until_helper(self):
        import datetime as dt
        store = self._store()  # empty calendar -> weekday fallback
        fri = dt.date(2026, 10, 16)
        self.assertEqual(scanner._trading_days_until(
            store, fri, fri), 0)
        self.assertEqual(scanner._trading_days_until(
            store, fri, dt.date(2026, 10, 19)), 1)   # Monday
        self.assertEqual(scanner._trading_days_until(
            store, fri, dt.date(2026, 10, 21)), 3)   # Wednesday
        self.assertEqual(scanner._trading_days_until(
            store, fri, dt.date(2026, 10, 15)), -1)  # past

    def test_no_earnings_scheduled_no_note(self):
        import datetime as dt
        store = self._store()
        with patch.object(scanner.requests, "get",
                          return_value=self._cal_resp([])):
            note = scanner.earnings_note(
                store, "TESTA", key="k", today=scanner._et_now().date())
        self.assertIsNone(note)

    def test_api_failure_fails_open(self):
        import datetime as dt
        store = self._store()
        with patch.object(scanner.requests, "get",
                          side_effect=ConnectionError("blocked")):
            note = scanner.earnings_note(
                store, "TESTA", key="k", today=scanner._et_now().date())
            d = scanner.get_earnings_date(store, "TESTB", key="k")
        self.assertIsNone(note)          # no flag, and no exception raised
        self.assertIsNone(d)

    def test_missing_key_no_fetch_no_note(self):
        store = self._store()
        with patch.object(scanner.requests, "get") as mock_get:
            note = scanner.earnings_note(store, "TESTA", key="")
        self.assertIsNone(note)
        mock_get.assert_not_called()

    def test_cached_24h_single_fetch(self):
        import datetime as dt
        store = self._store()
        today = scanner._et_now().date()
        d = (today + dt.timedelta(days=5)).isoformat()
        with patch.object(scanner.requests, "get",
                          return_value=self._cal_resp([d])) as mock_get:
            first = scanner.get_earnings_date(store, "TESTA", key="k")
            second = scanner.get_earnings_date(store, "TESTA", key="k")
        self.assertEqual(first, d)
        self.assertEqual(second, d)
        self.assertEqual(mock_get.call_count, 1)

    def test_fetch_window_params(self):
        # one Finnhub call: symbol + from=today + a bounded lookahead
        store = self._store()
        with patch.object(scanner.requests, "get",
                          return_value=self._cal_resp([])) as mock_get:
            scanner.get_earnings_date(store, "TESTA", key="sekret")
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["symbol"], "TESTA")
        self.assertEqual(params["token"], "sekret")
        self.assertIn("from", params)
        self.assertIn("to", params)

    def _run_morning_with_calendar(self, store, earnings_date):
        closes = BUY_CLOSES
        bars = {"TESTA": make_bars(closes, last_vol=5_000_000.0)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        push = MagicMock(return_value=True)
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "fundamental_bonus", return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             patch.object(scanner, "_current_regime", return_value=None), \
             patch.object(scanner.requests, "get",
                          return_value=self._cal_resp([earnings_date])), \
             _patch_sentiment():
            out = scanner.run_scan(store=store, rest=rest,
                                   dry_run=False, push_fn=push)
        return out, push

    def test_flag_in_morning_push_and_persisted_entry(self):
        import datetime as dt, json
        store = self._store()
        d = scanner._et_now().date() + dt.timedelta(days=1)
        out, push = self._run_morning_with_calendar(store, d.isoformat())
        flag = self._expected(d)
        self.assertEqual(len(out["buys"]), 1)
        self.assertEqual(out["buys"][0].get("earnings_note"), flag)
        _title, body = push.call_args[0]
        self.assertIn(flag, body)                     # ntfy push line
        data = json.loads(store.poller_get(scanner.SCAN_MORNING_KEY))
        entry = data["buys"][0]
        self.assertIn(flag, entry["reasons"])         # dashboard entry
        self.assertIn(flag, entry["breakdown"])

    def test_no_flag_in_morning_output_when_far(self):
        import datetime as dt
        store = self._store()
        d = scanner._et_now().date() + dt.timedelta(days=10)
        _out, push = self._run_morning_with_calendar(store, d.isoformat())
        _title, body = push.call_args[0]
        self.assertNotIn("⚠ earnings", body)

    def test_flag_in_watch_push_and_persisted_entry(self):
        import datetime as dt, json
        store = self._store()
        d = scanner._et_now().date() + dt.timedelta(days=2)
        flag = self._expected(d)
        closes = BUY_CLOSES
        bars = {"TESTA": make_bars(closes, last_vol=5_000_000.0)}
        rest = MagicMock()
        rest.daily_bars_multi.return_value = bars
        push = MagicMock(return_value=True)
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "_current_regime", return_value=None), \
             patch.object(scanner.requests, "get",
                          return_value=self._cal_resp([d.isoformat()])), \
             _patch_sentiment():
            out = scanner.watch_scan(store=store, rest=rest,
                                     dry_run=False, push_fn=push)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].get("earnings_note"), flag)
        _title, body = push.call_args[0]
        self.assertIn(flag, body)                     # watch push line
        data = json.loads(store.poller_get(scanner.SCAN_WATCH_KEY))
        entry = data["entries"][0]
        self.assertIn(flag, entry["reasons"])         # dashboard watch entry
        self.assertIn(flag, entry["breakdown"])



class TestPushPriceFloor(unittest.TestCase):
    """Fix 4: pushes (morning + watch) require price >= PUSH_MIN_PRICE;
    the persisted dashboard lists keep every setup at its true score."""

    def test_bar_and_floor_constants(self):
        self.assertEqual(scanner.PUSH_MIN_PRICE, 3.0)
        self.assertEqual(scanner.BUY_BARS["dip"], 60)
        self.assertEqual(scanner.SELL_BAR, 40)

    class _Rest:
        def daily_bars_multi(self, symbols, days=300):
            if "SPY" in symbols:
                return {}
            return {"PENNY": make_bars([2.5] * 250)}

    @staticmethod
    def _screen(bars, volume_scale=1.0, **kw):
        return {"price": 2.50, "tech_score": 70, "playbook": "dip",
                "setups": [("fake", 70, "fake setup")],
                "bear_score": 0, "bear_setups": [], "indicators": {}}

    def test_morning_sub_floor_candidate_not_pushed_but_persisted(self):
        import json
        store = Store(":memory:")
        push = MagicMock(return_value=True)
        with patch.object(sessions_mod, "is_trading_day",
                          return_value=True), \
                patch.object(scanner, "get_universe",
                             return_value=["PENNY"]), \
                patch.object(scanner, "technical_screen",
                             side_effect=self._screen), \
                patch.object(scanner, "passes_liquidity",
                             return_value=True), \
                patch.object(scanner, "fundamental_bonus",
                             return_value=(5, ["funds ok"])), \
                patch.object(scanner, "short_bonus", return_value=(0, [])), \
                patch.object(scanner.time, "sleep", lambda *_: None), \
                _patch_sentiment():
            out = scanner.run_scan(store=store, rest=self._Rest(),
                                   dry_run=False, push_fn=push)
        # tech 70 clears the dip bar 60 but $2.50 < $3.00 floor
        self.assertEqual(out["buys"], [])
        push.assert_not_called()
        rows = json.loads(store.poller_get(scanner.SCAN_MORNING_KEY))
        self.assertEqual(len(rows["buys"]), 1)          # still listed
        self.assertEqual(rows["buys"][0]["symbol"], "PENNY")
        self.assertEqual(rows["buys"][0]["score"], 75)  # composite 70 + 5
        self.assertFalse(rows["buys"][0]["top_pick"])   # not a push

    def test_watch_sub_floor_match_not_pushed_not_seen(self):
        import json
        store = Store(":memory:")
        push = MagicMock(return_value=True)
        with patch.object(scanner, "_in_market_hours", return_value=True), \
                patch.object(scanner, "get_universe",
                             return_value=["PENNY"]), \
                patch.object(scanner, "technical_screen",
                             side_effect=self._screen), \
                patch.object(scanner, "passes_liquidity",
                             return_value=True), \
                _patch_sentiment():
            out = scanner.watch_scan(store=store, rest=self._Rest(),
                                     dry_run=False, push_fn=push)
        self.assertEqual(out, [])
        push.assert_not_called()
        raw = store.poller_get(scanner.WATCH_SEEN_KEY)
        seen = json.loads(raw) if raw else {"symbols": []}
        self.assertEqual(seen["symbols"], [])  # not marked seen either


class TestPushBarsEndToEnd(unittest.TestCase):
    """A lone uptrend (momentum 15) must never push; a full dip pushes;
    the bull regime raises the dip bar to 75."""

    _UPTREND_ONLY = {"price": 50.0, "tech_score": 15, "playbook": "momentum",
                     "setups": [("uptrend", 15, "uptrend")],
                     "bear_score": 0, "bear_setups": [], "indicators": {}}
    _DIP_65 = {"price": 50.0, "tech_score": 65, "playbook": "dip",
               "setups": [("oversold_rsi30", 40, "RSI 27"),
                          ("capitulation_volume", 25, "volume")],
               "bear_score": 0, "bear_setups": [], "indicators": {}}

    class _Rest:
        def daily_bars_multi(self, symbols, days=300):
            if "SPY" in symbols:
                return {}
            return {"TESTA": make_bars([100.0] * 250)}

    def _watch(self, screen_result, regime=None):
        store = Store(":memory:")
        push = MagicMock(return_value=True)
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "_current_regime", return_value=regime), \
             patch.object(scanner, "technical_screen",
                          return_value=dict(screen_result)), \
             patch.object(scanner, "passes_liquidity", return_value=True), \
             _patch_sentiment():
            out = scanner.watch_scan(store=store, rest=self._Rest(),
                                     dry_run=False, push_fn=push)
        return out, push

    def test_lone_uptrend_no_watch_match(self):
        out, push = self._watch(self._UPTREND_ONLY)
        self.assertEqual(out, [])
        push.assert_not_called()

    def test_dip_clears_base_bar(self):
        out, push = self._watch(self._DIP_65)
        self.assertEqual([r["symbol"] for r in out], ["TESTA"])
        self.assertIn("dip buy", push.call_args[0][1])
        push.assert_called_once()

    def test_bull_regime_raises_dip_bar(self):
        with self.assertLogs("stockbot.scanner", level="INFO") as cm:
            out, push = self._watch(self._DIP_65, regime="bull")
        self.assertEqual(out, [])          # 65 < 75
        push.assert_not_called()
        self.assertTrue(any("buy bars dip 75 / momentum 75" in m
                            for m in cm.output), cm.output)

    def test_lone_uptrend_plus_bonus_no_morning_push(self):
        # the push decision is technical-only: fundamentals rank and rate
        store = Store(":memory:")
        push = MagicMock(return_value=True)
        with patch.object(sessions_mod, "is_trading_day",
                          return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "_current_regime", return_value=None), \
             patch.object(scanner, "technical_screen",
                          return_value=dict(self._UPTREND_ONLY)), \
             patch.object(scanner, "passes_liquidity", return_value=True), \
             patch.object(scanner, "fundamental_bonus",
                          return_value=(25, ["funds great"])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             patch.object(scanner.time, "sleep", lambda *_: None), \
             _patch_sentiment():
            out = scanner.run_scan(store=store, rest=self._Rest(),
                                   dry_run=False, push_fn=push)
        self.assertEqual(out["buys"], [])
        push.assert_not_called()

class TestWatchVolumeScaling(unittest.TestCase):
    """Fix 2: watch mode scales the volume-spike threshold by the elapsed
    session fraction; the morning scan keeps the full-day threshold."""

    def test_session_fraction(self):
        import datetime as dt

        def at(h, m):
            return dt.datetime(2026, 10, 20, h, m, tzinfo=scanner.ET)
        self.assertAlmostEqual(scanner._session_fraction(at(10, 0)),
                               30 / 390)          # 0.0769...
        self.assertEqual(scanner._session_fraction(at(9, 30)), 0.05)  # clamp
        self.assertEqual(scanner._session_fraction(at(9, 0)), 0.05)   # pre-open
        self.assertEqual(scanner._session_fraction(at(16, 0)), 1.0)  # close

    def test_partial_bar_spikes_only_at_scaled_threshold(self):
        import datetime as dt
        # last-day volume 1.5x the 20-day median, green day: NOT a spike
        # at full-day scale (needs 2x), a spike at 10:00 scale
        # (2 x median x 0.0769 = 0.15x median).
        closes = [100.0 + i * 0.5 for i in range(250)]
        bars = make_bars(closes, vol=100_000.0, last_vol=150_000.0)
        full = scanner.technical_screen(bars)
        self.assertNotIn("volume_up_day",
                         [n for n, _p, _r in full["setups"]])
        scale = scanner._session_fraction(
            dt.datetime(2026, 10, 20, 10, 0, tzinfo=scanner.ET))
        partial = scanner.technical_screen(bars, volume_scale=scale)
        self.assertIn("volume_up_day",
                      [n for n, _p, _r in partial["setups"]])
        # and the indicator stays the honest raw ratio either way
        self.assertAlmostEqual(partial["indicators"]["vol_ratio"], 1.5)

    def test_watch_passes_session_fraction_into_screen(self):
        store = Store(":memory:")
        seen_scales = []

        class Rest:
            def daily_bars_multi(self, symbols, days=300):
                if "SPY" in symbols:
                    return {}
                return {"TESTA": make_bars([100.0] * 250)}

        def screen(bars, volume_scale=1.0, **kw):
            seen_scales.append(volume_scale)
            return {"price": 50.0, "tech_score": 0, "setups": [],
                    "bear_score": 0, "bear_setups": [], "indicators": {}}

        with patch.object(scanner, "_in_market_hours", return_value=True), \
                patch.object(scanner, "get_universe",
                             return_value=["TESTA"]), \
                patch.object(scanner, "_session_fraction",
                             return_value=0.25), \
                patch.object(scanner, "technical_screen",
                             side_effect=screen), \
                patch.object(scanner, "passes_liquidity",
                             return_value=True), \
                _patch_sentiment():
            scanner.watch_scan(store=store, rest=Rest(), dry_run=False,
                               push_fn=MagicMock(return_value=True))
        self.assertEqual(seen_scales, [0.25])


class TestWatchSeenPerSide(unittest.TestCase):
    """Fix 6b: watch dedupe is per symbol+side — a BUY alert does not
    mute a later SELL flip the same day; legacy plain entries mute both."""

    class _Rest:
        def daily_bars_multi(self, symbols, days=300):
            if "SPY" in symbols:
                return {}
            return {"TESTA": make_bars([100.0] * 250)}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.mode = "bull"

    def _screen(self, bars, volume_scale=1.0, **kw):
        if self.mode == "bull":
            return {"price": 160.0, "tech_score": 65, "playbook": "dip",
                    "setups": [("fake", 65, "fake")],
                    "bear_score": 0, "bear_setups": [], "indicators": {}}
        return {"price": 60.0, "tech_score": 0, "setups": [],
                "bear_score": 55,
                "bear_setups": [("fake", 55, "fake")], "indicators": {}}

    def _watch_once(self, store, push):
        with patch.object(scanner, "_in_market_hours", return_value=True), \
                patch.object(scanner, "get_universe",
                             return_value=["TESTA"]), \
                patch.object(scanner, "technical_screen",
                             side_effect=self._screen), \
                patch.object(scanner, "passes_liquidity",
                             return_value=True), \
                _patch_sentiment():
            return scanner.watch_scan(store=store, rest=self._Rest(),
                                      dry_run=False, push_fn=push)

    def test_buy_then_sell_flip_realerts(self):
        import json
        store = Store(":memory:")
        push = MagicMock(return_value=True)
        out1 = self._watch_once(store, push)
        self.assertEqual(len(out1), 1)
        self.assertEqual(out1[0]["side"], "BUY")
        # same side again: deduped
        self.assertEqual(self._watch_once(store, push), [])
        # flip to SELL: re-alerts the same day
        self.mode = "bear"
        out3 = self._watch_once(store, push)
        self.assertEqual(len(out3), 1)
        self.assertEqual(out3[0]["side"], "SELL")
        seen = json.loads(store.poller_get(scanner.WATCH_SEEN_KEY))
        self.assertIn("TESTA|BUY", seen["symbols"])
        self.assertIn("TESTA|SELL", seen["symbols"])
        # and the sell side is now deduped too
        self.assertEqual(self._watch_once(store, push), [])
        self.assertEqual(push.call_count, 2)

    def test_legacy_plain_entry_mutes_both_sides(self):
        import json
        store = Store(":memory:")
        store.poller_set(scanner.WATCH_SEEN_KEY, json.dumps(
            {"date": scanner._et_today_str(), "symbols": ["TESTA"]}))
        push = MagicMock(return_value=True)
        self.assertEqual(self._watch_once(store, push), [])
        self.mode = "bear"
        self.assertEqual(self._watch_once(store, push), [])
        push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
