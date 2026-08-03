"""Database persistence: settings (key-value) + event log.

Everything the GUI lets you change is stored here so it survives restarts.

Supports two backends:
  * SQLite  — local dev (default when DATABASE_URL is not set)
  * PostgreSQL — Heroku / production (set DATABASE_URL env var)
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_USE_POSTGRES = _DATABASE_URL.startswith("postgres")
_lock = threading.Lock()

if _USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Heroku uses postgres:// but psycopg2 needs postgresql://
    if _DATABASE_URL.startswith("postgres://"):
        _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Fallback for SQLite
_DB_PATH = os.environ.get("ALGOFOUNDRY_DB", "algofoundry.db")

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
    "signup_code": "",            # registration code required to create accounts
    # ---- Long-Term Portfolio Tracker (Trading 212) — lt_ prefixed keys ----
    "lt_t212_api_key": "",
    "lt_t212_api_secret": "",         # paired with lt_t212_api_key (Basic auth)
    "lt_t212_env": "demo",            # demo | live
    "lt_finnhub_key": "",
    "lt_alpha_vantage_key": "",
    "lt_massive_key": "",             # Massive (formerly Polygon.io) API key
    "lt_polygon_key": "",             # legacy alias, still honoured
    "lt_callmebot_phone": "",
    "lt_callmebot_key": "",
    "lt_ai_provider": "openrouter",     # openrouter | groq | gemini
    "lt_groq_api_key": "",
    "lt_gemini_api_key": "",
    "lt_openrouter_model": "",
    "lt_openrouter_fallback": "",
    "lt_weight_technical": 1.0,
    "lt_weight_fundamental": 1.0,
    "lt_weight_analyst": 1.0,
    "lt_weight_news": 1.0,
    "lt_threshold_buy": 0.5,
    "lt_threshold_sell": 0.75,
    "lt_hysteresis_days": 2,
    "lt_hysteresis_margin": 0.15,
    "lt_earnings_freeze_days": 3,
    "lt_max_drawdown_pct": 25.0,
    "lt_schedule_time": "17:30",
    "lt_schedule_tz": "America/New_York",
    "lt_last_run_date": "",
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
    # ---- Long-Term Portfolio Tracker casts ----
    "lt_weight_technical": float,
    "lt_weight_fundamental": float,
    "lt_weight_analyst": float,
    "lt_weight_news": float,
    "lt_threshold_buy": float,
    "lt_threshold_sell": float,
    "lt_hysteresis_days": int,
    "lt_hysteresis_margin": float,
    "lt_earnings_freeze_days": int,
    "lt_max_drawdown_pct": float,
}


# ---- Connection helpers ----------------------------------------------------

def _conn_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _conn_pg():
    conn = psycopg2.connect(_DATABASE_URL)
    conn.autocommit = False
    return conn


def _pg_fetchall(cursor) -> list[dict[str, Any]]:
    """Convert psycopg2 cursor results to list of dicts."""
    if cursor.description is None:
        return []
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _pg_fetchone(cursor) -> dict[str, Any] | None:
    if cursor.description is None:
        return None
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    return dict(zip(cols, row)) if row else None


# ---- Schema init -----------------------------------------------------------

def init_db() -> None:
    if _USE_POSTGRES:
        _init_db_pg()
    else:
        _init_db_sqlite()


def _init_db_sqlite() -> None:
    with _lock, _conn_sqlite() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL NOT NULL,
                   kind TEXT NOT NULL,
                   action TEXT,
                   symbol TEXT,
                   status TEXT,
                   detail TEXT
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS longterm_holdings_snapshot (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   date TEXT NOT NULL,
                   t212_ticker TEXT NOT NULL,
                   symbol TEXT,
                   qty REAL,
                   avg_price REAL,
                   current_price REAL,
                   pnl REAL,
                   currency TEXT,
                   created_ts REAL,
                   UNIQUE(date, t212_ticker)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS longterm_verdicts (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   date TEXT NOT NULL,
                   symbol TEXT NOT NULL,
                   score_technical REAL,
                   score_fundamental REAL,
                   score_analyst REAL,
                   score_news REAL,
                   composite REAL,
                   label TEXT,
                   confidence REAL,
                   rationale TEXT,
                   override_flag INTEGER DEFAULT 0,
                   review_flags TEXT,
                   data_quality TEXT,
                   price_at_verdict REAL,
                   model_used TEXT,
                   prompt_version TEXT,
                   raw_ai_response TEXT,
                   fwd_return_7d REAL,
                   fwd_return_30d REAL,
                   fwd_return_90d REAL,
                   created_ts REAL,
                   UNIQUE(date, symbol)
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS longterm_instruments (
                   t212_ticker TEXT PRIMARY KEY,
                   yf_symbol TEXT,
                   finnhub_symbol TEXT,
                   currency TEXT,
                   exchange TEXT,
                   instrument_type TEXT,
                   manual_override INTEGER DEFAULT 0,
                   updated_ts REAL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS longterm_fundamentals_cache (
                   symbol TEXT PRIMARY KEY,
                   payload TEXT,
                   fetched_ts REAL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   username TEXT UNIQUE NOT NULL,
                   pw_hash TEXT NOT NULL,
                   is_admin INTEGER DEFAULT 0,
                   created_ts REAL,
                   last_login_ts REAL
               )"""
        )
        # Migration: add is_admin to a pre-existing users table.
        _cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in _cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   token TEXT PRIMARY KEY,
                   username TEXT NOT NULL,
                   created_ts REAL NOT NULL,
                   expires_ts REAL NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS portfolio_history (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   broker TEXT NOT NULL,
                   date TEXT NOT NULL,
                   ts REAL,
                   invested REAL,
                   market_value REAL,
                   pnl REAL,
                   currency TEXT DEFAULT 'GBP',
                   UNIQUE(broker, date)
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


def _init_db_pg() -> None:
    with _lock:
        conn = _conn_pg()
        try:
            cur = conn.cursor()
            # Serialize schema creation across processes. With multiple uvicorn
            # workers each process calls init_db() concurrently, and Postgres'
            # "CREATE TABLE IF NOT EXISTS" is not catalog-safe under concurrency
            # (SERIAL columns race on pg_type). A transaction-level advisory lock
            # makes exactly one worker build the schema; it auto-releases on
            # commit/rollback so the others then see the tables already present.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (72890123,))
            cur.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS events (
                       id SERIAL PRIMARY KEY,
                       ts DOUBLE PRECISION NOT NULL,
                       kind TEXT NOT NULL,
                       action TEXT,
                       symbol TEXT,
                       status TEXT,
                       detail TEXT
                   )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS longterm_holdings_snapshot (
                       id SERIAL PRIMARY KEY,
                       date TEXT NOT NULL,
                       t212_ticker TEXT NOT NULL,
                       symbol TEXT,
                       qty DOUBLE PRECISION,
                       avg_price DOUBLE PRECISION,
                       current_price DOUBLE PRECISION,
                       pnl DOUBLE PRECISION,
                       currency TEXT,
                       created_ts DOUBLE PRECISION,
                       UNIQUE(date, t212_ticker)
                   )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS longterm_verdicts (
                       id SERIAL PRIMARY KEY,
                       date TEXT NOT NULL,
                       symbol TEXT NOT NULL,
                       score_technical DOUBLE PRECISION,
                       score_fundamental DOUBLE PRECISION,
                       score_analyst DOUBLE PRECISION,
                       score_news DOUBLE PRECISION,
                       composite DOUBLE PRECISION,
                       label TEXT,
                       confidence DOUBLE PRECISION,
                       rationale TEXT,
                       override_flag INTEGER DEFAULT 0,
                       review_flags TEXT,
                       data_quality TEXT,
                       price_at_verdict DOUBLE PRECISION,
                       model_used TEXT,
                       prompt_version TEXT,
                       raw_ai_response TEXT,
                       fwd_return_7d DOUBLE PRECISION,
                       fwd_return_30d DOUBLE PRECISION,
                       fwd_return_90d DOUBLE PRECISION,
                       created_ts DOUBLE PRECISION,
                       UNIQUE(date, symbol)
                   )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS longterm_instruments (
                       t212_ticker TEXT PRIMARY KEY,
                       yf_symbol TEXT,
                       finnhub_symbol TEXT,
                       currency TEXT,
                       exchange TEXT,
                       instrument_type TEXT,
                       manual_override INTEGER DEFAULT 0,
                       updated_ts DOUBLE PRECISION
                   )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS longterm_fundamentals_cache (
                       symbol TEXT PRIMARY KEY,
                       payload TEXT,
                       fetched_ts DOUBLE PRECISION
                   )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS users (
                       id SERIAL PRIMARY KEY,
                       username TEXT UNIQUE NOT NULL,
                       pw_hash TEXT NOT NULL,
                       is_admin INTEGER DEFAULT 0,
                       created_ts DOUBLE PRECISION,
                       last_login_ts DOUBLE PRECISION
                   )"""
            )
            cur.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0"
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                       token TEXT PRIMARY KEY,
                       username TEXT NOT NULL,
                       created_ts DOUBLE PRECISION NOT NULL,
                       expires_ts DOUBLE PRECISION NOT NULL
                   )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS portfolio_history (
                       id SERIAL PRIMARY KEY,
                       broker TEXT NOT NULL,
                       date TEXT NOT NULL,
                       ts DOUBLE PRECISION,
                       invested DOUBLE PRECISION,
                       market_value DOUBLE PRECISION,
                       pnl DOUBLE PRECISION,
                       currency TEXT DEFAULT 'GBP',
                       UNIQUE(broker, date)
                   )"""
            )
            # Seed any missing defaults.
            cur.execute("SELECT key FROM settings")
            existing = {r[0] for r in cur.fetchall()}
            for key, val in DEFAULTS.items():
                if key not in existing:
                    cur.execute(
                        "INSERT INTO settings (key, value) VALUES (%s, %s)",
                        (key, json.dumps(val)),
                    )
            # Generate a webhook secret if none configured.
            env_secret = os.environ.get("ALGOFOUNDRY_WEBHOOK_SECRET", "").strip()
            cur.execute("SELECT value FROM settings WHERE key='webhook_secret'")
            row = cur.fetchone()
            current = json.loads(row[0]) if row else ""
            if not current:
                new_secret = env_secret or secrets.token_urlsafe(24)
                cur.execute(
                    "UPDATE settings SET value=%s WHERE key='webhook_secret'",
                    (json.dumps(new_secret),),
                )
            conn.commit()
        finally:
            conn.close()


