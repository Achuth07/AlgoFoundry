"""Daily orchestration tests (ALG-7).

All external legs are mocked: T212 portfolio, the data-source legs, AI, and the
notifier. Covers the happy path, same-day skip / force, per-holding isolation,
notification de-dup, and total T212 failure.
"""

from __future__ import annotations

import importlib

import pytest

from app.longterm.data_sources import LegResult
from app.longterm.t212 import Holding, T212Error


@pytest.fixture
def scheduler(db, instruments):
    """Reload the scheduler (and the modules it wires) bound to the temp DB."""
    import app.longterm.scoring as scoring_mod
    importlib.reload(scoring_mod)
    import app.longterm.notifier as notifier_mod
    importlib.reload(notifier_mod)
    import app.longterm.scheduler as sched_mod
    sched_mod = importlib.reload(sched_mod)
    return sched_mod


def _mock_legs(scheduler, monkeypatch, *, technical_score=1.0):
    """Wire all data legs to deterministic, network-free results."""
    ds = scheduler.data_sources

    monkeypatch.setattr(ds, "fetch_ohlcv", lambda *a, **k: object())

    import app.longterm.technicals as tech
    monkeypatch.setattr(
        tech, "technical_score",
        lambda df: LegResult(status="ok", score=technical_score, summary={}),
    )
    import app.longterm.fundamentals as fund
    monkeypatch.setattr(fund, "fetch_fundamentals", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(
        fund, "fundamental_score",
        lambda m, t: LegResult(status="ok", score=0.5, summary={})
        if t != "etf" else LegResult(status="not_applicable"),
    )
    monkeypatch.setattr(ds, "fetch_analyst", lambda sym: None)
    monkeypatch.setattr(
        ds, "analyst_score",
        lambda *a, **k: LegResult(status="not_applicable"),
    )
    monkeypatch.setattr(
        ds, "fetch_news",
        lambda *a, **k: LegResult(status="not_applicable"),
    )
    monkeypatch.setattr(ds, "fetch_earnings_calendar", lambda *a, **k: None)


def _two_holdings():
    return [
        Holding(t212_ticker="AAPL_US_EQ", quantity=10, avg_price=150.0,
                current_price=175.0, ppl=250.0, currency="USD"),
        Holding(t212_ticker="MSFT_US_EQ", quantity=5, avg_price=300.0,
                current_price=320.0, ppl=100.0, currency="USD"),
    ]


# ---- Happy path ------------------------------------------------------------
def test_happy_path_writes_verdicts_and_snapshot(scheduler, db, monkeypatch):
    monkeypatch.setattr(scheduler.t212, "fetch_portfolio", lambda: _two_holdings())
    _mock_legs(scheduler, monkeypatch)
    sent = []
    monkeypatch.setattr(scheduler.notifier, "send_whatsapp",
                        lambda msg: sent.append(msg) or True)

    summary = scheduler.run_daily_pipeline(force=True)
    assert summary["processed"] == 2
    assert not summary["errors"]

    today = scheduler._today()
    assert db.get_setting("lt_last_run_date") == today
    snap = db.get_holdings_snapshot(today)
    assert len(snap) == 2
    verdicts = db.get_verdicts_for_date(today)
    assert len(verdicts) == 2
    assert len(sent) == 1  # one message for the whole run


# ---- Same-day skip / force -------------------------------------------------
def test_same_day_rerun_skipped_without_force(scheduler, db, monkeypatch):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return _two_holdings()

    monkeypatch.setattr(scheduler.t212, "fetch_portfolio", fetch)
    _mock_legs(scheduler, monkeypatch)
    monkeypatch.setattr(scheduler.notifier, "send_whatsapp", lambda msg: True)

    scheduler.run_daily_pipeline(force=True)
    out = scheduler.run_daily_pipeline(force=False)
    assert out["skipped"] is True
    assert calls["n"] == 1  # portfolio not re-fetched on the skipped run


def test_force_reruns_and_resends(scheduler, db, monkeypatch):
    monkeypatch.setattr(scheduler.t212, "fetch_portfolio", lambda: _two_holdings())
    _mock_legs(scheduler, monkeypatch)
    sent = []
    monkeypatch.setattr(scheduler.notifier, "send_whatsapp",
                        lambda msg: sent.append(msg) or True)

    scheduler.run_daily_pipeline(force=True)
    scheduler.run_daily_pipeline(force=True)  # forced -> re-sends
    assert len(sent) == 2


def test_notification_not_resent_same_day(scheduler, db, monkeypatch):
    monkeypatch.setattr(scheduler.t212, "fetch_portfolio", lambda: _two_holdings())
    _mock_legs(scheduler, monkeypatch)
    sent = []
    monkeypatch.setattr(scheduler.notifier, "send_whatsapp",
                        lambda msg: sent.append(msg) or True)

    scheduler.run_daily_pipeline(force=True)  # sends + marks notify date
    # A non-forced run on the same day is skipped entirely -> still 1 send.
    scheduler.run_daily_pipeline(force=False)
    assert len(sent) == 1


# ---- Per-holding isolation -------------------------------------------------
def test_one_holding_failure_isolated(scheduler, db, monkeypatch):
    monkeypatch.setattr(scheduler.t212, "fetch_portfolio", lambda: _two_holdings())
    _mock_legs(scheduler, monkeypatch)
    monkeypatch.setattr(scheduler.notifier, "send_whatsapp", lambda msg: True)

    import app.longterm.technicals as tech
    real = LegResult(status="ok", score=1.0, summary={})

    def flaky(df):
        # Fail for the first holding processed, succeed after.
        if flaky.calls == 0:
            flaky.calls += 1
            raise RuntimeError("boom on first symbol")
        flaky.calls += 1
        return real
    flaky.calls = 0
    monkeypatch.setattr(tech, "technical_score", flaky)

    summary = scheduler.run_daily_pipeline(force=True)
    # One symbol errored, the other still got a verdict; the run completed.
    assert summary["processed"] == 1
    assert len(summary["errors"]) == 1
    assert len(db.get_verdicts_for_date(scheduler._today())) == 1


# ---- T212 total failure ----------------------------------------------------
def test_t212_failure_returns_error_no_verdicts(scheduler, db, monkeypatch):
    def boom():
        raise T212Error("auth failed")

    monkeypatch.setattr(scheduler.t212, "fetch_portfolio", boom)
    summary = scheduler.run_daily_pipeline(force=True)
    assert "error" in summary
    assert summary["processed"] == 0
    assert db.get_verdicts_for_date(scheduler._today()) == []


# ---- Ad-hoc single-symbol analysis (ALG-14) --------------------------------
def _mock_type(scheduler, monkeypatch, qtype):
    """Force _detect_instrument_type to a given quoteType without network."""
    import app.longterm.fundamentals as fund

    class _FakeTicker:
        def __init__(self, sym):
            self.info = {"quoteType": qtype}

    class _FakeYf:
        Ticker = _FakeTicker

    monkeypatch.setattr(fund, "yf", _FakeYf)


def test_adhoc_default_does_not_persist(scheduler, db, monkeypatch):
    _mock_legs(scheduler, monkeypatch)
    _mock_type(scheduler, monkeypatch, "EQUITY")

    verdict = scheduler.analyze_adhoc("AAPL", save=False)
    assert verdict["symbol"] == "AAPL"
    assert verdict["label"] in ("BUY", "HOLD", "SELL")
    # Nothing written to the verdicts table.
    assert db.get_verdicts_for_symbol("AAPL") == []
    assert db.get_verdicts_for_date(scheduler._today()) == []


def test_adhoc_save_writes_row_with_adhoc_flag(scheduler, db, monkeypatch):
    _mock_legs(scheduler, monkeypatch)
    _mock_type(scheduler, monkeypatch, "EQUITY")

    scheduler.analyze_adhoc("AAPL", save=True)
    rows = db.get_verdicts_for_symbol("AAPL")
    assert len(rows) == 1
    assert "adhoc" in (rows[0]["review_flags"] or "")


def test_adhoc_etf_still_renders(scheduler, db, monkeypatch):
    _mock_legs(scheduler, monkeypatch)  # fundamental_score -> not_applicable for etf
    _mock_type(scheduler, monkeypatch, "ETF")

    verdict = scheduler.analyze_adhoc("VUAG.L", save=False)
    assert verdict["symbol"] == "VUAG.L"
    # ETF: no finnhub symbol (suffixed) -> analyst not scored; fundamental n/a.
    legs = verdict["legs"]
    assert legs["fundamental"].status == "not_applicable"
    assert verdict["data_quality"] != "full" or verdict["label"] is not None


def test_adhoc_unknown_symbol_returns_no_data(scheduler, db, monkeypatch):
    # OHLCV None -> unknown/invalid symbol; must not raise, must be NO_DATA.
    monkeypatch.setattr(scheduler.data_sources, "fetch_ohlcv", lambda *a, **k: None)
    _mock_type(scheduler, monkeypatch, "EQUITY")

    verdict = scheduler.analyze_adhoc("NOSUCHXYZ", save=False)
    assert verdict["data_quality"] == "no_data"
    assert verdict["label"] == "HOLD"
    assert db.get_verdicts_for_symbol("NOSUCHXYZ") == []


def test_adhoc_never_touches_last_run_date(scheduler, db, monkeypatch):
    _mock_legs(scheduler, monkeypatch)
    _mock_type(scheduler, monkeypatch, "EQUITY")

    scheduler.analyze_adhoc("AAPL", save=True)
    assert db.get_setting("lt_last_run_date", "") == ""
    assert db.get_setting("lt_last_notify_date", "") == ""
