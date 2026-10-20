"""Tests for sentiment.py: lexicon scoring, aggregation, rating math, cache,
analyst recommendations, and scanner integration."""
import unittest
from unittest.mock import MagicMock, patch

import sentiment
from storage import Store


def _store():
    return Store(":memory:")


def _headlines(*texts, age_days=0.0):
    return [{"text": t, "age_days": age_days} for t in texts]


class TestLexiconScoring(unittest.TestCase):
    def test_clearly_positive(self):
        self.assertEqual(
            sentiment.score_headline(
                "Apple beats earnings estimates, raises guidance"), 1.0)
        self.assertEqual(
            sentiment.score_headline("FDA approves new drug, shares surge"), 1.0)
        self.assertEqual(
            sentiment.score_headline("Announces $5B buyback program"), 1.0)
        self.assertEqual(
            sentiment.score_headline(
                "Upgraded to overweight, price target raised"), 1.0)
        self.assertEqual(
            sentiment.score_headline(
                "Record quarterly revenue, raises dividend"), 1.0)
        self.assertEqual(
            sentiment.score_headline("Partners with Microsoft on AI deal"), 1.0)
        self.assertEqual(
            sentiment.score_headline("Wins Pentagon contract worth $200M"),
            1.0)
        self.assertEqual(
            sentiment.score_headline("Positive trial data meets primary endpoint"),
            1.0)

    def test_clearly_negative(self):
        self.assertEqual(
            sentiment.score_headline(
                "Company misses revenue estimates, cuts guidance"), -1.0)
        self.assertEqual(
            sentiment.score_headline("Files for chapter 11 bankruptcy"), -1.0)
        self.assertEqual(
            sentiment.score_headline("SEC probe into accounting practices"),
            -1.0)
        self.assertEqual(
            sentiment.score_headline("Downgraded to sell at Goldman Sachs"),
            -1.0)
        self.assertEqual(
            sentiment.score_headline("Announces layoffs, 10% workforce reduction"),
            -1.0)
        self.assertEqual(
            sentiment.score_headline("Restates earnings amid fraud probe"), -1.0)
        self.assertEqual(
            sentiment.score_headline("Dividend cut to preserve cash"), -1.0)
        self.assertEqual(
            sentiment.score_headline("Shares plunge after profit warning"), -1.0)

    def test_neutral_no_keywords(self):
        self.assertEqual(
            sentiment.score_headline("Apple to host product event next Tuesday"),
            0.0)
        self.assertEqual(
            sentiment.score_headline("CEO to speak at industry conference"), 0.0)
        self.assertEqual(sentiment.score_headline(""), 0.0)

    def test_mixed_headline_nets_out(self):
        # one positive ("beats"), one negative ("cuts guidance") -> 0
        self.assertEqual(
            sentiment.score_headline("Beats earnings but cuts guidance"), 0.0)

    def test_negation_flips_positive(self):
        # "did not beat estimates": the positive hit is negated -> negative
        s = sentiment.score_headline("Company did not beat estimates")
        self.assertLess(s, 0.0)

    def test_negation_flips_negative(self):
        # "not downgraded": the negative hit is negated -> positive
        s = sentiment.score_headline("Stock not downgraded, firm reiterates buy")
        self.assertGreater(s, 0.0)

    def test_negation_needs_proximity(self):
        # "not" far from the keyword does not flip it
        s = sentiment.score_headline(
            "Not a cloud in the sky today as company beats estimates soundly")
        self.assertGreater(s, 0.0)

    def test_multiple_hits_same_polarity(self):
        s = sentiment.score_headline(
            "Beats estimates, raises guidance, announces buyback")
        self.assertEqual(s, 1.0)  # normalized, still capped at 1

    def test_partial_match_not_a_word(self):
        # "upgrades" is a word; "upgradation" should not match "upgrade"
        self.assertEqual(
            sentiment.score_headline("System upgradation completed"), 0.0)


