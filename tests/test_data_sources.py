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


# ---- fetch_ohlcv: Stooq + Polygon fallbacks ------------------------------
def _no_sleep(ds, monkeypatch):
    monkeypatch.setattr(ds, "time", type("T", (), {
        "sleep": staticmethod(lambda *_: None),
        "monotonic": staticmethod(lambda: 0.0),
    }))


def _boom(sym, period):
    raise RuntimeError("yfinance down")


def test_stooq_download_parses_csv(ds, monkeypatch):
    csv = "Date,Open,High,Low,Close,Volume\n" + "\n".join(
        f"2024-01-{d:02d},10,11,9,10.5,1000" for d in range(1, 21)
    )

    class R:
        status_code = 200
        text = csv

    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: R())
    df = ds._download_ohlcv_stooq("MOG-A", "max")
    assert df is not None and not df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_stooq_no_data_returns_none(ds, monkeypatch):
    class R:
        status_code = 200
        text = "No data"

    monkeypatch.setattr(ds.requests, "get", lambda *a, **k: R())
    assert ds._download_ohlcv_stooq("BOGUS", "1y") is None


def test_stooq_skips_non_us_suffix(ds, monkeypatch):
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        raise AssertionError("should not hit the network")

    monkeypatch.setattr(ds.requests, "get", spy)
    assert ds._download_ohlcv_stooq("VUAG.L", "1y") is None
    assert called["n"] == 0


def test_fetch_ohlcv_falls_through_to_stooq(ds, tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", str(tmp_path / "cache"))
    _no_sleep(ds, monkeypatch)
    monkeypatch.setattr(ds, "_download_ohlcv", _boom)

    csv = "Date,Open,High,Low,Close,Volume\n" + "\n".join(
        f"2024-01-{d:02d},10,11,9,10.5,1000" for d in range(1, 21)
    )

    def fake_get(url, *a, **k):
        # AV and Polygon need keys (none set) so only Stooq reaches here.
        assert "stooq" in url
        return type("R", (), {"status_code": 200, "text": csv})()

    monkeypatch.setattr(ds.requests, "get", fake_get)
    df = ds.fetch_ohlcv("MOG-A", period="max")
    assert df is not None and not df.empty


def test_massive_needs_key(ds):
    # No key configured -> no network attempt, returns None.
    assert ds._download_ohlcv_massive("MOG-A", "1y") is None


def test_massive_key_back_compat_polygon_setting(ds, db):
    # A key saved under the legacy lt_polygon_key name is still honoured.
    db.set_setting("lt_polygon_key", "legacykey")
    assert ds._get_massive_key() == "legacykey"
    db.set_setting("lt_massive_key", "newkey")
    assert ds._get_massive_key() == "newkey"  # new name wins


def test_massive_converts_share_class_and_prefers_new_host(ds, db, monkeypatch):
    db.set_setting("lt_massive_key", "testkey")
    seen = {}
    ts = int(_dt.datetime(2024, 1, 2).timestamp() * 1000)
    results = [
        {"t": ts + i * 86400000, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000}
        for i in range(20)
    ]

    def fake_get(url, *a, **k):
        seen["url"] = url
        return type("R", (), {
            "status_code": 200,
            "json": staticmethod(lambda: {"results": results}),
        })()

    monkeypatch.setattr(ds.requests, "get", fake_get)
    df = ds._download_ohlcv_massive("MOG-A", "max")
    assert df is not None and not df.empty
    # yfinance MOG-A must be sent as MOG.A, to the new massive.com host first.
    assert "/ticker/MOG.A/" in seen["url"]
    assert "api.massive.com" in seen["url"]


def test_massive_falls_back_to_legacy_host(ds, db, monkeypatch):
    db.set_setting("lt_massive_key", "testkey")
    hosts = []
    ts = int(_dt.datetime(2024, 1, 2).timestamp() * 1000)
    results = [
        {"t": ts + i * 86400000, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000}
        for i in range(20)
    ]

    def fake_get(url, *a, **k):
        hosts.append(url)
        # New host errors out; legacy host serves data.
        if "massive.com" in url:
            return type("R", (), {"status_code": 500,
                                   "json": staticmethod(lambda: {})})()
        return type("R", (), {"status_code": 200,
                              "json": staticmethod(lambda: {"results": results})})()

    monkeypatch.setattr(ds.requests, "get", fake_get)
    df = ds._download_ohlcv_massive("AAPL", "max")
    assert df is not None and not df.empty
    assert any("massive.com" in u for u in hosts)
    assert any("polygon.io" in u for u in hosts)


def test_fetch_ohlcv_falls_through_to_massive(ds, tmp_path, db, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", str(tmp_path / "cache"))
    _no_sleep(ds, monkeypatch)
    monkeypatch.setattr(ds, "_download_ohlcv", _boom)
    db.set_setting("lt_massive_key", "testkey")

    ts = int(_dt.datetime(2024, 1, 2).timestamp() * 1000)
    results = [
        {"t": ts + i * 86400000, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000}
        for i in range(20)
    ]

    def fake_get(url, *a, **k):
        if "stooq" in url:
            return type("R", (), {"status_code": 200, "text": "No data"})()
        if "aggs" in url:
            return type("R", (), {
                "status_code": 200,
                "json": staticmethod(lambda: {"results": results}),
            })()
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(ds.requests, "get", fake_get)
    df = ds.fetch_ohlcv("MOG-A", period="max")
    assert df is not None and not df.empty


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