# ---- Settings CRUD ---------------------------------------------------------

def get_all_settings() -> dict[str, Any]:
    out = dict(DEFAULTS)
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM settings")
                rows = cur.fetchall()
            finally:
                conn.close()
        for key, value in rows:
            try:
                out[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                out[key] = value
    else:
        with _lock, _conn_sqlite() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
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
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    (key, json.dumps(value)),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
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


# ---- Events ----------------------------------------------------------------

def log_event(
    kind: str,
    *,
    action: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    detail: str | None = None,
) -> None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO events (ts, kind, action, symbol, status, detail) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (time.time(), kind, action, symbol, status, detail),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, action, symbol, status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), kind, action, symbol, status, detail),
            )


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT %s", (limit,)
                )
                rows = _pg_fetchall(cur)
            finally:
                conn.close()
        return rows
    else:
        with _lock, _conn_sqlite() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ---- Long-Term Portfolio Tracker CRUD helpers ------------------------------

def upsert_holdings_snapshot(
    *,
    date: str,
    t212_ticker: str,
    symbol: str | None = None,
    qty: float | None = None,
    avg_price: float | None = None,
    current_price: float | None = None,
    pnl: float | None = None,
    currency: str | None = None,
) -> None:
    ts = time.time()
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO longterm_holdings_snapshot "
                    "(date, t212_ticker, symbol, qty, avg_price, current_price, pnl, "
                    " currency, created_ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT(date, t212_ticker) DO UPDATE SET "
                    "  symbol=EXCLUDED.symbol, qty=EXCLUDED.qty, "
                    "  avg_price=EXCLUDED.avg_price, current_price=EXCLUDED.current_price, "
                    "  pnl=EXCLUDED.pnl, currency=EXCLUDED.currency, "
                    "  created_ts=EXCLUDED.created_ts",
                    (date, t212_ticker, symbol, qty, avg_price, current_price, pnl,
                     currency, ts),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                "INSERT INTO longterm_holdings_snapshot "
                "(date, t212_ticker, symbol, qty, avg_price, current_price, pnl, "
                " currency, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date, t212_ticker) DO UPDATE SET "
                "  symbol=excluded.symbol, qty=excluded.qty, "
                "  avg_price=excluded.avg_price, current_price=excluded.current_price, "
                "  pnl=excluded.pnl, currency=excluded.currency, "
                "  created_ts=excluded.created_ts",
                (date, t212_ticker, symbol, qty, avg_price, current_price, pnl,
                 currency, ts),
            )


