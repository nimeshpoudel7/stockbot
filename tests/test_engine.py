"""Tests for the alert engine state machine (alerts.py) with synthetic ticks.

Covers: ARMED -> TRIGGERED -> COOLDOWN -> ARMED, hysteresis re-arm,
one-time -> EXPIRED, expiry dates, per-minute dedupe, restart persistence,
and the volume-spike bar-close path. No network involved.
"""
import unittest

from alerts import AlertEngine, MarketState
from storage import Store


def make_engine():
    store = Store(":memory:")
    market = {}
    calls = []

    def notify(title, message, priority, tags):
        calls.append((title, message, priority))
        return True

    engine = AlertEngine(store, notify, market)
    return store, market, calls, engine


class TestPriceAlertLifecycle(unittest.TestCase):
    def test_full_cycle_with_hysteresis(self):
        store, market, calls, engine = make_engine()
        market["AAA"] = MarketState("AAA")
        aid = store.alert_create("AAA", "price_above", {"target": 230},
                                 one_time=False, cooldown_min=15,
                                 hysteresis_pct=0.005)
        engine.refresh_alerts()
        t0 = 10_000_000

        engine.on_tick("AAA", 229.0, 10, t0)
        self.assertEqual(calls, [])                       # armed, no fire

        engine.on_tick("AAA", 231.0, 10, t0 + 1)
        self.assertEqual(len(calls), 1)                    # TRIGGERED
        self.assertIn("Price above $230.00", calls[0][1])
        self.assertEqual(store.state_get(aid)["state"], "COOLDOWN")

        engine.on_tick("AAA", 235.0, 10, t0 + 300)
        self.assertEqual(len(calls), 1)                    # cooldown, no refire

        # cooldown elapsed but price never moved back -> stays COOLDOWN
        engine.on_tick("AAA", 235.0, 10, t0 + 901)
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.state_get(aid)["state"], "COOLDOWN")

        # price moves back 0.5% past trigger (<= 229.845) -> re-armed
        engine.on_tick("AAA", 229.0, 10, t0 + 902)
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.state_get(aid)["state"], "ARMED")

        engine.on_tick("AAA", 231.0, 10, t0 + 903)
        self.assertEqual(len(calls), 2)                    # fires again

    def test_one_time_goes_expired(self):
        store, market, calls, engine = make_engine()
        market["BBB"] = MarketState("BBB")
        aid = store.alert_create("BBB", "price_below", {"target": 100},
                                 one_time=True)
        engine.refresh_alerts()
        engine.on_tick("BBB", 99.0, 1, 5000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.state_get(aid)["state"], "EXPIRED")
        engine.on_tick("BBB", 50.0, 1, 6000)
        self.assertEqual(len(calls), 1)                    # never again

    def test_expiry_date(self):
        store, market, calls, engine = make_engine()
        market["CCC"] = MarketState("CCC")
        aid = store.alert_create("CCC", "price_above", {"target": 10},
                                 expires_at="2020-01-01T00:00:00+00:00")
        engine.refresh_alerts()
        engine.on_tick("CCC", 999.0, 1, 1_791_000_000)  # Sep 2026, past expiry
        self.assertEqual(calls, [])
        self.assertEqual(store.state_get(aid)["state"], "EXPIRED")

    def test_dedupe_same_minute(self):
        store, market, calls, engine = make_engine()
        market["DDD"] = MarketState("DDD")
        aid = store.alert_create("DDD", "price_above", {"target": 10},
                                 cooldown_min=0)
        engine.refresh_alerts()
        engine.on_tick("DDD", 11.0, 1, 7000)
        self.assertEqual(len(calls), 1)
        # an event row exists for this trigger minute
        self.assertTrue(store.event_exists_for_minute(aid, 7000))

    def test_restart_never_refires(self):
        store, market, calls, engine = make_engine()
        market["EEE"] = MarketState("EEE")
        aid = store.alert_create("EEE", "price_above", {"target": 50},
                                 cooldown_min=60)
        engine.refresh_alerts()
        engine.on_tick("EEE", 55.0, 1, 8000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.state_get(aid)["state"], "COOLDOWN")

        # "restart": brand-new engine over the same DB
        calls2 = []
        engine2 = AlertEngine(
            store, lambda t, m, p, g: calls2.append(t) or True, market)
        engine2.on_tick("EEE", 60.0, 1, 8100)   # still in cooldown
        self.assertEqual(calls2, [])
        self.assertEqual(store.state_get(aid)["state"], "COOLDOWN")

    def test_paused_alert_skipped(self):
        store, market, calls, engine = make_engine()
        market["FFF"] = MarketState("FFF")
        aid = store.alert_create("FFF", "price_above", {"target": 10})
        store.alert_update(aid, paused=True)
        engine.refresh_alerts()
        engine.on_tick("FFF", 999.0, 1, 9000)
        self.assertEqual(calls, [])


