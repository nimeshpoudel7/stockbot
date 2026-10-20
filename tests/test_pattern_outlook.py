"""'What usually happens next': textbook text + historical odds."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pattern_outlook as P


def _stats(path, key, **kw):
    st = {"n": 500, "up5": 55.0, "up10": 58.0, "avg5": 0.5, "avg10": 1.4, "med10": 0.9}
    st.update(kw)
    json.dump({"meta": {}, "base": {"n": 9000, "up10": 52.0, "avg10": 0.4},
               "stats": {key: st}}, open(path, "w"))


class TestOutlook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "s.json")
        P._STATS = None

    def test_key_matches_study_format(self):
        f = {"name": "Double top", "bias": "bear", "status": "Confirmed"}
        self.assertEqual(P.finding_key(f), "Double top|bear|Confirmed")
        f = {"name": "Hammer", "bias": "bull", "status": "Today"}
        self.assertEqual(P.finding_key(f), "Hammer|bull")

    def test_textbook_status_then_bias_then_any(self):
        self.assertIn("pullbacks", P.textbook({"name": "Pullback vs reversal", "bias": "bull", "status": "Pullback"}))
        self.assertIn("more than a pullback", P.textbook({"name": "Pullback vs reversal", "bias": "bear", "status": "Possible reversal"}))
        self.assertIn("follow-through", P.textbook({"name": "Hammer", "bias": "bull", "status": "Today"}))
        self.assertIsNone(P.textbook({"name": "Unknown thing", "bias": "bull", "status": "x"}))

    def test_history_and_verdicts(self):
        _stats(self.path, "RSI (14)|bull")
        with patch.object(P, "STATS_PATH", self.path):
            f = {"name": "RSI (14)", "bias": "bull", "status": "28", "active": True}
            P.annotate([{"items": [f]}])
        self.assertEqual(f["odds"]["edge10"], 1.0)
        self.assertIn("helped a little", f["odds_text"])
        self.assertIn("58%", f["odds_text"])

    def test_bear_warning_that_failed_says_so(self):
        _stats(self.path, "Volume climax|bear", avg10=1.6)
        with patch.object(P, "STATS_PATH", self.path):
            f = {"name": "Volume climax", "bias": "bear", "status": "x", "active": True}
            P.annotate([{"items": [f]}])
        self.assertIn("usually failed", f["odds_text"])

    def test_thin_samples_and_missing_file_hidden(self):
        _stats(self.path, "RSI (14)|bull", n=12)
        with patch.object(P, "STATS_PATH", self.path):
            self.assertIsNone(P.history({"name": "RSI (14)", "bias": "bull", "status": "x"}))
        P._STATS = None
        with patch.object(P, "STATS_PATH", os.path.join(self.tmp, "none.json")):
            self.assertIsNone(P.history({"name": "RSI (14)", "bias": "bull", "status": "x"}))

    def test_inactive_findings_untouched(self):
        f = {"name": "Hammer", "bias": "bull", "status": "Not present", "active": False}
        P.annotate([{"items": [f]}])
        self.assertNotIn("next", f)

    def test_shipped_stats_file_is_valid(self):
        P._STATS = None
        data = P._load()
        self.assertGreater(data["base"]["n"], 10000)
        self.assertGreater(len(data["stats"]), 50)


if __name__ == "__main__":
    unittest.main()