class TestAggregation(unittest.TestCase):
    def test_recency_weighting(self):
        # new positive (age 0, w=1) vs old negative (age 6, w=1/7)
        s, n = sentiment.aggregate([
            {"text": "Beats earnings estimates", "age_days": 0.0},
            {"text": "Misses revenue estimates", "age_days": 6.0},
        ])
        self.assertEqual(n, 2)
        self.assertAlmostEqual(s, (1 - 1 / 7) / (1 + 1 / 7), places=4)
        self.assertGreater(s, 0.0)  # newer headline dominates

    def test_equal_age_is_mean(self):
        s, n = sentiment.aggregate(_headlines(
            "Beats earnings estimates", "Misses revenue estimates"))
        self.assertEqual(n, 2)
        self.assertAlmostEqual(s, 0.0)

    def test_empty_is_neutral(self):
        s, n = sentiment.aggregate([])
        self.assertEqual((s, n), (0.0, 0))


class TestRatingAdjustment(unittest.TestCase):
    def test_positive_adds(self):
        adj, contrib = sentiment.apply_to_rating(6.8, 0.533)
        self.assertAlmostEqual(contrib, 0.8, places=1)
        self.assertAlmostEqual(adj, 7.6, places=1)

    def test_negative_subtracts(self):
        adj, contrib = sentiment.apply_to_rating(6.8, -0.4)
        self.assertAlmostEqual(contrib, -0.6, places=1)
        self.assertAlmostEqual(adj, 6.2, places=1)

    def test_clamps_at_ten(self):
        adj, _ = sentiment.apply_to_rating(9.5, 1.0)
        self.assertEqual(adj, 10.0)

    def test_clamps_at_zero(self):
        adj, _ = sentiment.apply_to_rating(0.5, -1.0)
        self.assertEqual(adj, 0.0)

    def test_neutral_unchanged(self):
        adj, contrib = sentiment.apply_to_rating(6.8, 0.0)
        self.assertEqual((adj, contrib), (6.8, 0.0))

    def test_display_string(self):
        self.assertEqual(sentiment.adjusted_rating_str(68, 0.533), "7.6/10")
        self.assertEqual(sentiment.adjusted_rating_str(95, 1.0), "10.0/10")

    def test_format_contribution(self):
        self.assertEqual(sentiment.format_contribution(0.533),
                         "news: positive (+0.8)")
        self.assertEqual(sentiment.format_contribution(-0.4),
                         "news: negative (-0.6)")
        self.assertEqual(sentiment.format_contribution(0.0),
                         "news: neutral (+0.0)")

    def test_labels(self):
        self.assertEqual(sentiment.label_for(0.5), "positive")
        self.assertEqual(sentiment.label_for(-0.5), "negative")
        self.assertEqual(sentiment.label_for(0.1), "neutral")
        self.assertEqual(sentiment.label_for(0.0), "neutral")


class TestNewsCache(unittest.TestCase):
    def _fake_news(self):
        return [{"text": "Beats earnings estimates, raises guidance",
                 "age_days": 0.0, "headline": "h", "url": "u"}]

    def test_cache_hit_avoids_api_call(self):
        store = _store()
        with patch.object(sentiment, "fetch_headlines",
                          return_value=self._fake_news()) as fh:
            first = sentiment.get_news(store, "AAA")
            second = sentiment.get_news(store, "AAA")
        self.assertEqual(fh.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["n"], 1)
        self.assertGreater(first["sentiment"], 0)

    def test_cache_expiry_refetches(self):
        import json
        store = _store()
        old = {"fetched_at": "2020-01-01T00:00:00+00:00",
               "payload": {"sentiment": 0.0, "n": 0, "label": "neutral",
                           "fetched_at": "2020-01-01T00:00:00+00:00"}}
        store.poller_set("sentiment_AAA", json.dumps(old))
        with patch.object(sentiment, "fetch_headlines",
                          return_value=self._fake_news()) as fh:
            out = sentiment.get_news(store, "AAA")
        self.assertEqual(fh.call_count, 1)
        self.assertGreater(out["sentiment"], 0)

    def test_fetch_failure_degrades_to_neutral(self):
        store = _store()
        with patch.object(sentiment, "fetch_headlines",
                          side_effect=RuntimeError("no key")):
            out = sentiment.get_news(store, "BBB")
        self.assertEqual(out["sentiment"], 0.0)
        self.assertEqual(out["n"], 0)
        self.assertEqual(out["label"], "neutral")

    def test_empty_headlines_neutral_but_cached(self):
        store = _store()
        with patch.object(sentiment, "fetch_headlines",
                          return_value=[]) as fh:
            out = sentiment.get_news(store, "CCC")
            out2 = sentiment.get_news(store, "CCC")
        self.assertEqual(fh.call_count, 1)  # empty result still cached
        self.assertEqual(out["sentiment"], 0.0)
        self.assertEqual(out["n"], 0)
        # same payload content (fetched_at may differ by microseconds only
        # if the cache write raced; compare the meaningful fields)
        for k in ("sentiment", "n", "label"):
            self.assertEqual(out2[k], out[k])


