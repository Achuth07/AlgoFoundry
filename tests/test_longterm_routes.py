"""Long-Term dashboard route tests (ALG-9).

Uses FastAPI's TestClient with HTTP Basic auth. Creds default to
``admin`` / ``change-me-now`` (read from env at import time in ``app.main``);
we set them explicitly so the test is stable. The temp DB is bound via
``ALGOFOUNDRY_DB`` before ``app.main`` is imported/reloaded.
"""

from __future__ import annotations

import base64
import datetime as _dt
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "routes.db"
    monkeypatch.setenv("ALGOFOUNDRY_DB", str(db_file))
    monkeypatch.setenv("ALGOFOUNDRY_USER", "admin")
    monkeypatch.setenv("ALGOFOUNDRY_PASSWORD", "change-me-now")

    import app.db as db_mod
    db_mod = importlib.reload(db_mod)
    db_mod.init_db()

    import app.main as main_mod
    main_mod = importlib.reload(main_mod)

    tc = TestClient(main_mod.app)
    return tc, db_mod, main_mod


def _auth() -> dict:
    token = base64.b64encode(b"admin:change-me-now").decode()
    return {"Authorization": f"Basic {token}"}


def _seed(db_mod):
    today = _dt.date.today().isoformat()
    db_mod.upsert_holdings_snapshot(
        date=today, t212_ticker="AAPL_US_EQ", symbol="AAPL", qty=10,
        avg_price=150.0, current_price=175.0, pnl=250.0, currency="USD",
    )
    db_mod.upsert_verdict(
        date=today, symbol="AAPL", composite=0.9, label="BUY", confidence=0.45,
        score_technical=1.0, score_fundamental=0.5, data_quality="partial_data",
        review_flags="", rationale="Verdict: BUY. Strong trend.",
    )
    return today


# ---- auth ------------------------------------------------------------------
def test_longterm_requires_auth(client):
    tc, _db, _main = client
    assert tc.get("/longterm").status_code == 401


# ---- /longterm -------------------------------------------------------------
def test_longterm_renders_rows(client):
    tc, db_mod, _main = client
    _seed(db_mod)
    resp = tc.get("/longterm", headers=_auth())
    assert resp.status_code == 200
    body = resp.text
    assert "AAPL" in body
    assert "BUY" in body
    assert "partial" in body  # data_quality badge


def test_longterm_empty_state(client):
    tc, _db, _main = client
    resp = tc.get("/longterm", headers=_auth())
    assert resp.status_code == 200
    assert "No long-term holdings" in resp.text


# ---- /longterm/history -----------------------------------------------------
def test_longterm_history_renders(client):
    tc, db_mod, _main = client
    _seed(db_mod)
    resp = tc.get("/longterm/history?symbol=AAPL", headers=_auth())
    assert resp.status_code == 200
    assert "AAPL" in resp.text
    assert "BUY" in resp.text


