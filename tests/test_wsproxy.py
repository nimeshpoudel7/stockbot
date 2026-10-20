"""Tests for wsproxy: CONNECT tunneling through the egress HTTP proxy."""
import os
import socket
import threading
import unittest
from unittest import mock

from wsproxy import connect_through_proxy


def _fake_proxy(response: bytes):
    """Run a one-shot fake HTTP proxy; return (port, seen, thread)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen = {}

    def serve():
        conn, _ = srv.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        seen["request"] = data
        conn.sendall(response)
        conn.close()
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return port, seen, t


class WsProxyTest(unittest.TestCase):
    def test_no_proxy_env_returns_none(self):
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(connect_through_proxy("example.com", 443))

    def test_no_proxy_host_bypass(self):
        with mock.patch.dict(
            os.environ,
            {"https_proxy": "http://127.0.0.1:9", "no_proxy": "example.com"},
            clear=False,
        ):
            self.assertIsNone(connect_through_proxy("example.com", 443))

    def test_connect_tunnel_ok(self):
        port, seen, t = _fake_proxy(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        with mock.patch.dict(
            os.environ,
            {"https_proxy": f"http://user:pass@127.0.0.1:{port}", "no_proxy": ""},
        ):
            sock = connect_through_proxy("stream.data.alpaca.markets", 443, timeout=5)
        t.join(timeout=5)
        try:
            self.assertIsNotNone(sock)
            req = seen["request"].decode("latin1")
            self.assertIn("CONNECT stream.data.alpaca.markets:443 HTTP/1.1", req)
            self.assertIn("Proxy-Authorization: Basic", req)
        finally:
            sock.close()

    def test_connect_tunnel_rejected(self):
        port, seen, t = _fake_proxy(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        with mock.patch.dict(
            os.environ,
            {"https_proxy": f"http://127.0.0.1:{port}", "no_proxy": ""},
        ):
            with self.assertRaises(OSError):
                connect_through_proxy("example.com", 443, timeout=5)
        t.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
