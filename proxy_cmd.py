#!/usr/bin/env python3
"""ssh ProxyCommand: shuttle stdio through an HTTP CONNECT tunnel.

Reads the egress proxy from https_proxy/HTTPS_PROXY (with credentials).
Usage: proxy_cmd.py <host> <port>
"""
import base64
import os
import socket
import sys
import threading
import urllib.parse


def main() -> int:
    host, port = sys.argv[1], int(sys.argv[2])
    px = urllib.parse.urlparse(
        os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
    )
    if not px.hostname:
        print("proxy_cmd: no https_proxy in environment", file=sys.stderr)
        return 1
    s = socket.create_connection((px.hostname, px.port or 8080), timeout=20)
    try:
        user = urllib.parse.unquote(px.username or "")
        pwd = urllib.parse.unquote(px.password or "")
        auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        s.sendall(
            f"CONNECT {host}:{port} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                print("proxy_cmd: proxy closed connection", file=sys.stderr)
                return 1
            resp += chunk
        if b" 200 " not in resp.split(b"\r\n", 1)[0]:
            print(f"proxy_cmd: CONNECT failed: {resp.split(b"\r")[0]!r}",
                  file=sys.stderr)
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"proxy_cmd: {e}", file=sys.stderr)
        s.close()
        return 1

    def stdin_to_sock():
        try:
            while True:
                data = os.read(0, 65536)
                if not data:
                    break
                s.sendall(data)
        except OSError:
            pass
        finally:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def sock_to_stdout():
        out = sys.stdout.buffer
        try:
            while True:
                data = s.recv(65536)
                if not data:
                    break
                out.write(data)
                out.flush()
        except OSError:
            pass

    t = threading.Thread(target=sock_to_stdout, daemon=True)
    t.start()
    stdin_to_sock()
    t.join(timeout=5)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
