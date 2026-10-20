"""Tests for multi-timeframe bars, timeframe routing, regime gating, and
external event firing (fire_external). Synthetic ticks only, no network."""
import unittest

from alerts import AlertEngine, MarketState
from storage import Store


def make_engine(regime_fn=None):
    store = Store(":memory:")
    market = {}
    calls = []

    def notify(title, message, priority, tags):
        calls.append((title, message, priority))
        return True

    engine = AlertEngine(store, notify, market, regime_fn=regime_fn)
    return store, market, calls, engine


def feed_1m(engine, symbol, closes, t0=1_700_000_000, vol=1000):
    """Push synthetic 1m bars (one tick per minute forces bar closes)."""
    for i, c in enumerate(closes):
        # two ticks per minute: first opens the bar, second (next minute)
        # closes the previous one
        engine.on_tick(symbol, c, vol, t0 + i * 60)
    engine.on_tick(symbol, closes[-1], vol, t0 + len(closes) * 60)


class TestMTFRoll(unittest.TestCase):
    def test_5m_aggregation(self):
        store, market, calls, engine = make_engine()
        st = MarketState("AAA")
        market["AAA"] = st
        # 999900 is 5m-bucket aligned: first five bars share bucket 3333
        bars = [{"ts": 999_900 + i * 60, "o": 10 + i, "h": 11 + i,
                 "l": 9 + i, "c": 10 + i, "v": 100} for i in range(6)]
        for b in bars:
            engine._roll_mtf(st, "AAA", b, b["ts"] + 60)
        # bars 0..4 -> one 5m bar; bar 5 starts the next bucket (still forming)
        self.assertEqual(len(st.mtf["5m"]), 1)
        b5 = st.mtf["5m"][0]
        self.assertEqual(b5["o"], 10)
        self.assertEqual(b5["c"], 14)
        self.assertEqual(b5["h"], 15)
        self.assertEqual(b5["l"], 9)
        self.assertEqual(b5["v"], 500)

    def test_timeframe_routing(self):
        """A 5m rsi alert must NOT fire on 1m closes, only on 5m closes."""
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        aid = store.alert_create("AAA", "rsi_14", {"above": 70, "below": 30},
                                 cooldown_min=60, timeframe="5m")
        engine.refresh_alerts()
        # steep climb: 1m RSI would cross 70 quickly, 5m RSI needs 16 5m bars
        feed_1m(engine, "AAA", [100 + i for i in range(30)])
        st = market["AAA"]
        # only 6 five-minute bars closed -> not enough for RSI(14)
        self.assertEqual(calls, [])
        self.assertEqual(store.state_get(aid)["state"], "ARMED")

    def test_5m_alert_fires_on_5m_close(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        # volume_spike on 5m: needs 20 prior 5m bars + a spike bar
        aid = store.alert_create("AAA", "volume_spike",
                                 {"mult": 2, "lookback": 5},
                                 cooldown_min=60 * 24, timeframe="5m")
        engine.refresh_alerts()
        st = market["AAA"]
        t0 = 2_000_000
        # seed 21 normal 5m bars directly, then roll a spike
        for i in range(21):
            b = {"ts": t0 + i * 300, "o": 10, "h": 10, "l": 10, "c": 10,
                 "v": 100}
            engine._roll_mtf(st, "AAA", b, b["ts"] + 300)
        self.assertEqual(calls, [])
        spike = {"ts": t0 + 21 * 300, "o": 10, "h": 10, "l": 10, "c": 10,
                 "v": 10_000}
        engine._roll_mtf(st, "AAA", spike, spike["ts"] + 300)
        # spike bar is still forming; close it with the next bucket's bar
        nxt = {"ts": t0 + 22 * 300, "o": 10, "h": 10, "l": 10, "c": 10,
               "v": 100}
        engine._roll_mtf(st, "AAA", nxt, nxt["ts"] + 300)
        self.assertEqual(len(calls), 1)
        self.assertIn("Volume spike", calls[0][1])


class TestRegimeGating(unittest.TestCase):
    def test_bull_only_blocked_in_bear(self):
        store, market, calls, engine = make_engine(
            regime_fn=lambda: "bear")
        market["AAA"] = MarketState("AAA")
        store.alert_create("AAA", "price_above", {"target": 100},
                           regime="bull")
        store.alert_create("AAA", "price_below", {"target": 1000},
                           regime="any")
        engine.refresh_alerts()
        engine.on_tick("AAA", 101, 10, 5_000_000)
        self.assertEqual(len(calls), 1)
        self.assertIn("Price below", calls[0][1])

    def test_regime_fn_none_means_any(self):
        store, market, calls, engine = make_engine(regime_fn=lambda: None)
        market["AAA"] = MarketState("AAA")
        store.alert_create("AAA", "price_above", {"target": 100},
                           regime="bear")
        engine.refresh_alerts()
        engine.on_tick("AAA", 101, 10, 5_000_000)
        self.assertEqual(len(calls), 1)  # unknown regime -> fail open

    def test_no_regime_fn(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        store.alert_create("AAA", "price_above", {"target": 100},
                           regime="bull")
        engine.refresh_alerts()
        engine.on_tick("AAA", 101, 10, 5_000_000)
        self.assertEqual(len(calls), 1)


class TestFireExternal(unittest.TestCase):
    def test_dedupe_by_event_key(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        aid = store.alert_create("AAA", "split_announced", {},
                                 one_time=False, cooldown_min=60 * 24)
        engine.refresh_alerts()
        self.assertTrue(engine.fire_external(aid, 0, "Split 4:1", 6_000_000,
                                             "split:2026-01-01"))
        self.assertFalse(engine.fire_external(aid, 0, "Split 4:1", 6_000_001,
                                              "split:2026-01-01"))  # dup key
        self.assertEqual(len(calls), 1)
        # new key fires again (state went COOLDOWN -> force ARMED for test)
        store.state_set(aid, state="ARMED")
        self.assertTrue(engine.fire_external(aid, 0, "Split 2:1", 6_000_002,
                                             "split:2026-06-01"))
        self.assertEqual(len(calls), 2)

    def test_paused_or_expired_never_fire(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        aid = store.alert_create("AAA", "news_spike", {}, one_time=True)
        engine.refresh_alerts()
        self.assertTrue(engine.fire_external(aid, 0, "spike", 6_000_000,
                                             "news:2026-01-01"))
        self.assertEqual(store.state_get(aid)["state"], "EXPIRED")  # one-time
        self.assertFalse(engine.fire_external(aid, 0, "spike", 6_000_001,
                                              "news:2026-01-02"))
        self.assertEqual(len(calls), 1)

    def test_atr_stop_tick(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        st = market["AAA"]
        # 20 flat bars -> ATR small; entry 100, k=2
        for i, c in enumerate([100.0] * 20):
            st.bars.append({"ts": i * 60, "o": c, "h": c + 0.2,
                            "l": c - 0.2, "c": c, "v": 100})
        store.alert_create("AAA", "atr_stop",
                           {"entry": 100, "k": 2.0, "direction": "long"},
                           cooldown_min=60 * 24)
        engine.refresh_alerts()
        engine.on_tick("AAA", 100.0, 10, 7_000_000)
        self.assertEqual(calls, [])            # above stop
        engine.on_tick("AAA", 99.0, 10, 7_000_001)   # below 100-2*0.4=99.2
        self.assertEqual(len(calls), 1)
        self.assertIn("ATR stop", calls[0][1])


class TestNewBarTypes(unittest.TestCase):
    def test_bollinger_break(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        st = market["AAA"]
        for i in range(25):
            c = 100.0 + (i % 2) * 0.5  # slight oscillation -> nonzero width
            st.bars.append({"ts": i * 60, "o": c, "h": c + 0.5,
                            "l": c - 0.5, "c": c, "v": 100})
        store.alert_create("AAA", "bollinger_break", {"n": 20, "k": 2.0},
                           cooldown_min=60 * 24)
        engine.refresh_alerts()
        # close a bar far above the band
        st.bars.append({"ts": 26 * 60, "o": 100, "h": 112, "l": 100,
                        "c": 111, "v": 100})
        engine._evaluate("AAA", {"bollinger_break"}, 111, 26 * 60 + 1,
                         bar=st.bars[-1], bars=list(st.bars), tf="1m")
        self.assertEqual(len(calls), 1)
        self.assertIn("Bollinger breakout", calls[0][1])

    def test_fib_cross(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        st = market["AAA"]
        # swing 100 -> 200 over 50 bars, then cross back down through 0.5
        closes = [100 + i * 2 for i in range(50)]  # 100..198
        for i, c in enumerate(closes):
            st.bars.append({"ts": i * 60, "o": c, "h": c + 1, "l": c - 1,
                            "c": c, "v": 100})
        store.alert_create("AAA", "fib_cross", {"lookback": 50},
                           cooldown_min=60 * 24)
        engine.refresh_alerts()
        # swing high ~199, low ~99; 50% level = 149. Cross down through it.
        st.bars.append({"ts": 50 * 60, "o": 160, "h": 161, "l": 140,
                        "c": 145, "v": 100})
        engine._evaluate("AAA", {"fib_cross"}, 145, 50 * 60 + 1,
                         bar=st.bars[-1], bars=list(st.bars), tf="1m")
        self.assertEqual(len(calls), 1)
        self.assertIn("Fibonacci", calls[0][1])


if __name__ == "__main__":
    unittest.main()
