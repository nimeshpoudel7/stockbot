"""The 15-minute watch pass leaves a heartbeat on every run, so the
dashboard can tell a quiet day from a stopped scanner."""
import json
import unittest
from unittest.mock import MagicMock, patch

import scanner
from storage import Store
from tests.test_scanner import BUY_CLOSES, _patch_sentiment, make_bars


class _Rest:
    def __init__(self, bars):
        self.bars = bars

    def daily_bars_multi(self, symbols, days=300):
        return {s: self.bars[s] for s in symbols if s in self.bars}


def _run(store, bars, *, dry_run=False, open_=True, push=None):
    with patch.object(scanner, "_in_market_hours", return_value=open_), \
            patch.object(scanner, "get_universe", return_value=list(bars)), \
            patch.object(scanner, "_current_regime", return_value=None), \
            patch.object(scanner, "earnings_note", return_value=None), \
            _patch_sentiment():
        return scanner.watch_scan(store=store, rest=_Rest(bars),
                                  dry_run=dry_run,
                                  push_fn=push or MagicMock(return_value=True))


def _last(store):
    raw = store.poller_get(scanner.SCAN_WATCH_LAST_KEY)
    return json.loads(raw) if raw else None


class TestWatchHeartbeat(unittest.TestCase):
    def test_quiet_pass_still_records(self):
        store = Store(":memory:")
        out = _run(store, {"FLAT": make_bars([100.0] * 250)})
        self.assertEqual(out, [])
        last = _last(store)
        self.assertEqual(last["screened"], 1)
        self.assertEqual(last["new"], 0)
        self.assertIsNone(last["push_ok"])
        self.assertEqual(last["interval_min"], scanner.WATCH_INTERVAL_MIN)
        self.assertIn("T", last["ts"])

    def test_match_records_count_and_push_result(self):
        store = Store(":memory:")
        bars = {"DIP": make_bars(BUY_CLOSES, last_vol=5_000_000.0),
                "FLAT": make_bars([100.0] * 250)}
        _run(store, bars, push=MagicMock(return_value=False))
        last = _last(store)
        self.assertEqual((last["screened"], last["new"], last["push_ok"]),
                         (2, 1, False))

    def test_dry_run_and_closed_market_leave_no_heartbeat(self):
        store = Store(":memory:")
        _run(store, {"FLAT": make_bars([100.0] * 250)}, dry_run=True)
        self.assertIsNone(_last(store))
        _run(store, {"FLAT": make_bars([100.0] * 250)}, open_=False)
        self.assertIsNone(_last(store))

    def test_dashboard_exposes_heartbeat(self):
        import dashboard
        store = Store(":memory:")
        _run(store, {"FLAT": make_bars([100.0] * 250)})
        app = dashboard.create_app(store, MagicMock(), {}, {})
        ep = next(r.endpoint for r in app.routes
                  if getattr(r, "path", "") == "/api/scanner")
        self.assertEqual(ep()["watch_last"]["screened"], 1)


if __name__ == "__main__":
    unittest.main()
