"""Open wss:// connections through an egress HTTP proxy (CONNECT tunnel).

This VM intercepts direct outbound TLS, so a plain websockets.connect()
fails with ``SSL: WRONG_VERSION_NUMBER``. All external traffic must go
through the HTTP proxy in ``https_proxy``/``HTTPS_PROXY``.

On a normal machine (no proxy env vars, or host matched by ``no_proxy``)
``connect_through_proxy`` returns ``None`` and the caller connects directly,
so this module is portable.

Use via :func:`ws_connect`, a drop-in replacement for
``websockets.connect``::

    from wsproxy import ws_connect
    async with await ws_connect("wss://example.com/stream", ping_interval=20) as ws:
        ...
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import urllib.parse

log = logging.getLogger("stockbot.wsproxy")


def _proxy_url_for(host: str) -> str | None:
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    for entry in (e.strip() for e in no_proxy.split(",") if e.strip()):
        if host == entry or host.endswith("." + entry.lstrip(".")):
            return None
    return (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )


def connect_through_proxy(host: str, port: int, timeout: float = 15) -> socket.socket | None:
    """Return a TCP socket tunneled to host:port via HTTP CONNECT, or None.

    Returns None when no proxy is configured (or the host is in no_proxy),
    meaning the caller should connect directly.
    """
    proxy_url = _proxy_url_for(host)
    if not proxy_url:
        return None
    p = urllib.parse.urlparse(proxy_url)
    if p.scheme not in ("http", "https") or not p.hostname:
        log.warning("ignoring unsupported proxy url scheme: %s", p.scheme)
        return None
    sock = socket.create_connection((p.hostname, p.port or 8080), timeout=timeout)
    try:
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if p.username:
            user = urllib.parse.unquote(p.username)
            pwd = urllib.parse.unquote(p.password or "")
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        lines += ["Proxy-Connection: Keep-Alive", "", ""]
        sock.sendall("\r\n".join(lines).encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("proxy closed the connection during CONNECT")
            resp += chunk
            if len(resp) > 65536:
                raise OSError("proxy sent an oversized CONNECT response")
        status_line = resp.split(b"\r\n", 1)[0].decode("latin1")
        if " 200 " not in f" {status_line} ":
            raise OSError(f"proxy CONNECT failed: {status_line}")
        return sock
    except Exception:
        sock.close()
        raise


async def ws_connect(url: str, **kwargs):
    """Drop-in for ``websockets.connect`` that tunnels via the egress proxy.

    Returns the same async-context-manager object ``websockets.connect``
    returns, so call sites become ``async with await ws_connect(url) as ws:``.
    """
    import websockets  # local import: keeps this module importable without it

    _apply_connection_header_workaround()
    p = urllib.parse.urlparse(url)
    port = p.port or (443 if p.scheme == "wss" else 80)
    sock = await asyncio.to_thread(connect_through_proxy, p.hostname or "", port)
    if sock is not None:
        kwargs["sock"] = sock
    return websockets.connect(url, **kwargs)


_workaround_applied = False


def _apply_connection_header_workaround() -> None:
    """Tolerate the egress proxy rewriting ``Connection: Upgrade``.

    The proxy in front of this VM rewrites the ``Connection`` header to
    ``close`` on ``101 Switching Protocols`` responses. The ``websockets``
    client strictly rejects that (RFC 6455), even though the upgrade itself
    succeeded.

    This wraps ``ClientProtocol.process_response`` so that a 101 response is
    still accepted when it carries ``Upgrade: websocket`` *and* a correct
    ``Sec-WebSocket-Accept`` key — which only the real server can produce.
    Every other validation stays exactly as strict as the library's.
    """
    global _workaround_applied
    if _workaround_applied:
        return
    from websockets.client import ClientProtocol
    from websockets.exceptions import InvalidUpgrade
    from websockets.utils import accept_key

    orig_process_response = ClientProtocol.process_response

    def process_response(self, response):  # type: ignore[no-untyped-def]
        try:
            return orig_process_response(self, response)
        except InvalidUpgrade as exc:
            if exc.name != "Connection":
                raise
            if response.status_code != 101:
                raise
            if response.headers.get("Upgrade", "").lower() != "websocket":
                raise
            try:
                accept = response.headers["Sec-WebSocket-Accept"]
            except KeyError:
                raise
            if accept != accept_key(self.key):
                raise
            log.debug(
                "accepted 101 with proxy-mangled Connection header "
                "(valid Sec-WebSocket-Accept)"
            )
            return None

    ClientProtocol.process_response = process_response
    _workaround_applied = True