def get_holdings_snapshot(date: str) -> list[dict[str, Any]]:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM longterm_holdings_snapshot WHERE date=%s "
                    "ORDER BY t212_ticker", (date,),
                )
                rows = _pg_fetchall(cur)
            finally:
                conn.close()
        return rows
    else:
        with _lock, _conn_sqlite() as conn:
            rows = conn.execute(
                "SELECT * FROM longterm_holdings_snapshot WHERE date=? "
                "ORDER BY t212_ticker", (date,),
            ).fetchall()
        return [dict(r) for r in rows]


def snapshot_dates() -> list[str]:
    """Return all distinct holdings-snapshot dates, oldest first."""
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT DISTINCT date FROM longterm_holdings_snapshot ORDER BY date"
                )
                return [r[0] for r in cur.fetchall()]
            finally:
                conn.close()
    with _lock, _conn_sqlite() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM longterm_holdings_snapshot ORDER BY date"
        ).fetchall()
    return [r["date"] for r in rows]


def latest_snapshot_date() -> str | None:
    """Return the most recent date in the holdings snapshot table, or ``None``.

    Used by the long-term view to fall back to the last run when there's no
    snapshot for today.
    """
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT date FROM longterm_holdings_snapshot "
                    "ORDER BY date DESC LIMIT 1"
                )
                row = cur.fetchone()
            finally:
                conn.close()
        return row[0] if row else None
    else:
        with _lock, _conn_sqlite() as conn:
            row = conn.execute(
                "SELECT date FROM longterm_holdings_snapshot "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
        return row["date"] if row else None


def upsert_verdict(*, date: str, symbol: str, **fields: Any) -> None:
    allowed = {
        "score_technical", "score_fundamental", "score_analyst", "score_news",
        "composite", "label", "confidence", "rationale", "override_flag",
        "review_flags", "data_quality", "price_at_verdict", "model_used",
        "prompt_version", "raw_ai_response", "fwd_return_7d", "fwd_return_30d",
        "fwd_return_90d",
    }
    cols = ["date", "symbol"]
    vals: list[Any] = [date, symbol]
    for key in allowed:
        if key in fields:
            cols.append(key)
            vals.append(fields[key])
    cols.append("created_ts")
    vals.append(time.time())

    if _USE_POSTGRES:
        placeholders = ", ".join("%s" for _ in cols)
        updates = ", ".join(
            f"{c}=EXCLUDED.{c}" for c in cols if c not in ("date", "symbol")
        )
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO longterm_verdicts ({', '.join(cols)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT(date, symbol) DO UPDATE SET {updates}",
                    vals,
                )
                conn.commit()
            finally:
                conn.close()
    else:
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in cols if c not in ("date", "symbol")
        )
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                f"INSERT INTO longterm_verdicts ({', '.join(cols)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT(date, symbol) DO UPDATE SET {updates}",
                vals,
            )