class TestAnalystScore(unittest.TestCase):
    def test_bullish_consensus(self):
        score, counts = sentiment.analyst_score_from_period(
            {"strongBuy": 10, "buy": 5, "hold": 2, "sell": 0,
             "strongSell": 0, "period": "2026-10-01"})
        self.assertEqual(score, 1.0)  # (20 + 5) / 17 clamps to 1.0
        self.assertEqual(counts["total"], 17)

    def test_bearish_consensus(self):
        score, _ = sentiment.analyst_score_from_period(
            {"strongBuy": 0, "buy": 1, "hold": 2, "sell": 5,
             "strongSell": 3, "period": "2026-10-01"})
        self.assertAlmostEqual(score, (1 - 5 - 6) / 11, places=4)
        self.assertLess(score, 0)

    def test_empty_period_neutral(self):
        score, counts = sentiment.analyst_score_from_period({})
        self.assertEqual(score, 0.0)
        self.assertEqual(counts["total"], 0)

    def test_clamped(self):
        score, _ = sentiment.analyst_score_from_period(
            {"strongBuy": 100, "buy": 0, "hold": 0, "sell": 0,
             "strongSell": 0, "period": "x"})
        self.assertEqual(score, 1.0)

    def test_get_analyst_caches(self):
        store = _store()
        periods = [{"strongBuy": 8, "buy": 6, "hold": 3, "sell": 1,
                    "strongSell": 0, "period": "2026-10-01"}]
        with patch.object(sentiment, "fetch_recommendations",
                          return_value=periods) as fr:
            a1 = sentiment.get_analyst(store, "AAA")
            a2 = sentiment.get_analyst(store, "AAA")
        self.assertEqual(fr.call_count, 1)
        self.assertEqual(a1, a2)
        self.assertGreater(a1["score"], 0)
        self.assertEqual(a1["counts"]["total"], 18)

    def test_get_analyst_failure_neutral(self):
        store = _store()
        with patch.object(sentiment, "fetch_recommendations",
                          side_effect=RuntimeError("boom")):
            out = sentiment.get_analyst(store, "BBB")
        self.assertEqual(out["score"], 0.0)
        self.assertEqual(out["counts"]["total"], 0)


