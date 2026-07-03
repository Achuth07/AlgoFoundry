"""Data-source layer tests (ALG-2 price fetch + ALG-3 Finnhub legs).

No real network: yfinance download and the ``_finnhub_get`` HTTP seam are both
monkeypatched. The temp-DB ``db`` fixture (conftest) backs settings + event log.
"""

from __future__ import annotations

import datetime as _dt
import importlib

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def ds(db):
    """Reload data_sources bound to the temp-DB app.db."""
    import app.longterm.data_sources as mod

    mod = importlib.reload(mod)
    return mod


def _frame(n=60):
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    closes = 100 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
         "Close": closes, "Volume": np.full(n, 1e6)},
        index=idx,
    )


# ---- LegResult -----------------------------------------------------------
def test_legresult_defaults(ds):
    r = ds.LegResult(status="ok", score=1.0)
    assert r.summary == {} and r.detail == ""


# ---- fetch_ohlcv: retry + same-day cache ---------------------------------
def test_fetch_ohlcv_success_and_cache(ds, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", str(tmp_path / "cache"))
    calls = {"n": 0}

    def fake_dl(sym, period):
        calls["n"] += 1
        return _frame()

    monkeypatch.setattr(ds, "_download_ohlcv", fake_dl)

    df1 = ds.fetch_ohlcv("AAPL")
    assert df1 is not None and not df1.empty
    assert calls["n"] == 1
    # Second call same day -> served from disk cache, no new download.
    df2 = ds.fetch_ohlcv("AAPL")
    assert df2 is not None
    assert calls["n"] == 1


def test_fetch_ohlcv_retries_then_gives_up(ds, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(ds, "time", type("T", (), {
        "sleep": staticmethod(lambda *_: None),
        "monotonic": staticmethod(lambda: 0.0),
    }))
    calls = {"n": 0}

    def boom(sym, period):
        calls["n"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(ds, "_download_ohlcv", boom)
    assert ds.fetch_ohlcv("AAPL", retries=3) is None
    assert calls["n"] == 3


def test_fetch_ohlcv_empty_symbol(ds):
    assert ds.fetch_ohlcv("") is None


# ---- Analyst leg ---------------------------------------------------------
def test_analyst_not_applicable_for_etf(ds):
    r = ds.analyst_score({}, finnhub_symbol="SPY", instrument_type="etf")
    assert r.status == "not_applicable"


def test_analyst_not_applicable_without_symbol(ds):
    r = ds.analyst_score({}, finnhub_symbol=None, instrument_type="equity")
    assert r.status == "not_applicable"


def test_analyst_bullish_scores_positive(ds):
    payload = {
        "recommendations": [
            {"strongBuy": 10, "buy": 8, "hold": 2, "sell": 0, "strongSell": 0,
             "period": "2024-02-01"},
            {"strongBuy": 6, "buy": 6, "hold": 4, "sell": 2, "strongSell": 0,
             "period": "2024-01-01"},
        ],
        "price_target": {"targetMean": 130.0},
    }
    r = ds.analyst_score(payload, finnhub_symbol="AAPL",
                         instrument_type="equity", current_price=100.0)
    assert r.status == "ok"
    assert r.score > 0
    assert r.summary["target_upside_pct"] == pytest.approx(30.0)
    assert r.summary["net_upgrade_delta"] > 0


def test_analyst_bearish_scores_negative(ds):
    payload = {
        "recommendations": [
            {"strongBuy": 0, "buy": 1, "hold": 3, "sell": 6, "strongSell": 4,
             "period": "2024-02-01"},
            {"strongBuy": 2, "buy": 3, "hold": 3, "sell": 2, "strongSell": 1,
             "period": "2024-01-01"},
        ],
        "price_target": {"targetMean": 80.0},
    }
    r = ds.analyst_score(payload, finnhub_symbol="XYZ",
                         instrument_type="equity", current_price=100.0)
    assert r.status == "ok"
    assert r.score < 0


def test_analyst_no_data(ds):
    r = ds.analyst_score(None, finnhub_symbol="AAPL", instrument_type="equity")
    assert r.status == "no_data"


def test_fetch_analyst_uses_seam(ds, monkeypatch):
    def fake_get(path, params=None):
        if "recommendation" in path:
            return [{"strongBuy": 5, "buy": 5, "hold": 1, "sell": 0,
                     "strongSell": 0, "period": "2024-02-01"}]
        return {"targetMean": 200.0}

    monkeypatch.setattr(ds, "_finnhub_get", fake_get)
    out = ds.fetch_analyst("AAPL")
    assert out["price_target"]["targetMean"] == 200.0
    assert out["recommendations"][0]["strongBuy"] == 5


# ---- News leg ------------------------------------------------------------
def test_news_not_applicable_for_etf(ds):
    r = ds.fetch_news("SPY", instrument_type="etf")
    assert r.status == "not_applicable"


def test_news_ok(ds, monkeypatch):
    def fake_get(path, params=None):
        return [
            {"headline": "Big news", "source": "Reuters",
             "datetime": 1700000000, "url": "http://x"},
            {"headline": "More news", "source": "WSJ",
             "datetime": 1700000100, "url": "http://y"},
        ]

    monkeypatch.setattr(ds, "_finnhub_get", fake_get)
    r = ds.fetch_news("AAPL", instrument_type="equity")
    assert r.status == "ok"
    assert r.summary["count"] == 2
    assert r.summary["headlines"][0]["source"] == "Reuters"


def test_news_no_data(ds, monkeypatch):
    monkeypatch.setattr(ds, "_finnhub_get", lambda *a, **k: None)
    r = ds.fetch_news("AAPL", instrument_type="equity")
    assert r.status == "no_data"


# ---- Earnings calendar ---------------------------------------------------
def test_earnings_calendar_next_date(ds, monkeypatch):
    soon = (_dt.date.today() + _dt.timedelta(days=5)).isoformat()
    later = (_dt.date.today() + _dt.timedelta(days=10)).isoformat()

    def fake_get(path, params=None):
        return {"earningsCalendar": [{"date": later}, {"date": soon}]}

    monkeypatch.setattr(ds, "_finnhub_get", fake_get)
    d = ds.fetch_earnings_calendar("AAPL", instrument_type="equity")
    assert d == _dt.date.fromisoformat(soon)


def test_earnings_calendar_etf_none(ds):
    assert ds.fetch_earnings_calendar("SPY", instrument_type="etf") is None


def test_earnings_calendar_empty(ds, monkeypatch):
    monkeypatch.setattr(ds, "_finnhub_get", lambda *a, **k: {"earningsCalendar": []})
    assert ds.fetch_earnings_calendar("AAPL", instrument_type="equity") is None


# ---- _finnhub_get seam ---------------------------------------------------
def test_finnhub_get_no_key_returns_none(ds):
    # Default seeded key is empty -> no HTTP attempted.
    assert ds._finnhub_get("/stock/recommendation", {"symbol": "AAPL"}) is None


def test_finnhub_get_never_logs_key(ds, db, monkeypatch):
    db.set_setting("lt_finnhub_key", "SECRET_TOKEN_123")

    class FakeResp:
        status_code = 500
        def json(self):
            return {}

    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: FakeResp())
    ds._finnhub_get("/stock/recommendation", {"symbol": "AAPL"})
    events = db.recent_events(20)
    assert all("SECRET_TOKEN_123" not in (e.get("detail") or "") for e in events)
