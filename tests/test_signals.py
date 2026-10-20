"""Trader-logic signal tests (Oct 20, 2026 rebuild).

Each test pins one rule of the chart-reading playbook: trend context,
dip vs momentum BUYs, sell-the-rip SELLs, the rules that were removed
because they lost money, push bars by regime, and ATR risk levels.
"""
import unittest

import scanner
from tests.test_scanner import BUY_CLOSES, SELL_CLOSES, make_bars


def names(setups):
    return {n for n, _, _ in setups}


def momentum_bars(last_jump=1.03, last_vol=5_000_000.0):
    closes = [50.0 + i * 0.2 for i in range(250)]
    closes[-1] = closes[-2] * last_jump
    return make_bars(closes, last_vol=last_vol)


FLAT_SPY = [100.0] * 250


class TestWeights(unittest.TestCase):
    def test_playbooks_top_out_at_100(self):
        dip_max = (DIP := scanner.DIP_WEIGHTS)["oversold_rsi30"] + \
            DIP["below_lower_band"] + DIP["stretched_below_sma20"] + \
            DIP["capitulation_volume"]
        self.assertEqual(dip_max, 100)
        M = scanner.MOMENTUM_WEIGHTS
        mom_max = M["leader_rs20"] + M["extended15"] + M["volume_up_day"] + \
            M["breakout20_volume"] + M["uptrend"]
        self.assertEqual(mom_max, 100)
        self.assertEqual(sum(scanner.BEAR_WEIGHTS.values()), 100)

    def test_removed_setups_are_gone(self):
        # near-52w-high, golden cross, squeeze, and every "sell weakness"
        # rule lost money in the validation — none may come back silently
        gone = {"breakout_watch", "golden_cross", "bollinger_squeeze",
                "oversold_bounce", "overbought_breakdown",
                "bollinger_breakdown", "volume_spike_down",
                "low52_breakdown", "death_cross", "atr_selloff"}
        self.assertFalse(gone & set(scanner.WEIGHTS))
        self.assertFalse(gone & set(scanner.BEAR_WEIGHTS))


class TestTrendContext(unittest.TestCase):
    def test_uptrend(self):
        self.assertEqual(scanner._trend_context(
            [50.0 + i * 0.2 for i in range(250)]), "up")

    def test_downtrend(self):
        self.assertEqual(scanner._trend_context(
            [160.0 - i * 0.3 for i in range(250)]), "down")

    def test_flat_is_neither(self):
        self.assertIsNone(scanner._trend_context([100.0] * 250))

    def test_short_history_unknown(self):
        self.assertIsNone(scanner._trend_context([100.0] * 200))


class TestDipBuy(unittest.TestCase):
    def test_full_capitulation_dip_scores_100(self):
        r = scanner.technical_screen(make_bars(BUY_CLOSES,
                                               last_vol=5_000_000.0))
        self.assertEqual(r["playbook"], "dip")
        self.assertEqual(r["tech_score"], 100)
        self.assertEqual(names(r["setups"]), {
            "oversold_rsi30", "below_lower_band", "stretched_below_sma20",
            "capitulation_volume"})
        self.assertEqual(scanner.classify_side(r), "BUY")

    def test_dip_without_volume_has_no_capitulation(self):
        r = scanner.technical_screen(make_bars(BUY_CLOSES))
        self.assertNotIn("capitulation_volume", names(r["setups"]))
        self.assertEqual(r["tech_score"], 75)

    def test_oversold_never_becomes_a_sell(self):
        # selling into oversold was the worst signal measured
        r = scanner.technical_screen(make_bars(BUY_CLOSES,
                                               last_vol=5_000_000.0))
        self.assertEqual(r["bear_score"], 0)

    def test_rsi35_tier_exclusive_with_rsi30(self):
        r = scanner.technical_screen(make_bars(BUY_CLOSES))
        n = names(r["setups"])
        self.assertIn("oversold_rsi30", n)
        self.assertNotIn("oversold_rsi35", n)


