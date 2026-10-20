"""Push notifications via any ntfy-compatible server — free, no account needed.

The user installs the free ntfy app (iOS/Android), subscribes to the topic
name in NTFY_TOPIC, and alert pushes land on his phone in ~1 second.

NOTE: public ntfy servers are public — the topic name is the only secret, so
make it hard to guess (e.g. "stockbot-x7kq2-alerts", never "myalerts").
Set NTFY_SERVER to pick the server (default https://ntfy.sh).
"""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger("stockbot.notify")


def send_ntfy(server: str, topic: str, title: str, message: str,
              priority: int = 3, tags: list[str] | None = None,
              timeout: int = 30, retries: int = 2) -> bool:
    """POST one push to an ntfy-compatible server. Returns True on 200."""
    if not topic:
        log.warning("ntfy: no topic configured, alert dropped")
        return False
    priority = max(1, min(5, int(priority)))
    body = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or ["chart_with_upwards_trend"],
    }
    url = f"{server.rstrip('/')}/{topic}"
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=body, timeout=timeout)
            if r.status_code == 200:
                return True
            log.warning("ntfy: HTTP %s for topic %s (attempt %d)",
                        r.status_code, topic, attempt + 1)
        except Exception as e:  # noqa: BLE001 - never crash the engine on notify
            last_err = e
            log.warning("ntfy: send failed (%s, attempt %d)", e, attempt + 1)
        time.sleep(2)
    if last_err:
        log.warning("ntfy: giving up on topic %s: %s", topic, last_err)
    return False