# ---- POST /longterm/run ----------------------------------------------------
def test_longterm_run_returns_immediately(client, monkeypatch):
    tc, _db, main_mod = client
    called = {"n": 0}

    import app.longterm.scheduler as sched
    monkeypatch.setattr(
        sched, "run_daily_pipeline",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    resp = tc.post("/longterm/run", headers=_auth())
    assert resp.status_code == 200
    assert "Run started" in resp.text


# ---- POST /longterm/analyze (ALG-14) ---------------------------------------
def _mock_adhoc_legs(monkeypatch, *, etf=False):
    """Wire the scheduler's ad-hoc legs to deterministic, network-free results."""
    from app.longterm.data_sources import LegResult
    import app.longterm.scheduler as sched
    import app.longterm.technicals as tech
    import app.longterm.fundamentals as fund

    monkeypatch.setattr(sched.data_sources, "fetch_ohlcv", lambda *a, **k: object())
    monkeypatch.setattr(
        tech, "technical_score",
        lambda df: LegResult(status="ok", score=1.0, summary={}),
    )
    monkeypatch.setattr(fund, "fetch_fundamentals", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(
        fund, "fundamental_score",
        lambda m, t: LegResult(status="not_applicable") if t == "etf"
        else LegResult(status="ok", score=0.5, summary={}),
    )
    monkeypatch.setattr(sched.data_sources, "fetch_analyst", lambda *a, **k: None)
    monkeypatch.setattr(
        sched.data_sources, "analyst_score",
        lambda *a, **k: LegResult(status="not_applicable"),
    )
    monkeypatch.setattr(
        sched.data_sources, "fetch_news",
        lambda *a, **k: LegResult(status="not_applicable"),
    )
    monkeypatch.setattr(sched.data_sources, "fetch_earnings_calendar",
                        lambda *a, **k: None)

    # quoteType detection (no network)
    qtype = "ETF" if etf else "EQUITY"

    class _FakeTicker:
        def __init__(self, sym):
            self.info = {"quoteType": qtype}

    class _FakeYf:
        Ticker = _FakeTicker

    monkeypatch.setattr(fund, "yf", _FakeYf)


def test_analyze_empty_symbol_friendly_400(client):
    tc, _db, _main = client
    resp = tc.post("/longterm/analyze", headers=_auth(), data={"symbol": "  "})
    assert resp.status_code == 400
    assert "symbol" in resp.text.lower()


def test_analyze_renders_verdict_fragment(client, monkeypatch):
    tc, _db, _main = client
    _mock_adhoc_legs(monkeypatch)
    resp = tc.post("/longterm/analyze", headers=_auth(), data={"symbol": "aapl"})
    assert resp.status_code == 200
    body = resp.text
    assert "AAPL" in body
    assert "not saved to history" in body
    assert "technical" in body


def test_analyze_etf_still_renders(client, monkeypatch):
    tc, _db, _main = client
    _mock_adhoc_legs(monkeypatch, etf=True)
    resp = tc.post("/longterm/analyze", headers=_auth(), data={"symbol": "VUAG.L"})
    assert resp.status_code == 200
    assert "VUAG.L" in resp.text
    assert "n/a" in resp.text  # fundamental / analyst not-applicable chip


def test_analyze_default_writes_nothing(client, monkeypatch):
    tc, db_mod, _main = client
    _mock_adhoc_legs(monkeypatch)
    tc.post("/longterm/analyze", headers=_auth(), data={"symbol": "AAPL"})
    assert db_mod.get_verdicts_for_symbol("AAPL") == []


def test_analyze_save_writes_adhoc_row(client, monkeypatch):
    tc, db_mod, _main = client
    _mock_adhoc_legs(monkeypatch)
    resp = tc.post("/longterm/analyze", headers=_auth(),
                   data={"symbol": "AAPL", "save": "on"})
    assert resp.status_code == 200
    assert "saved (adhoc)" in resp.text
    rows = db_mod.get_verdicts_for_symbol("AAPL")
    assert len(rows) == 1
    assert "adhoc" in (rows[0]["review_flags"] or "")


def test_analyze_unknown_symbol_no_data_fragment(client, monkeypatch):
    tc, db_mod, _main = client
    import app.longterm.scheduler as sched
    monkeypatch.setattr(sched.data_sources, "fetch_ohlcv", lambda *a, **k: None)
    resp = tc.post("/longterm/analyze", headers=_auth(),
                   data={"symbol": "NOSUCHXYZ"})
    assert resp.status_code == 200  # never 5xx
    assert "NO_DATA" in resp.text or "no_data" in resp.text
    assert db_mod.get_verdicts_for_symbol("NOSUCHXYZ") == []


# ---- settings post ---------------------------------------------------------
def test_longterm_settings_updates_key(client):
    tc, db_mod, _main = client
    resp = tc.post(
        "/longterm/settings",
        headers=_auth(),
        data={
            "lt_t212_api_key": "new-t212-key",
            "lt_t212_api_secret": "new-t212-secret",
            "lt_t212_env": "live",
            "lt_finnhub_key": "fh-key",
            "lt_schedule_time": "09:15",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db_mod.get_setting("lt_t212_api_key") == "new-t212-key"
    assert db_mod.get_setting("lt_t212_api_secret") == "new-t212-secret"
    assert db_mod.get_setting("lt_t212_env") == "live"
    assert db_mod.get_setting("lt_schedule_time") == "09:15"
