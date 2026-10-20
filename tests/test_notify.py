"""ntfy pushes: JSON goes to the server root (posting it to /<topic> made
the phone show raw JSON), messages stay under the 4 KB attachment cap,
and scanner picks render as short readable blocks."""
import unittest
from unittest.mock import MagicMock, patch

import notify
import scanner
from tests.test_scanner import BUY_CLOSES, SELL_CLOSES, make_bars


class TestSendNtfy(unittest.TestCase):
    def test_json_goes_to_server_root_with_topic_in_body(self):
        resp = MagicMock(status_code=200)
        with patch.object(notify.requests, "post", return_value=resp) as post:
            ok = notify.send_ntfy("https://ntfy.sh/", "my-topic", "Title",
                                  "Hello", priority=4, tags=["bell"])
        self.assertTrue(ok)
        url = post.call_args[0][0]
        body = post.call_args.kwargs["json"]
        self.assertEqual(url, "https://ntfy.sh/")
        self.assertEqual(body["topic"], "my-topic")
        self.assertEqual(body["message"], "Hello")
        self.assertEqual(body["title"], "Title")

    def test_long_message_trimmed_under_cap(self):
        blocks = "\n\n".join("🟢 BUY: XYZ  $10.00\n• " + "x" * 200
                             for _ in range(40))
        out = notify.fit_message(blocks)
        self.assertLessEqual(len(out.encode()), notify.NTFY_MAX_BYTES)
        self.assertTrue(out.endswith("…more on the dashboard"))
        self.assertNotIn("…more", notify.fit_message("short"))


class TestPushBlock(unittest.TestCase):
    def test_buy_block_reads_like_a_ticket(self):
        r = scanner.technical_screen(make_bars(BUY_CLOSES,
                                               last_vol=5_000_000.0))
        row = dict(r, symbol="DIPX")
        blk = scanner.push_block(row, "BUY", "9.1/10",
                                 scanner._candidate_reasons(row, "BUY"))
        lines = blk.split("\n")
        self.assertTrue(lines[0].startswith("🟢 BUY: DIPX  $70.00  ·  9.1/10"))
        self.assertIn("dip buy", lines[0])
        self.assertTrue(all(l.startswith("• ") for l in lines[1:-1]))
        self.assertLessEqual(len(lines) - 2, scanner.PUSH_REASON_LINES)
        self.assertRegex(lines[-1], r"^Stop \$[\d.]+ \(−[\d.]+%\)  ·  "
                                    r"Target \$[\d.]+ \(\+[\d.]+%\)$")
        self.assertNotIn("{", blk)

    def test_sell_block(self):
        r = scanner.technical_screen(make_bars(SELL_CLOSES))
        row = dict(r, symbol="RIPX")
        blk = scanner.push_block(row, "SELL", "10.0/10",
                                 scanner._candidate_reasons(row, "SELL"))
        self.assertTrue(blk.startswith("🔴 SELL: RIPX"))
        self.assertIn("Stop $", blk.split("\n")[-1])

    def test_full_morning_push_fits_one_message(self):
        per = 300  # worst-case block bytes measured on real data
        self.assertLess(2 * scanner.SCAN_TOP_N * per + 200,
                        notify.SAFE_BYTES)
        self.assertLess(scanner.WATCH_PUSH_MAX * per + 100,
                        notify.SAFE_BYTES)


if __name__ == "__main__":
    unittest.main()
