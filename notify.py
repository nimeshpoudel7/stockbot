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


NTFY_MAX_BYTES = 4096   # longer messages arrive as a file attachment
SAFE_BYTES = 3900


def fit_message(message: str, limit: int = SAFE_BYTES) -> str:
    """Trim to fit one ntfy message. Cuts at a blank line (between picks)
    when it can, and says how much was left out."""
    raw = message.encode("utf-8")
    if len(raw) <= limit:
        return message
    note = "\n\n…more on the dashboard"
    budget = limit - len(note.encode("utf-8"))
    cut = raw[:budget].decode("utf-8", errors="ignore")
    para = cut.rfind("\n\n")
    if para > budget // 2:
        cut = cut[:para]
    return cut.rstrip() + note


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
    # JSON publishing goes to the server ROOT; the topic travels in the body.
    # Posting JSON to /<topic> makes ntfy treat the whole JSON document as
    # the message text — which is exactly what showed up on the phone.
    url = server.rstrip('/') + '/'
    message = fit_message(message)
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
