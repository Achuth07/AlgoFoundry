"""Schema + CRUD tests for the long-term tables (ALG-8)."""

from __future__ import annotations


def _table_names(db):
    with db._conn_sqlite() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    return {r["name"] for r in rows}


def test_longterm_tables_exist(db):
    tables = _table_names(db)
    for name in (
        "longterm_holdings_snapshot",
        "longterm_verdicts",
        "longterm_instruments",
        "longterm_fundamentals_cache",
    ):
        assert name in tables


def test_swing_tables_untouched(db):
    tables = _table_names(db)
    assert "settings" in tables
    assert "events" in tables


def test_lt_defaults_seeded(db):
    s = db.get_all_settings()
    assert s["lt_t212_env"] == "demo"
    assert s["lt_schedule_time"] == "17:30"
    assert s["lt_schedule_tz"] == "America/New_York"
    # Numeric casts round-trip as the right type.
    assert isinstance(s["lt_weight_technical"], float)
    assert s["lt_weight_technical"] == 1.0
    assert isinstance(s["lt_hysteresis_days"], int)
    assert s["lt_hysteresis_days"] == 2
    assert s["lt_threshold_buy"] == 0.5
    assert s["lt_threshold_sell"] == 0.75
    assert s["lt_max_drawdown_pct"] == 25.0


def test_swing_defaults_untouched(db):
    s = db.get_all_settings()
    # A representative sample of the swing-trading keys/values.
    assert s["ibkr_port"] == 4002
    assert s["mode_label"] == "paper"
    assert s["trading_enabled"] is False
    assert s["sizing_mode"] == "fixed_dollars"
    assert s["max_positions"] == 5
    # webhook_secret gets auto-generated, so just ensure it exists & non-empty.
    assert s["webhook_secret"]


def test_lt_cast_applied_on_set(db):
    db.set_setting("lt_weight_analyst", "2.5")
    assert db.get_setting("lt_weight_analyst") == 2.5
    db.set_setting("lt_hysteresis_days", "4")
    assert db.get_setting("lt_hysteresis_days") == 4


def test_holdings_snapshot_upsert_idempotent(db):
    db.upsert_holdings_snapshot(
        date="2026-07-02", t212_ticker="AAPL_US_EQ", symbol="AAPL",
        qty=10, avg_price=100.0, current_price=110.0, pnl=100.0, currency="USD",
    )
    db.upsert_holdings_snapshot(
        date="2026-07-02", t212_ticker="AAPL_US_EQ", symbol="AAPL",
        qty=12, avg_price=101.0, current_price=115.0, pnl=168.0, currency="USD",
    )
    rows = db.get_holdings_snapshot("2026-07-02")
    assert len(rows) == 1
    assert rows[0]["qty"] == 12
    assert rows[0]["current_price"] == 115.0


def test_verdict_upsert_idempotent_on_date_symbol(db):
    db.upsert_verdict(
        date="2026-07-02", symbol="AAPL", composite=0.6, label="BUY",
        rationale="first",
    )
    db.upsert_verdict(
        date="2026-07-02", symbol="AAPL", composite=0.8, label="HOLD",
        rationale="second",
    )
    rows = db.get_verdicts_for_date("2026-07-02")
    assert len(rows) == 1
    v = rows[0]
    assert v["composite"] == 0.8
    assert v["label"] == "HOLD"
    assert v["rationale"] == "second"


def test_verdict_queries(db):
    db.upsert_verdict(date="2026-07-01", symbol="AAPL", composite=0.5)
    db.upsert_verdict(date="2026-07-02", symbol="AAPL", composite=0.6)
    db.upsert_verdict(date="2026-07-02", symbol="MSFT", composite=0.7)

    aapl = db.get_verdicts_for_symbol("AAPL")
    assert len(aapl) == 2
    assert aapl[0]["date"] == "2026-07-02"  # most recent first

    one = db.get_verdict("2026-07-02", "MSFT")
    assert one is not None and one["composite"] == 0.7
    assert db.get_verdict("2026-07-02", "NOPE") is None

    recent = db.recent_verdicts(limit=2)
    assert len(recent) == 2


def test_instrument_upsert_and_manual_protection(db):
    db.upsert_instrument(
        t212_ticker="AAPL_US_EQ", yf_symbol="AAPL", finnhub_symbol="AAPL",
        currency="USD", exchange="US", instrument_type="equity",
        manual_override=0,
    )
    got = db.get_instrument("AAPL_US_EQ")
    assert got["yf_symbol"] == "AAPL"

    # Set a manual mapping...
    db.upsert_instrument(
        t212_ticker="AAPL_US_EQ", yf_symbol="AAPL.CUSTOM",
        instrument_type="equity", manual_override=1,
    )
    # ...an auto call must NOT clobber it.
    db.upsert_instrument(
        t212_ticker="AAPL_US_EQ", yf_symbol="AAPL", manual_override=0,
    )
    got = db.get_instrument("AAPL_US_EQ")
    assert got["yf_symbol"] == "AAPL.CUSTOM"
    assert got["manual_override"] == 1


def test_fundamentals_cache_roundtrip(db):
    assert db.get_fundamentals_cache("AAPL") is None
    db.set_fundamentals_cache("AAPL", {"pe": 30, "sector": "Tech"})
    row = db.get_fundamentals_cache("AAPL")
    assert row["payload"] == {"pe": 30, "sector": "Tech"}
    assert row["fetched_ts"] > 0
    # Upsert overwrites.
    db.set_fundamentals_cache("AAPL", {"pe": 31})
    assert db.get_fundamentals_cache("AAPL")["payload"] == {"pe": 31}