class TestDayPctAnd52w(unittest.TestCase):
    def test_day_pct_change(self):
        store, market, calls, engine = make_engine()
        st = MarketState("GHI")
        st.prev_close = 100.0
        market["GHI"] = st
        store.alert_create("GHI", "day_pct_change", {"pct": 5})
        engine.refresh_alerts()
        engine.on_tick("GHI", 104.9, 1, 10000)
        self.assertEqual(calls, [])
        engine.on_tick("GHI", 105.1, 1, 10001)
        self.assertEqual(len(calls), 1)
        self.assertIn("Up 5.10% on the day", calls[0][1])

    def test_day_pct_hysteresis_rearm(self):
        store, market, calls, engine = make_engine()
        st = MarketState("QQQ")
        st.prev_close = 100.0
        market["QQQ"] = st
        aid = store.alert_create("QQQ", "day_pct_change", {"pct": 5},
                                 cooldown_min=1, hysteresis_pct=0.005)
        engine.refresh_alerts()
        t0 = 20_000_000
        engine.on_tick("QQQ", 105.5, 1, t0)       # +5.5% -> fires
        self.assertEqual(len(calls), 1)
        engine.on_tick("QQQ", 105.5, 1, t0 + 61)  # cooldown done, still hot
        self.assertEqual(store.state_get(aid)["state"], "COOLDOWN")
        engine.on_tick("QQQ", 104.9, 1, t0 + 62)  # +4.9% <= 4.975 -> re-arm
        self.assertEqual(store.state_get(aid)["state"], "ARMED")
        engine.on_tick("QQQ", 105.5, 1, t0 + 63)   # fires again
        self.assertEqual(len(calls), 2)

    def test_52w_high(self):
        store, market, calls, engine = make_engine()
        st = MarketState("JKL")
        st.high_52w = 200.0
        market["JKL"] = st
        store.alert_create("JKL", "high_52w", {})
        engine.refresh_alerts()
        engine.on_tick("JKL", 199.0, 1, 11000)
        self.assertEqual(calls, [])
        engine.on_tick("JKL", 200.5, 1, 11001)
        self.assertEqual(len(calls), 1)
        self.assertIn("52-week high", calls[0][1])


class TestBarCloseAlerts(unittest.TestCase):
    def test_volume_spike_on_bar_close(self):
        store, market, calls, engine = make_engine()
        st = MarketState("MNO")
        for i in range(20):  # 20 quiet 1m bars
            st.bars.append({"ts": i * 60, "o": 1, "h": 1, "l": 1,
                            "c": 1, "v": 100.0})
        market["MNO"] = st
        store.alert_create("MNO", "volume_spike",
                           {"mult": 3, "lookback": 20}, cooldown_min=0)
        engine.refresh_alerts()
        base = 2000 * 60
        for i in range(7):  # loud bar: 350 total volume
            engine.on_tick("MNO", 1.0, 50.0, base + i)
        self.assertEqual(calls, [])          # not evaluated mid-bar
        engine.on_tick("MNO", 1.0, 1.0, base + 60)  # next minute closes it
        self.assertEqual(len(calls), 1)
        self.assertIn("Volume spike", calls[0][1])

    def test_rsi_needs_warmup(self):
        store, market, calls, engine = make_engine()
        st = MarketState("PQR")
        for i in range(5):  # far fewer than the 16 bars RSI needs
            st.bars.append({"ts": i * 60, "o": 1, "h": 1, "l": 1,
                            "c": 1, "v": 10.0})
        market["PQR"] = st
        store.alert_create("PQR", "rsi_14", {"above": 70, "below": 30})
        engine.refresh_alerts()
        engine.on_tick("PQR", 1.0, 1.0, 3000)
        engine.on_tick("PQR", 1.0, 1.0, 3060)
        self.assertEqual(calls, [])          # not enough data, stays quiet


if __name__ == "__main__":
    unittest.main()