def get_verdict(date: str, symbol: str) -> dict[str, Any] | None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM longterm_verdicts WHERE date=%s AND symbol=%s",
                    (date, symbol),
                )
                row = _pg_fetchone(cur)
            finally:
                conn.close()
        return row
    else:
        with _lock, _conn_sqlite() as conn:
            row = conn.execute(
                "SELECT * FROM longterm_verdicts WHERE date=? AND symbol=?",
                (date, symbol),
            ).fetchone()
        return dict(row) if row else None


def get_verdicts_for_symbol(symbol: str, limit: int = 50) -> list[dict[str, Any]]:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM longterm_verdicts WHERE symbol=%s "
                    "ORDER BY date DESC LIMIT %s", (symbol, limit),
                )
                rows = _pg_fetchall(cur)
            finally:
                conn.close()
        return rows
    else:
        with _lock, _conn_sqlite() as conn:
            rows = conn.execute(
                "SELECT * FROM longterm_verdicts WHERE symbol=? "
                "ORDER BY date DESC LIMIT ?", (symbol, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_verdicts_for_date(date: str) -> list[dict[str, Any]]:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM longterm_verdicts WHERE date=%s ORDER BY symbol",
                    (date,),
                )
                rows = _pg_fetchall(cur)
            finally:
                conn.close()
        return rows
    else:
        with _lock, _conn_sqlite() as conn:
            rows = conn.execute(
                "SELECT * FROM longterm_verdicts WHERE date=? ORDER BY symbol",
                (date,),
            ).fetchall()
        return [dict(r) for r in rows]


def recent_verdicts(limit: int = 50) -> list[dict[str, Any]]:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM longterm_verdicts ORDER BY date DESC, id DESC LIMIT %s",
                    (limit,),
                )
                rows = _pg_fetchall(cur)
            finally:
                conn.close()
        return rows
    else:
        with _lock, _conn_sqlite() as conn:
            rows = conn.execute(
                "SELECT * FROM longterm_verdicts ORDER BY date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_instrument(t212_ticker: str) -> dict[str, Any] | None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM longterm_instruments WHERE t212_ticker=%s",
                    (t212_ticker,),
                )
                row = _pg_fetchone(cur)
            finally:
                conn.close()
        return row
    else:
        with _lock, _conn_sqlite() as conn:
            row = conn.execute(
                "SELECT * FROM longterm_instruments WHERE t212_ticker=?",
                (t212_ticker,),
            ).fetchone()
        return dict(row) if row else None


