"""Fundamentals leg tests (ALG-13).

yfinance is mocked entirely (fixture dicts / monkeypatched fetch) — no network.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def fund(db):
    import app.longterm.fundamentals as mod

    mod = importlib.reload(mod)
    return mod


# ---- Rubric --------------------------------------------------------------
HEALTHY_LARGE_CAP = {
    "trailing_pe": 28.0,
    "forward_pe": 22.0,        # forward < trailing -> earnings growth
    "revenue_growth": 0.18,    # strong
    "earnings_growth": 0.25,
    "profit_margin": 0.24,     # fat margins
    "debt_to_equity": 40.0,    # 0.4x
    "recommendation_key": "buy",
    "recommendation_mean": 1.8,
}

DETERIORATING_SMALL_CAP = {
    "trailing_pe": 60.0,
    "forward_pe": 90.0,        # forward > trailing -> earnings shrinking
    "revenue_growth": -0.12,   # declining
    "earnings_growth": -0.3,
    "profit_margin": -0.05,    # loss-making
    "debt_to_equity": 320.0,   # 3.2x leverage
    "recommendation_key": "sell",
    "recommendation_mean": 4.2,
}


def test_healthy_large_cap_positive(fund):
    r = fund.fundamental_score(HEALTHY_LARGE_CAP, "equity")
    assert r.status == "ok"
    assert r.score > 0
    assert r.summary["components_used"] == 3
    assert r.summary["valuation"]["component"] > 0
    assert r.summary["growth"]["component"] > 0


def test_deteriorating_small_cap_negative(fund):
    r = fund.fundamental_score(DETERIORATING_SMALL_CAP, "equity")
    assert r.status == "ok"
    assert r.score < 0
    assert r.summary["quality"]["component"] < 0


def test_etf_not_applicable(fund):
    r = fund.fundamental_score(HEALTHY_LARGE_CAP, "etf")
    assert r.status == "not_applicable"


def test_mostly_missing_is_no_data(fund):
    # Only one usable component (valuation) -> below the 2-component floor.
    metrics = {"trailing_pe": 20.0, "forward_pe": None, "revenue_growth": None,
               "profit_margin": None, "debt_to_equity": None}
    r = fund.fundamental_score(metrics, "equity")
    assert r.status == "no_data"
    assert r.summary["components_used"] == 1


def test_missing_component_noted(fund):
    # Two components present (growth + quality), valuation absent -> ok + note.
    metrics = {"revenue_growth": 0.10, "profit_margin": 0.15,
               "debt_to_equity": 30.0}
    r = fund.fundamental_score(metrics, "equity")
    assert r.status == "ok"
    assert any("valuation skipped" in n for n in r.summary["notes"])


def test_none_metrics_no_data(fund):
    assert fund.fundamental_score(None, "equity").status == "no_data"


# ---- Fetch + cache (yfinance mocked) -------------------------------------
def test_fetch_reads_through_cache(fund, db, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(sym):
        calls["n"] += 1
        return {"trailing_pe": 20.0, "revenue_growth": 0.1}

    monkeypatch.setattr(fund, "_fetch_from_yf", fake_fetch)

    m1 = fund.fetch_fundamentals("AAPL")
    assert m1["trailing_pe"] == 20.0
    assert calls["n"] == 1
    # Fresh cache -> no second network fetch.
    m2 = fund.fetch_fundamentals("AAPL")
    assert m2["trailing_pe"] == 20.0
    assert calls["n"] == 1


def test_fetch_stale_cache_refreshes(fund, db, monkeypatch):
    # Seed a stale cache entry (fetched 8 days ago).
    import time
    db.set_fundamentals_cache("AAPL", {"trailing_pe": 99.0})
    with db._conn_sqlite() as conn:
        conn.execute(
            "UPDATE longterm_fundamentals_cache SET fetched_ts=? WHERE symbol=?",
            (time.time() - 8 * 24 * 3600, "AAPL"),
        )

    monkeypatch.setattr(fund, "_fetch_from_yf",
                        lambda sym: {"trailing_pe": 15.0})
    m = fund.fetch_fundamentals("AAPL")
    assert m["trailing_pe"] == 15.0  # refreshed, not the stale 99.0


def test_fetch_falls_back_to_stale_on_error(fund, db, monkeypatch):
    import time
    db.set_fundamentals_cache("AAPL", {"trailing_pe": 42.0})
    with db._conn_sqlite() as conn:
        conn.execute(
            "UPDATE longterm_fundamentals_cache SET fetched_ts=? WHERE symbol=?",
            (time.time() - 8 * 24 * 3600, "AAPL"),
        )

    def boom(sym):
        raise RuntimeError("yf down")

    monkeypatch.setattr(fund, "_fetch_from_yf", boom)
    m = fund.fetch_fundamentals("AAPL")
    assert m["trailing_pe"] == 42.0  # stale fallback beats nothing


def test_fetch_empty_symbol(fund):
    assert fund.fetch_fundamentals("") is None


def test_extract_metrics_maps_info_keys(fund):
    info = {"trailingPE": 25.0, "forwardPE": 20.0, "revenueGrowth": 0.1,
            "profitMargins": 0.2, "debtToEquity": 50.0,
            "recommendationKey": "buy"}
    m = fund._extract_metrics(info)
    assert m["trailing_pe"] == 25.0
    assert m["forward_pe"] == 20.0
    assert m["recommendation_key"] == "buy"