class TestCombined(unittest.TestCase):
    def test_combine_weights(self):
        # 0.65 news / 0.35 analyst
        self.assertAlmostEqual(sentiment.combine(1.0, -1.0), 0.3)
        self.assertAlmostEqual(sentiment.combine(0.6, 0.2), 0.46)
        self.assertEqual(sentiment.combine(1.0, 1.0), 1.0)
        self.assertEqual(sentiment.combine(-1.0, -1.0), -1.0)

    def test_get_sentiment_merges_legs(self):
        store = _store()
        news = {"sentiment": 0.6, "n": 4, "label": "positive",
                "fetched_at": "2026-01-01T00:00:00+00:00"}
        analyst = {"score": 0.2,
                   "counts": {"strongBuy": 2, "buy": 3, "hold": 4, "sell": 1,
                              "strongSell": 0, "total": 10,
                              "period": "2026-10-01"},
                   "fetched_at": "2026-01-01T00:00:00+00:00"}
        marketaux = {"score": None, "n": 0,
                     "fetched_at": "2026-01-01T00:00:00+00:00"}
        with patch.object(sentiment, "get_news", return_value=news), \
             patch.object(sentiment, "get_analyst", return_value=analyst), \
             patch.object(sentiment, "get_marketaux",
                          return_value=marketaux):
            out = sentiment.get_sentiment(store, "AAA")
        # 0.65*0.6 + 0.35*0.2 = 0.46 (Marketaux stubbed to no coverage)
        self.assertAlmostEqual(out["sentiment"], 0.46)
        self.assertEqual(out["news"], 0.6)
        self.assertEqual(out["analyst"], 0.2)
        self.assertEqual(out["n"], 4)
        self.assertEqual(out["label"], "positive")
        self.assertEqual(out["analyst_counts"]["total"], 10)
        self.assertIsNone(out["marketaux"])
        self.assertEqual(out["marketaux_n"], 0)

    def test_has_signal(self):
        self.assertTrue(sentiment.has_signal({"n": 2, "analyst_counts": {}}))
        self.assertTrue(sentiment.has_signal(
            {"n": 0, "analyst_counts": {"total": 5}}))
        self.assertFalse(sentiment.has_signal(
            {"n": 0, "analyst_counts": {"total": 0}}))
        self.assertFalse(sentiment.has_signal(
            {"sentiment": 0.0, "n": 0, "label": "neutral"}))  # legacy stub
        self.assertFalse(sentiment.has_signal(None))

    def test_format_combined(self):
        payload = {"news": 0.64, "n": 5,
                   "analyst_counts": {"strongBuy": 2, "buy": 12, "hold": 4,
                                      "sell": 1, "strongSell": 0, "total": 19,
                                      "period": "2026-10-01"}}
        self.assertEqual(sentiment.format_combined(payload),
                         "news: +0.6 | analysts: 14 buy / 4 hold / 1 sell")