def upsert_instrument(
    *,
    t212_ticker: str,
    yf_symbol: str | None = None,
    finnhub_symbol: str | None = None,
    currency: str | None = None,
    exchange: str | None = None,
    instrument_type: str | None = None,
    manual_override: int = 0,
) -> None:
    ts = time.time()
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT manual_override FROM longterm_instruments WHERE t212_ticker=%s",
                    (t212_ticker,),
                )
                existing = cur.fetchone()
                if existing is not None and existing[0] and not manual_override:
                    return
                cur.execute(
                    "INSERT INTO longterm_instruments "
                    "(t212_ticker, yf_symbol, finnhub_symbol, currency, exchange, "
                    " instrument_type, manual_override, updated_ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT(t212_ticker) DO UPDATE SET "
                    "  yf_symbol=EXCLUDED.yf_symbol, "
                    "  finnhub_symbol=EXCLUDED.finnhub_symbol, "
                    "  currency=EXCLUDED.currency, exchange=EXCLUDED.exchange, "
                    "  instrument_type=EXCLUDED.instrument_type, "
                    "  manual_override=EXCLUDED.manual_override, "
                    "  updated_ts=EXCLUDED.updated_ts",
                    (t212_ticker, yf_symbol, finnhub_symbol, currency, exchange,
                     instrument_type, int(manual_override), ts),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            existing = conn.execute(
                "SELECT manual_override FROM longterm_instruments WHERE t212_ticker=?",
                (t212_ticker,),
            ).fetchone()
            if (
                existing is not None
                and existing["manual_override"]
                and not manual_override
            ):
                return
            conn.execute(
                "INSERT INTO longterm_instruments "
                "(t212_ticker, yf_symbol, finnhub_symbol, currency, exchange, "
                " instrument_type, manual_override, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(t212_ticker) DO UPDATE SET "
                "  yf_symbol=excluded.yf_symbol, "
                "  finnhub_symbol=excluded.finnhub_symbol, "
                "  currency=excluded.currency, exchange=excluded.exchange, "
                "  instrument_type=excluded.instrument_type, "
                "  manual_override=excluded.manual_override, "
                "  updated_ts=excluded.updated_ts",
                (t212_ticker, yf_symbol, finnhub_symbol, currency, exchange,
                 instrument_type, int(manual_override), ts),
            )


def get_fundamentals_cache(symbol: str) -> dict[str, Any] | None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM longterm_fundamentals_cache WHERE symbol=%s",
                    (symbol,),
                )
                out = _pg_fetchone(cur)
            finally:
                conn.close()
        if not out:
            return None
        try:
            out["payload"] = json.loads(out["payload"]) if out["payload"] else None
        except (json.JSONDecodeError, TypeError):
            pass
        return out
    else:
        with _lock, _conn_sqlite() as conn:
            row = conn.execute(
                "SELECT * FROM longterm_fundamentals_cache WHERE symbol=?",
                (symbol,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["payload"] = json.loads(out["payload"]) if out["payload"] else None
        except (json.JSONDecodeError, TypeError):
            pass
        return out


def set_fundamentals_cache(symbol: str, payload: Any) -> None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO longterm_fundamentals_cache (symbol, payload, fetched_ts) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT(symbol) DO UPDATE SET "
                    "  payload=EXCLUDED.payload, fetched_ts=EXCLUDED.fetched_ts",
                    (symbol, json.dumps(payload), time.time()),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                "INSERT INTO longterm_fundamentals_cache (symbol, payload, fetched_ts) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "  payload=excluded.payload, fetched_ts=excluded.fetched_ts",
                (symbol, json.dumps(payload), time.time()),
            )


# ---- Users & sessions (GUI authentication) ---------------------------------

def count_users() -> int:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM users")
                n = cur.fetchone()[0]
            finally:
                conn.close()
        return int(n)
    with _lock, _conn_sqlite() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    return int(n)


def get_user(username: str) -> dict[str, Any] | None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("SELECT * FROM users WHERE username=%s", (username,))
                return _pg_fetchone(cur)
            finally:
                conn.close()
    with _lock, _conn_sqlite() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
    return dict(row) if row else None


def create_user(username: str, pw_hash: str, is_admin: bool = False) -> bool:
    """Insert a new user. Returns False if the username already exists."""
    ts = time.time()
    admin = 1 if is_admin else 0
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO users (username, pw_hash, is_admin, created_ts) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (username) DO NOTHING",
                    (username, pw_hash, admin, ts),
                )
                inserted = cur.rowcount > 0
                conn.commit()
            finally:
                conn.close()
        return inserted
    with _lock, _conn_sqlite() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO users (username, pw_hash, is_admin, created_ts) "
            "VALUES (?, ?, ?, ?)",
            (username, pw_hash, admin, ts),
        )
        return cur.rowcount > 0