class TestMomentumBuy(unittest.TestCase):
    def test_leader_with_volume_in_uptrend(self):
        r = scanner.technical_screen(momentum_bars(), spy_closes=FLAT_SPY)
        self.assertEqual(r["playbook"], "momentum")
        n = names(r["setups"])
        self.assertIn("uptrend", n)
        self.assertIn("volume_up_day", n)
        self.assertTrue(n & {"leader_rs10", "leader_rs20"})

    def test_no_spy_no_relative_strength(self):
        r = scanner.technical_screen(momentum_bars())
        self.assertIsNone(r["indicators"]["rs63"])
        self.assertFalse(names(r["setups"]) & {"leader_rs10", "leader_rs20"})

    def test_lone_uptrend_is_far_below_the_bar(self):
        r = scanner.technical_screen(
            make_bars([50.0 + i * 0.2 for i in range(250)]))
        self.assertEqual(names(r["setups"]), {"uptrend"})
        self.assertLess(r["tech_score"],
                        scanner.push_bar("BUY", None, "momentum"))

    def test_breakout_needs_volume_and_strong_close(self):
        def bars(last_vol, close_at_high):
            b = make_bars([100.0] * 249 + [104.0], last_vol=last_vol)
            if close_at_high:
                b[-1]["h"] = b[-1]["c"]
            return b
        hit = scanner.technical_screen(bars(3_000_000.0, True))
        quiet = scanner.technical_screen(bars(None, True))
        weak_close = scanner.technical_screen(bars(3_000_000.0, False))
        self.assertIn("breakout20_volume", names(hit["setups"]))
        self.assertNotIn("breakout20_volume", names(quiet["setups"]))
        self.assertNotIn("breakout20_volume", names(weak_close["setups"]))


class TestSellTheRip(unittest.TestCase):
    def test_rally_into_downtrend_scores_100(self):
        r = scanner.technical_screen(make_bars(SELL_CLOSES))
        self.assertEqual(r["trend"], "down")
        self.assertEqual(r["bear_score"], 100)
        self.assertEqual(names(r["bear_setups"]), {
            "rally_in_downtrend", "overbought_below_200",
            "short_term_overbought_dn", "downtrend"})
        self.assertEqual(scanner.classify_side(r), "SELL")

    def test_downtrend_alone_scores_nothing(self):
        # no rally = nothing to sell into; "downtrend" only adds to a setup
        r = scanner.technical_screen(make_bars(
            [160.0 - i * 0.3 for i in range(250)]))
        self.assertEqual(r["bear_score"], 0)

    def test_overbought_in_uptrend_is_not_a_sell(self):
        # RSI > 70 in an uptrend is strength, not a sell
        closes = [50.0 + i * 0.2 for i in range(240)] + \
            [98.0 + i * 1.5 for i in range(1, 11)]
        r = scanner.technical_screen(make_bars(closes))
        self.assertGreater(r["indicators"]["rsi"], 70)
        self.assertEqual(r["bear_score"], 0)


class TestPushBars(unittest.TestCase):
    def test_base_bars(self):
        self.assertEqual(scanner.push_bar("BUY", None, "dip"), 60)
        self.assertEqual(scanner.push_bar("BUY", None, "momentum"), 75)
        self.assertEqual(scanner.push_bar("SELL", None), 40)

    def test_regime_picks_the_buy_playbook(self):
        # bull: momentum favoured, dips need more
        self.assertEqual(scanner.push_bar("BUY", "bull", "dip"), 75)
        self.assertEqual(scanner.push_bar("BUY", "bull", "momentum"), 75)
        # corrections: dips favoured, momentum needs more
        for rg in ("sideways", "bear"):
            self.assertEqual(scanner.push_bar("BUY", rg, "dip"), 60)
            self.assertEqual(scanner.push_bar("BUY", rg, "momentum"), 90)

    def test_bear_regime_pauses_sells(self):
        self.assertEqual(scanner.push_bar("SELL", "bull"), 40)
        self.assertEqual(scanner.push_bar("SELL", "sideways"), 40)
        self.assertEqual(scanner.push_bar("SELL", "bear"), 90)

    def test_unknown_playbook_falls_back_to_dip(self):
        self.assertEqual(scanner.push_bar("BUY", None, None), 60)


class TestRiskLevels(unittest.TestCase):
    def test_buy_and_sell_levels(self):
        self.assertEqual(scanner.risk_levels(100.0, 2.0, "BUY"), (96.0, 106.0))
        self.assertEqual(scanner.risk_levels(100.0, 2.0, "SELL"),
                         (104.0, 94.0))
        self.assertIsNone(scanner.risk_levels(100.0, None, "BUY"))

    def test_screen_carries_levels_and_note(self):
        r = scanner.technical_screen(make_bars(BUY_CLOSES,
                                               last_vol=5_000_000.0))
        stop, tgt = r["levels"]["BUY"]
        self.assertLess(stop, r["price"])
        self.assertGreater(tgt, r["price"])
        note = scanner.risk_note(r, "BUY")
        self.assertTrue(note.startswith("stop $"))
        self.assertIn("target $", note)


class TestRatingScale(unittest.TestCase):
    def test_same_scale_both_sides(self):
        self.assertEqual(scanner.side_rating(100, "BUY"), "10.0/10")
        self.assertEqual(scanner.side_rating(100, "SELL"), "10.0/10")
        self.assertEqual(scanner.side_rating(60, "BUY"), "6.0/10")


if __name__ == "__main__":
    unittest.main()
