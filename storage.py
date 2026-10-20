"""SQLite persistence: watchlist, alerts, alert state, candle cache, alert events.

The alert state table is the heart of the "never re-fire after a restart"
guarantee: every state transition is committed immediately, and on boot the
engine reloads each alert's last known state instead of starting ARMED.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist(
    symbol TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    type TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    one_time INTEGER NOT NULL DEFAULT 0,
    cooldown_min REAL NOT NULL DEFAULT 15,
    expires_at TEXT,
    hysteresis_pct REAL NOT NULL DEFAULT 0.005,
    ntfy_priority INTEGER NOT NULL DEFAULT 3,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_state(
    alert_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'ARMED',
    last_trigger_ts TEXT,
    cooldown_until TEXT,
    rearm_ref REAL,
    last_bar_ts INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candles(
    symbol TEXT NOT NULL,
    tf TEXT NOT NULL DEFAULT '1m',
    ts INTEGER NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    v REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(symbol, tf, ts)
);
CREATE TABLE IF NOT EXISTS alert_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_candles_sym_tf_ts ON candles(symbol, tf, ts);
CREATE INDEX IF NOT EXISTS idx_events_alert_ts ON alert_events(alert_id, ts);
CREATE TABLE IF NOT EXISTS fundamentals_cache(
    symbol TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poller_state(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_calendar(
    date TEXT PRIMARY KEY,
    is_open INTEGER NOT NULL DEFAULT 1,
    open_time TEXT,
    close_time TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created by older versions."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "timeframe" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN timeframe TEXT NOT NULL DEFAULT '1m'")
    if "regime" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN regime TEXT NOT NULL DEFAULT 'any'")


class Store:
    """Thread-safe SQLite store. All timestamps stored as ISO UTC strings,
    except candle ``ts`` which is an integer epoch minute."""

    def __init__(self, path: str):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.executescript(SCHEMA)
            _migrate(self.conn)

    # ---- watchlist ------------------------------------------------------
    def watchlist_add(self, symbol: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO watchlist(symbol, added_at) VALUES(?, ?)",
                (symbol.upper(), _utcnow()),
            )

    def watchlist_remove(self, symbol: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM watchlist WHERE symbol=?", (symbol.upper(),))

    def watchlist_all(self) -> list[str]:
        with self._lock:
            return [r["symbol"] for r in
                    self.conn.execute("SELECT symbol FROM watchlist ORDER BY symbol")]

    # ---- alerts ---------------------------------------------------------
    def alert_create(self, symbol: str, type_: str, params: dict,
                     one_time: bool = False, cooldown_min: float = 15.0,
                     expires_at: str | None = None,
                     hysteresis_pct: float = 0.005,
                     ntfy_priority: int = 3,
                     timeframe: str = "1m", regime: str = "any") -> int:
        timeframe = timeframe if timeframe in ("1m", "5m", "15m", "1h") else "1m"
        regime = regime if regime in ("any", "bull", "bear") else "any"
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO alerts(symbol, type, params, one_time, cooldown_min,
                                      expires_at, hysteresis_pct, ntfy_priority,
                                      paused, created_at, timeframe, regime)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (symbol.upper(), type_, json.dumps(params), int(one_time),
                 cooldown_min, expires_at, hysteresis_pct, ntfy_priority,
                 _utcnow(), timeframe, regime),
            )
            alert_id = cur.lastrowid
            self.conn.execute(
                """INSERT OR IGNORE INTO alert_state(alert_id, state, updated_at)
                   VALUES(?, 'ARMED', ?)""",
                (alert_id, _utcnow()),
            )
            return alert_id

    def alert_get(self, alert_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["params"] = json.loads(d["params"])
            return d

    def alert_list(self, symbol: str | None = None) -> list[dict]:
        with self._lock:
            q = ("SELECT a.*, s.state AS live_state FROM alerts a "
                 "LEFT JOIN alert_state s ON s.alert_id = a.id")
            args: tuple = ()
            if symbol:
                q += " WHERE a.symbol=?"
                args = (symbol.upper(),)
            q += " ORDER BY a.symbol, a.id"
            rows = self.conn.execute(q, args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["params"] = json.loads(d["params"])
                out.append(d)
            return out

    def alert_update(self, alert_id: int, **fields) -> bool:
        allowed = {"symbol", "type", "params", "one_time", "cooldown_min",
                   "expires_at", "hysteresis_pct", "ntfy_priority", "paused",
                   "timeframe", "regime"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "params":
                v = json.dumps(v)
            if k == "symbol" and isinstance(v, str):
                v = v.upper()
            if k == "timeframe" and v not in ("1m", "5m", "15m", "1h"):
                continue
            if k == "regime" and v not in ("any", "bull", "bear"):
                continue
            if k in ("one_time", "paused"):
                v = int(bool(v))
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return False
        args.append(alert_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE alerts SET {', '.join(sets)} WHERE id=?", args)
            return cur.rowcount > 0

    def alert_delete(self, alert_id: int) -> bool:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM alert_state WHERE alert_id=?", (alert_id,))
            cur = self.conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
            return cur.rowcount > 0

    # ---- alert state ----------------------------------------------------
    def state_get(self, alert_id: int) -> dict:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM alert_state WHERE alert_id=?", (alert_id,)).fetchone()
            if row:
                return dict(row)
            self.conn.execute(
                """INSERT INTO alert_state(alert_id, state, updated_at)
                   VALUES(?, 'ARMED', ?)""", (alert_id, _utcnow()))
            self.conn.commit()
            return {"alert_id": alert_id, "state": "ARMED",
                    "last_trigger_ts": None, "cooldown_until": None,
                    "rearm_ref": None, "last_bar_ts": None,
                    "updated_at": _utcnow()}

    def state_set(self, alert_id: int, **fields) -> None:
        allowed = {"state", "last_trigger_ts", "cooldown_until",
                   "rearm_ref", "last_bar_ts"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        args = [fields[k] for k in fields if k in allowed]
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(_utcnow())
        args.append(alert_id)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE alert_state SET {', '.join(sets)} WHERE alert_id=?", args)

    # ---- events ---------------------------------------------------------
    def event_record(self, alert_id: int, symbol: str, price: float,
                     detail: str = "", trigger_ts: float | None = None) -> int:
        """Record a fire. ``trigger_ts`` is the tick's epoch seconds — the
        event is stamped with the trigger time (not wall-clock), so the
        per-minute dedupe key is deterministic."""
        ts = (datetime.fromtimestamp(trigger_ts, tz=timezone.utc).isoformat()
              if trigger_ts is not None else _utcnow())
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO alert_events(alert_id, ts, symbol, price, detail)
                   VALUES(?, ?, ?, ?, ?)""",
                (alert_id, ts, symbol.upper(), price, detail))
            return cur.lastrowid

    def event_exists_for_minute(self, alert_id: int, minute_ts: int) -> bool:
        """Dedupe guard: did this alert already fire for this trigger minute?"""
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 FROM alert_events
                   WHERE alert_id=? AND substr(ts, 1, 16)=substr(?, 1, 16)
                   LIMIT 1""",
                (alert_id,
                 datetime.fromtimestamp(minute_ts, tz=timezone.utc).isoformat()),
            ).fetchone()
            return row is not None

    def events_for_alert(self, alert_id: int, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM alert_events WHERE alert_id=? ORDER BY id DESC LIMIT ?",
                (alert_id, limit))]

    # ---- candle cache ---------------------------------------------------
    def candles_save(self, symbol: str, tf: str, bars: list[dict]) -> None:
        with self._lock, self.conn:
            self.conn.executemany(
                """INSERT OR REPLACE INTO candles(symbol, tf, ts, o, h, l, c, v)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                [(symbol.upper(), tf, b["ts"], b["o"], b["h"], b["l"], b["c"],
                  b.get("v", 0)) for b in bars],
            )

    def candles_load(self, symbol: str, tf: str = "1m", limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT ts, o, h, l, c, v FROM candles
                   WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?""",
                (symbol.upper(), tf, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ---- fundamentals cache (24h TTL, enforced by callers) ----------------
    def fund_cache_get(self, symbol: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT data_json, fetched_at FROM fundamentals_cache WHERE symbol=?",
                (symbol.upper(),)).fetchone()
            if not row:
                return None
            return {"data": json.loads(row["data_json"]),
                    "fetched_at": row["fetched_at"]}

    def fund_cache_set(self, symbol: str, data: dict) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO fundamentals_cache(symbol, data_json, fetched_at)
                   VALUES(?, ?, ?)""",
                (symbol.upper(), json.dumps(data), _utcnow()))

    # ---- event-poller state ------------------------------------------------
    def poller_get(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM poller_state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def poller_set(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO poller_state(key, value, updated_at)
                   VALUES(?, ?, ?)""", (key, value, _utcnow()))

    # ---- market calendar ---------------------------------------------------
    def calendar_save(self, days: list[dict]) -> None:
        """days: [{date: 'YYYY-MM-DD', is_open: bool, open: 'HH:MM', close: 'HH:MM'}]"""
        with self._lock, self.conn:
            self.conn.executemany(
                """INSERT OR REPLACE INTO market_calendar(date, is_open, open_time, close_time)
                   VALUES(?, ?, ?, ?)""",
                [(d["date"], int(bool(d["is_open"])), d.get("open"), d.get("close"))
                 for d in days])

    def calendar_is_open(self, date: str) -> bool | None:
        """True/False for a YYYY-MM-DD date, None if unknown (not cached)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT is_open FROM market_calendar WHERE date=?", (date,)).fetchone()
            return None if row is None else bool(row["is_open"])

    def close(self) -> None:
        with self._lock:
            self.conn.close()