def count_admins() -> int:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM users WHERE is_admin=1")
                n = cur.fetchone()[0]
            finally:
                conn.close()
        return int(n)
    with _lock, _conn_sqlite() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin=1"
        ).fetchone()["n"]
    return int(n)


def set_user_admin(username: str, is_admin: bool) -> None:
    admin = 1 if is_admin else 0
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET is_admin=%s WHERE username=%s",
                    (admin, username),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                "UPDATE users SET is_admin=? WHERE username=?", (admin, username)
            )


def earliest_username() -> str | None:
    """Return the username of the first-created account, or ``None``."""
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("SELECT username FROM users ORDER BY id ASC LIMIT 1")
                row = cur.fetchone()
            finally:
                conn.close()
        return row[0] if row else None
    with _lock, _conn_sqlite() as conn:
        row = conn.execute(
            "SELECT username FROM users ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return row["username"] if row else None


def touch_user_login(username: str) -> None:
    ts = time.time()
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET last_login_ts=%s WHERE username=%s",
                    (ts, username),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                "UPDATE users SET last_login_ts=? WHERE username=?", (ts, username)
            )


def create_session(token: str, username: str, expires_ts: float) -> None:
    ts = time.time()
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO sessions (token, username, created_ts, expires_ts) "
                    "VALUES (%s, %s, %s, %s)",
                    (token, username, ts, expires_ts),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                "INSERT INTO sessions (token, username, created_ts, expires_ts) "
                "VALUES (?, ?, ?, ?)",
                (token, username, ts, expires_ts),
            )