class TestMarketaux(unittest.TestCase):
    def _article(self, entities, age_days=0.0):
        import datetime as dt
        pub = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=age_days))
        return {"published_at": pub.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "entities": entities}

    def _response(self, articles):
        resp = MagicMock()
        resp.json.return_value = {"data": articles,
                                  "meta": {"count": len(articles)}}
        resp.raise_for_status.return_value = None
        return resp

    def _env(self, token="tok123"):
        import os
        return patch.dict(os.environ, {"MARKETAUX_API_TOKEN": token})

    def test_score_extraction_and_recency_weighting(self):
        store = _store()
        articles = [
            # AAA +1.0 today (w=1), AAA -0.5 three days ago (w=1/4)
            self._article([{"symbol": "AAA", "sentiment_score": 1.0}],
                          age_days=0.0),
            self._article([{"symbol": "AAA", "sentiment_score": -0.5}],
                          age_days=3.0),
            # other symbol only — ignored
            self._article([{"symbol": "ZZZ", "sentiment_score": 1.0}]),
            # outside the 7-day window — ignored
            self._article([{"symbol": "AAA", "sentiment_score": 1.0}],
                          age_days=9.0),
        ]
        with self._env(), \
             patch("sentiment.requests.get",
                   return_value=self._response(articles)) as mg:
            out = sentiment.get_marketaux(store, "AAA")
        # (1*1.0 + 0.25*(-0.5)) / 1.25 = 0.7
        self.assertAlmostEqual(out["score"], 0.7, places=3)
        self.assertEqual(out["n"], 2)
        params = mg.call_args.kwargs["params"]
        self.assertEqual(params["symbols"], "AAA")
        self.assertEqual(params["filter_entities"], "true")
        self.assertEqual(params["language"], "en")
        self.assertEqual(params["limit"], 10)
        self.assertEqual(params["api_token"], "tok123")

    def test_matching_entity_not_first_and_missing_score_skipped(self):
        store = _store()
        articles = [
            # AAA is the second entity; the first one's score must not leak
            self._article([{"symbol": "ZZZ", "sentiment_score": 0.9},
                           {"symbol": "AAA", "sentiment_score": 0.2}]),
            # matching entity without a score — article does not count
            self._article([{"symbol": "AAA"}]),
        ]
        with self._env(), \
             patch("sentiment.requests.get",
                   return_value=self._response(articles)):
            out = sentiment.get_marketaux(store, "AAA")
        self.assertAlmostEqual(out["score"], 0.2, places=3)
        self.assertEqual(out["n"], 1)

    def test_no_matching_entities_none_and_cached(self):
        store = _store()
        articles = [self._article([{"symbol": "ZZZ",
                                    "sentiment_score": 0.9}])]
        with self._env(), \
             patch("sentiment.requests.get",
                   return_value=self._response(articles)) as mg:
            out = sentiment.get_marketaux(store, "AAA")
            out2 = sentiment.get_marketaux(store, "AAA")
        self.assertEqual(mg.call_count, 1)  # no-coverage result is cached
        self.assertIsNone(out["score"])
        self.assertEqual(out["n"], 0)
        self.assertEqual(out2, out)

    def test_cache_hit_makes_one_http_call(self):
        store = _store()
        articles = [self._article([{"symbol": "AAA",
                                    "sentiment_score": 0.5}])]
        with self._env(), \
             patch("sentiment.requests.get",
                   return_value=self._response(articles)) as mg:
            first = sentiment.get_marketaux(store, "AAA")
            second = sentiment.get_marketaux(store, "AAA")
        self.assertEqual(mg.call_count, 1)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first["score"], 0.5, places=3)

    def test_counter_increments_per_fetch(self):
        store = _store()
        articles = [self._article([{"symbol": "AAA",
                                    "sentiment_score": 0.5}]),
                    self._article([{"symbol": "BBB",
                                    "sentiment_score": -0.5}])]
        with self._env(), \
             patch("sentiment.requests.get",
                   return_value=self._response(articles)):
            sentiment.get_marketaux(store, "AAA")
            sentiment.get_marketaux(store, "BBB")
        self.assertEqual(
            store.poller_get(sentiment._marketaux_counter_key()), "2")

    def test_daily_budget_cap_skips_fetch(self):
        store = _store()
        store.poller_set(sentiment._marketaux_counter_key(), "90")
        with self._env(), \
             patch("sentiment.requests.get") as mg:
            out = sentiment.get_marketaux(store, "AAA")
        mg.assert_not_called()
        self.assertIsNone(out["score"])
        self.assertEqual(out["n"], 0)

    def test_http_error_fails_open_and_caches_none(self):
        import requests as rq
        store = _store()
        with self._env(), \
             patch("sentiment.requests.get",
                   side_effect=rq.exceptions.HTTPError("429")) as mg:
            out = sentiment.get_marketaux(store, "AAA")
            out2 = sentiment.get_marketaux(store, "AAA")
        self.assertIsNone(out["score"])
        self.assertEqual(out["n"], 0)
        self.assertIsNone(out2["score"])
        self.assertEqual(mg.call_count, 1)  # failure cached for 24h

    def test_no_token_no_http_call(self):
        store = _store()
        with self._env(token=""), \
             patch("sentiment.requests.get") as mg:
            out = sentiment.get_marketaux(store, "AAA")
        mg.assert_not_called()
        self.assertIsNone(out["score"])
        self.assertEqual(out["n"], 0)

    def test_blend_both_news_legs(self):
        store = _store()
        news = {"sentiment": 0.6, "n": 4, "label": "positive",
                "fetched_at": "2026-01-01T00:00:00+00:00"}
        analyst = {"score": 0.2,
                   "counts": {"strongBuy": 2, "buy": 3, "hold": 4,
                              "sell": 1, "strongSell": 0, "total": 10,
                              "period": "2026-10-01"},
                   "fetched_at": "2026-01-01T00:00:00+00:00"}
        marketaux = {"score": 0.2, "n": 6,
                     "fetched_at": "2026-01-01T00:00:00+00:00"}
        with patch.object(sentiment, "get_news", return_value=news), \
             patch.object(sentiment, "get_analyst", return_value=analyst), \
             patch.object(sentiment, "get_marketaux",
                          return_value=marketaux):
            out = sentiment.get_sentiment(store, "AAA")
        # news = mean(0.6, 0.2) = 0.4; total = 0.65*0.4 + 0.35*0.2 = 0.33
        self.assertAlmostEqual(out["sentiment"], 0.33)
        self.assertEqual(out["news"], 0.6)
        self.assertEqual(out["marketaux"], 0.2)
        self.assertEqual(out["marketaux_n"], 6)

    def test_blend_marketaux_only_when_finnhub_has_no_headlines(self):
        store = _store()
        news = {"sentiment": 0.0, "n": 0, "label": "neutral",
                "fetched_at": "2026-01-01T00:00:00+00:00"}
        analyst = {"score": 0.0,
                   "counts": {"strongBuy": 0, "buy": 0, "hold": 0,
                              "sell": 0, "strongSell": 0, "total": 0,
                              "period": ""},
                   "fetched_at": "2026-01-01T00:00:00+00:00"}
        marketaux = {"score": -0.4, "n": 3,
                     "fetched_at": "2026-01-01T00:00:00+00:00"}
        with patch.object(sentiment, "get_news", return_value=news), \
             patch.object(sentiment, "get_analyst", return_value=analyst), \
             patch.object(sentiment, "get_marketaux",
                          return_value=marketaux):
            out = sentiment.get_sentiment(store, "AAA")
        # Finnhub leg unavailable (n=0): news = -0.4 alone, NOT mean(0, -0.4)
        # total = 0.65*(-0.4) + 0.35*0 = -0.26
        self.assertAlmostEqual(out["sentiment"], -0.26)

    def test_blend_news_math(self):
        self.assertAlmostEqual(sentiment.blend_news(0.6, 4, 0.2), 0.4)
        self.assertAlmostEqual(sentiment.blend_news(0.6, 4, None), 0.6)
        self.assertAlmostEqual(sentiment.blend_news(0.0, 0, -0.4), -0.4)
        self.assertEqual(sentiment.blend_news(0.0, 0, None), 0.0)

    def test_has_signal_via_marketaux(self):
        self.assertTrue(sentiment.has_signal(
            {"n": 0, "marketaux_n": 3, "analyst_counts": {"total": 0}}))
        self.assertFalse(sentiment.has_signal(
            {"n": 0, "marketaux_n": 0, "analyst_counts": {"total": 0}}))

    def test_format_combined_with_marketaux(self):
        payload = {"news": 0.64, "n": 5, "marketaux": 0.42,
                   "marketaux_n": 6,
                   "analyst_counts": {"strongBuy": 2, "buy": 12, "hold": 4,
                                      "sell": 1, "strongSell": 0,
                                      "total": 19, "period": "2026-10-01"}}
        self.assertEqual(
            sentiment.format_combined(payload),
            "news: +0.6 | analysts: 14 buy / 4 hold / 1 sell"
            " | marketaux: +0.4 (6 articles)")
        payload["marketaux_n"] = 1
        self.assertTrue(
            sentiment.format_combined(payload).endswith("(1 article)"))


