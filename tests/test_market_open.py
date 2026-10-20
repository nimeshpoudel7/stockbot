"""Tests for the market-open bell (market_open.py)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_open
from storage import Store


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(path)
    s.conn.execute(
        "CREATE TABLE IF NOT EXISTS poller_state (key TEXT PRIMARY KEY, value TEXT, updated_at REAL)"
    )
    return s, path


class TestBuildMessage(unittest.TestCase):
    def test_with_universe_and_regime(self):
        title, msg = market_open.build_message(13520, "bull")
        self.assertEqual(title, "Market open")
        self.assertIn("13,520", msg)
        self.assertIn("bull", msg)

    def test_fallbacks(self):
        title, msg = market_open.build_message(None, None)
        self.assertIn("full market", msg)
        self.assertNotIn("regime", msg)


class TestUniverseSize(unittest.TestCase):
    def test_reads_cache(self):
        s, path = _store()
        try:
            s.poller_set("scanner_universe_v2",
                         json.dumps({"symbols": ["A", "B", "C"]}))
            self.assertEqual(market_open.universe_size(s), 3)
        finally:
            os.unlink(path)

    def test_missing_cache(self):
        s, path = _store()
        try:
            self.assertIsNone(market_open.universe_size(s))
        finally:
            os.unlink(path)


class TestMainQuietOffHours(unittest.TestCase):
    def test_not_trading_day_sends_nothing(self):
        s, path = _store()
        try:
            with mock.patch.object(market_open, "is_trading_day_today",
                                   return_value=False), \
                 mock.patch("market_open.send_ntfy") as push, \
                 mock.patch("market_open.Store", return_value=s), \
                 mock.patch("market_open.Settings") as settings:
                settings.from_env.return_value.db_path = path
                rc = market_open.main()
            self.assertEqual(rc, 0)
            push.assert_not_called()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