def get_session(token: str) -> dict[str, Any] | None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("SELECT * FROM sessions WHERE token=%s", (token,))
                return _pg_fetchone(cur)
            finally:
                conn.close()
    with _lock, _conn_sqlite() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token=?", (token,)
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def purge_expired_sessions() -> None:
    now = time.time()
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM sessions WHERE expires_ts < %s", (now,))
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_ts < ?", (now,))


# ---- Portfolio value history (per-broker growth chart) ---------------------

def record_portfolio_snapshot(
    broker: str,
    date: str,
    invested: float | None,
    market_value: float | None,
    pnl: float | None,
    currency: str = "GBP",
) -> None:
    """Upsert one broker's total portfolio value for ``date`` (one row/day).

    Amounts are stored in a single base currency (``currency``, GBP) so the
    growth series is comparable over time regardless of the display currency.
    """
    ts = time.time()
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO portfolio_history "
                    "(broker, date, ts, invested, market_value, pnl, currency) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT(broker, date) DO UPDATE SET "
                    "  ts=EXCLUDED.ts, invested=EXCLUDED.invested, "
                    "  market_value=EXCLUDED.market_value, pnl=EXCLUDED.pnl, "
                    "  currency=EXCLUDED.currency",
                    (broker, date, ts, invested, market_value, pnl, currency),
                )
                conn.commit()
            finally:
                conn.close()
    else:
        with _lock, _conn_sqlite() as conn:
            conn.execute(
                "INSERT INTO portfolio_history "
                "(broker, date, ts, invested, market_value, pnl, currency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(broker, date) DO UPDATE SET "
                "  ts=excluded.ts, invested=excluded.invested, "
                "  market_value=excluded.market_value, pnl=excluded.pnl, "
                "  currency=excluded.currency",
                (broker, date, ts, invested, market_value, pnl, currency),
            )


def get_portfolio_history(broker: str, limit: int = 365) -> list[dict[str, Any]]:
    """Return a broker's value history, oldest first, capped to ``limit`` days."""
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM portfolio_history WHERE broker=%s "
                    "ORDER BY date DESC LIMIT %s",
                    (broker, limit),
                )
                rows = _pg_fetchall(cur)
            finally:
                conn.close()
        return list(reversed(rows))
    with _lock, _conn_sqlite() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_history WHERE broker=? "
            "ORDER BY date DESC LIMIT ?",
            (broker, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def portfolio_history_dates(broker: str) -> set[str]:
    """Return the set of dates already recorded for ``broker`` (for backfill)."""
    if _USE_POSTGRES:
        with _lock:
            conn = _conn_pg()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT date FROM portfolio_history WHERE broker=%s", (broker,)
                )
                return {r[0] for r in cur.fetchall()}
            finally:
                conn.close()
    with _lock, _conn_sqlite() as conn:
        rows = conn.execute(
            "SELECT date FROM portfolio_history WHERE broker=?", (broker,)
        ).fetchall()
    return {r["date"] for r in rows}
