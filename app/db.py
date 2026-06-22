"""SQLite persistence: settings (key-value) + event log.

Everything the GUI lets you change is stored here so it survives restarts.
The DB is intentionally tiny and dependency-free (stdlib sqlite3 only).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any

_DB_PATH = os.environ.get("ALGOFOUNDRY_DB", "algofoundry.db")
_lock = threading.Lock()

# ---- Default settings ------------------------------------------------------
# Ports: IB Gateway paper = 4002, live = 4001. TWS paper = 7497, live = 7496.
DEFAULTS: dict[str, Any] = {
    "ibkr_host": "127.0.0.1",
    "ibkr_port": 4002,            # paper Gateway by default
    "ibkr_client_id": 1,
    "ibkr_account": "",           # optional; required if the login has multiple accounts
    "mode_label": "paper",        # informational badge only ("paper"/"live")
    "trading_enabled": False,     # global kill switch — starts OFF for safety
    "sizing_mode": "fixed_dollars",  # fixed_shares | fixed_dollars | percent_equity
    "sizing_value": 1000.0,       # shares, dollars, or percent depending on mode
    "order_type": "market",       # market | limit
    "limit_offset_pct": 0.1,      # for limit orders: % through the market for fill
    "max_positions": 5,           # don't open more than this many distinct symbols
    "max_position_value": 10000.0,  # hard cap on $ per single position
    "allow_buy": True,
    "allow_sell": True,
    "webhook_secret": "",         # set on first run if empty
}

_CASTS = {
    "ibkr_port": int,
    "ibkr_client_id": int,
    "trading_enabled": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "allow_buy": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "allow_sell": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    "sizing_value": float,
    "limit_offset_pct": float,
    "max_positions": int,
    "max_position_value": float,
}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL NOT NULL,
                   kind TEXT NOT NULL,        -- webhook | order | error | info
                   action TEXT,               -- buy | sell | flatten | ...
                   symbol TEXT,
                   status TEXT,               -- accepted | rejected | filled | ...
                   detail TEXT
               )"""
        )
        # Seed any missing defaults.
        existing = {r["key"] for r in conn.execute("SELECT key FROM settings")}
        for key, val in DEFAULTS.items():
            if key not in existing:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (key, json.dumps(val)),
                )
        # Generate a webhook secret if none configured.
        env_secret = os.environ.get("ALGOFOUNDRY_WEBHOOK_SECRET", "").strip()
        cur = conn.execute("SELECT value FROM settings WHERE key='webhook_secret'")
        row = cur.fetchone()
        current = json.loads(row["value"]) if row else ""
        if not current:
            new_secret = env_secret or secrets.token_urlsafe(24)
            conn.execute(
                "UPDATE settings SET value=? WHERE key='webhook_secret'",
                (json.dumps(new_secret),),
            )


def get_all_settings() -> dict[str, Any]:
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULTS)
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            out[r["key"]] = r["value"]
    return out


def get_setting(key: str, default: Any = None) -> Any:
    return get_all_settings().get(key, default)


def set_setting(key: str, value: Any) -> Any:
    if key in _CASTS:
        value = _CASTS[key](value)
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
    return value


def update_settings(items: dict[str, Any]) -> None:
    for key, value in items.items():
        if key in DEFAULTS:
            set_setting(key, value)


def log_event(
    kind: str,
    *,
    action: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    detail: str | None = None,
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO events (ts, kind, action, symbol, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), kind, action, symbol, status, detail),
        )


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