class TestScannerIntegration(unittest.TestCase):
    def _store(self):
        return Store(":memory:")

    def _bars(self):
        closes = [100.0] * 245 + [160.0] * 5
        bars = []
        for i, c in enumerate(closes):
            v = 5_000_000.0 if i == len(closes) - 1 else 1_000_000.0
            bars.append({"ts": 1700000000 + i * 86400, "o": c,
                         "h": c * 1.005, "l": c * 0.995, "c": c, "v": v})
        return {"TESTA": bars}

    def _pos_payload(self, *a, **k):
        return {"sentiment": 0.8, "news": 0.8, "analyst": 0.8, "n": 3,
                "analyst_counts": {"strongBuy": 2, "buy": 12, "hold": 4,
                                   "sell": 1, "strongSell": 0, "total": 19,
                                   "period": "2026-10-01"},
                "label": "positive",
                "fetched_at": "2026-01-01T00:00:00+00:00"}

    def _neutral_payload(self, *a, **k):
        return {"sentiment": 0.0, "news": 0.0, "analyst": 0.0, "n": 0,
                "analyst_counts": {"strongBuy": 0, "buy": 0, "hold": 0,
                                   "sell": 0, "strongSell": 0, "total": 0,
                                   "period": ""},
                "label": "neutral",
                "fetched_at": "2026-01-01T00:00:00+00:00"}

    def test_watch_mode_shows_signal_line_and_adjusted_rating(self):
        import scanner
        store, push = self._store(), MagicMock(return_value=True)
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._bars()
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch("scanner.sentiment.get_sentiment",
                   side_effect=self._pos_payload):
            out = scanner.watch_scan(store=store, rest=rest,
                                     dry_run=False, push_fn=push)
        self.assertEqual(len(out), 1)
        title, body = push.call_args[0]
        # combined sentiment 0.8 -> +1.2 contribution, shown as signal line
        self.assertIn("news: +0.8 | analysts: 14 buy / 4 hold / 1 sell", body)
        # base tech score 28 of the 28-point BUY watch scale -> 10.0/10;
        # +1.2 from sentiment is clamped at 10.0
        self.assertIn("10.0/10", body)
        self.assertIn("10.0/10", title)

    def test_watch_mode_no_signal_no_line(self):
        import scanner
        store, push = self._store(), MagicMock(return_value=True)
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._bars()
        with patch.object(scanner, "_in_market_hours", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch("scanner.sentiment.get_sentiment",
                   side_effect=self._neutral_payload):
            scanner.watch_scan(store=store, rest=rest,
                               dry_run=False, push_fn=push)
        _, body = push.call_args[0]
        self.assertNotIn("news:", body)
        self.assertNotIn("analysts:", body)
        self.assertIn("10.0/10", body)  # unadjusted: 28 of 28

    def test_run_scan_candidate_gets_sentiment(self):
        import scanner
        import sessions as sessions_mod
        store, push = self._store(), MagicMock(return_value=True)
        rest = MagicMock()
        rest.daily_bars_multi.return_value = self._bars()
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["TESTA"]), \
             patch.object(scanner, "fundamental_bonus",
                          return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             patch("scanner.sentiment.get_sentiment",
                   side_effect=self._pos_payload):
            out = scanner.run_scan(store=store, rest=rest,
                                   dry_run=False, push_fn=push)
        self.assertEqual(len(out["buys"]), 1)
        c = out["buys"][0]
        self.assertEqual(c["sentiment"], 0.8)
        self.assertEqual(c["sentiment_n"], 3)
        _, body = push.call_args[0]
        self.assertIn("news: +0.8 | analysts: 14 buy / 4 hold / 1 sell", body)
        # 28 of the 53-point morning BUY scale -> 5.3/10, +1.2 -> 6.5/10
        self.assertIn("6.5/10", body)

    def test_run_scan_only_scores_candidates(self):
        # symbols with no setups never reach the sentiment fetch. The sine
        # fixture stays mid-range (no 52w extremes), mid-RSI, constant
        # volume, and under 146 bars so the squeeze/SMA200 logic is skipped.
        import math
        import scanner
        import sessions as sessions_mod
        store = self._store()
        closes = [100.0 + 3.0 * math.sin(i * 0.3) for i in range(95)]
        bars = [{"ts": 1700000000 + i * 86400, "o": c, "h": c * 1.001,
                 "l": c * 0.999, "c": c, "v": 1_000_000.0}
                for i, c in enumerate(closes)]
        res = scanner.technical_screen(bars)
        self.assertEqual(res["setups"], [])
        self.assertEqual(res["bear_setups"], [])
        rest = MagicMock()
        rest.daily_bars_multi.return_value = {"FLAT": bars}
        with patch.object(sessions_mod, "is_trading_day", return_value=True), \
             patch.object(scanner, "get_universe", return_value=["FLAT"]), \
             patch.object(scanner, "fundamental_bonus",
                          return_value=(0, [])), \
             patch.object(scanner, "short_bonus", return_value=(0, [])), \
             patch("scanner.sentiment.get_sentiment") as gs:
            out = scanner.run_scan(store=store, rest=rest, dry_run=True)
        self.assertEqual(out, {"buys": [], "sells": []})
        gs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
