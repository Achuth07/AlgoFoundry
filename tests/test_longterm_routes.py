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
import json
import re
import threading
import time

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
    assert ">Partial<" in body  # data_quality badge (pill label)


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


# ---- manual run + auto-refresh polling -------------------------------------
def test_run_status_idle_is_terminal_and_no_trigger(client):
    """With no run ever started the fragment must not poll or fire a refresh."""
    tc, _db, _main = client
    resp = tc.get("/longterm/run-status", headers=_auth())
    assert resp.status_code == 200
    assert "hx-trigger=\"every 2s\"" not in resp.text
    assert "HX-Trigger" not in resp.headers


def test_run_returns_polling_fragment(client, monkeypatch):
    """Run now returns a self-polling fragment while the pipeline is in flight."""
    tc, _db, main_mod = client

    started = threading.Event()
    release = threading.Event()

    def slow_pipeline(force=False):
        started.set()
        release.wait(timeout=5)
        return {"processed": 3, "counts": {"BUY": 2, "HOLD": 1, "SELL": 0},
                "errors": []}

    import app.longterm.scheduler as sched
    monkeypatch.setattr(sched, "run_daily_pipeline", slow_pipeline)

    resp = tc.post("/longterm/run", headers=_auth())
    assert resp.status_code == 200
    assert 'hx-get="/longterm/run-status"' in resp.text
    assert 'hx-trigger="every 2s"' in resp.text
    assert started.wait(timeout=5)

    # Mid-run the poll endpoint keeps polling and does not fire the refresh.
    mid = tc.get("/longterm/run-status", headers=_auth())
    assert 'hx-trigger="every 2s"' in mid.text
    assert "in progress" in mid.text.lower()
    assert "HX-Trigger" not in mid.headers

    # A second click must not launch an overlapping pipeline.
    dup = tc.post("/longterm/run", headers=_auth())
    assert "in progress" in dup.text.lower()

    release.set()
    _wait_until(lambda: not main_mod._LT_RUN["running"])

    # Terminal fragment: no polling, summary shown, refresh event fired.
    done = tc.get("/longterm/run-status", headers=_auth())
    assert 'hx-trigger="every 2s"' not in done.text
    assert "Run complete" in done.text
    assert "3 processed" in done.text
    assert "2 BUY" in done.text
    assert done.headers.get("HX-Trigger") == "ltRunDone"


def test_run_failure_surfaces_and_still_triggers_refresh(client, monkeypatch):
    tc, _db, main_mod = client

    def boom(force=False):
        raise RuntimeError("pipeline exploded")

    import app.longterm.scheduler as sched
    monkeypatch.setattr(sched, "run_daily_pipeline", boom)

    tc.post("/longterm/run", headers=_auth())
    _wait_until(lambda: not main_mod._LT_RUN["running"])

    done = tc.get("/longterm/run-status", headers=_auth())
    assert "Run failed" in done.text
    assert "pipeline exploded" in done.text
    assert 'hx-trigger="every 2s"' not in done.text
    assert done.headers.get("HX-Trigger") == "ltRunDone"


# ---- dashboard tab wiring --------------------------------------------------
def test_lt_subnav_items_each_have_a_view(client):
    """Every Long-Term sidebar item must map to a view div, else it blanks."""
    tc, _db, _main = client
    body = tc.get("/", headers=_auth()).text

    navs = re.findall(r'data-view="(lt:[a-z]+)"', body)
    views = set(re.findall(r'id="(view-lt-[a-z]+)"', body))
    assert navs == ["lt:analysis", "lt:analyze", "lt:settings", "lt:logs"]
    for nav in navs:
        assert f"view-{nav.replace(':', '-')}" in views


def test_adhoc_form_lives_in_analyze_view_only(client):
    """The ad-hoc form moved out of the portfolio page into its own tab."""
    tc, _db, _main = client
    body = tc.get("/", headers=_auth()).text

    assert body.count('hx-post="/longterm/analyze"') == 1
    assert body.count('id="longterm-body"') == 1

    analyze_at = body.index('id="view-lt-analyze"')
    form_at = body.index('hx-post="/longterm/analyze"')
    settings_at = body.index('id="view-lt-settings"')
    portfolio_at = body.index('id="view-lt-analysis"')
    table_at = body.index('id="longterm-body"')

    # Form sits inside the analyze view; the verdict table inside the portfolio view.
    assert analyze_at < form_at < settings_at
    assert portfolio_at < table_at < analyze_at


# ---- data-quality badge + flag chips ---------------------------------------
def test_data_quality_badge_maps_all_states(client):
    _tc, _db, main_mod = client
    assert main_mod._lt_data_quality_badge("full")["tone"] == "on"
    assert main_mod._lt_data_quality_badge("full")["label"] == "Full"
    assert main_mod._lt_data_quality_badge("partial_data")["tone"] == "amber"
    assert main_mod._lt_data_quality_badge("no_data")["tone"] == "off"
    assert main_mod._lt_data_quality_badge(None) is None
    # Unknown value degrades gracefully rather than raising.
    assert main_mod._lt_data_quality_badge("weird")["tone"] == "neutral"


def test_flag_chips_are_readable_with_tooltips(client):
    _tc, _db, main_mod = client
    chips = main_mod._lt_flag_chips("high_divergence,manual_review")
    assert [c["label"] for c in chips] == ["Signals diverge", "Review"]
    assert all(c["tone"] == "amber" for c in chips)
    # Tooltip carries the full plain-English phrase.
    assert "manual review" in chips[1]["title"]

    assert main_mod._lt_flag_chips("") == []
    assert main_mod._lt_flag_chips(None) == []

    # Unknown flag: humanized label, neutral tone, no crash.
    odd = main_mod._lt_flag_chips("adhoc,some_new_flag")
    assert odd[0]["label"] == "Ad-hoc"
    assert odd[1]["label"] == "some new flag"


def test_verdict_table_renders_full_pill_and_flag_chips(client):
    tc, db_mod, _main = client
    today = _dt.date.today().isoformat()
    db_mod.upsert_holdings_snapshot(
        date=today, t212_ticker="AAPL_US_EQ", symbol="AAPL", qty=1.0,
        avg_price=100.0, current_price=110.0, pnl=10.0, currency="USD",
    )
    db_mod.upsert_verdict(
        date=today, symbol="AAPL", composite=0.5, label="BUY", confidence=0.4,
        score_technical=1.0, score_fundamental=0.5, score_analyst=0.5,
        score_news=1.0, data_quality="full",
        review_flags="high_divergence,manual_review",
        rationale="Verdict: BUY.",
    )
    body = tc.get("/longterm", headers=_auth()).text

    # "Full" is now a green pill, not plain muted text.
    assert 'class="badge sm on"' in body and ">Full<" in body
    assert ">NO_DATA<" not in body  # legacy raw labels gone
    # Flags are readable chips, not the raw comma string.
    assert "high_divergence,manual_review" not in body
    assert ">Signals diverge<" in body and ">Review<" in body


# ---- verdict "why" panel breakdown -----------------------------------------
def test_verdict_detail_splits_legs_news_and_note(client):
    """Rationale + stored AI payload decompose into structured panel parts."""
    _tc, _db, main_mod = client
    v = {
        "rationale": (
            "Verdict: BUY. Technicals +0.50 (trend +0.0, RSI 59). "
            "Fundamentals +0.10 (fwd P/E 37.2, rev growth -31%). "
            "Analyst view +0.92 (consensus +0.61). News read +2.00. "
            "News: Coinbase stock jumped. "
            "Note: a material event flags this for manual review."
        ),
        "score_technical": 0.5, "score_fundamental": 0.1,
        "score_analyst": 0.92, "score_news": 2.0,
        "raw_ai_response": json.dumps({
            "key_facts": ["Fact one", "Fact two", "Fact three"],
        }),
    }
    d = main_mod._lt_verdict_detail(v)

    assert [leg["label"] for leg in d["legs"]] == [
        "Technical", "Fundamental", "Analyst", "News",
    ]
    assert d["legs"][0]["score"] == 0.5
    assert d["legs"][0]["detail"] == "trend +0.0, RSI 59"
    assert d["legs"][1]["detail"] == "fwd P/E 37.2, rev growth -31%"
    assert d["legs"][2]["detail"] == "consensus +0.61"
    assert d["legs"][3]["detail"] == ""  # news leg has no parenthetical

    # Structured key_facts win over the prose "News:" sentences.
    assert d["news"] == ["Fact one", "Fact two", "Fact three"]
    assert d["note"] == "a material event flags this for manual review."


def test_verdict_detail_falls_back_to_prose_news(client):
    """With no AI payload, news bullets come from the rationale sentences."""
    _tc, _db, main_mod = client
    v = {
        "rationale": (
            "Verdict: HOLD. Technicals +0.00 (trend +0.0, RSI 49). "
            "News: Alpha happened. News: Beta happened."
        ),
        "score_technical": 0.0,
        "raw_ai_response": None,
    }
    d = main_mod._lt_verdict_detail(v)
    assert [leg["label"] for leg in d["legs"]] == ["Technical"]
    assert d["news"] == ["Alpha happened", "Beta happened"]
    assert d["note"] == ""


def test_verdict_detail_handles_missing_and_malformed(client):
    """Absent legs are skipped; bad JSON must not raise."""
    _tc, _db, main_mod = client
    assert main_mod._lt_verdict_detail({}) == {"legs": [], "news": [], "note": ""}

    d = main_mod._lt_verdict_detail(
        {"rationale": "Verdict: HOLD.", "score_news": -1.0,
         "raw_ai_response": "{not valid json"}
    )
    assert [leg["label"] for leg in d["legs"]] == ["News"]
    assert d["legs"][0]["score"] == -1.0
    assert d["news"] == []


def test_why_panel_renders_bullets_and_colors(client, monkeypatch):
    """The expanded row renders as a full-width structured panel."""
    tc, db_mod, _main = client
    today = _dt.date.today().isoformat()
    db_mod.upsert_holdings_snapshot(
        date=today, t212_ticker="COIN_US_EQ", symbol="COIN", qty=1.0,
        avg_price=100.0, current_price=120.0, pnl=20.0, currency="USD",
    )
    db_mod.upsert_verdict(
        date=today, symbol="COIN", composite=0.88, label="BUY", confidence=0.44,
        score_technical=0.5, score_news=2.0, data_quality="full",
        review_flags="manual_review",
        rationale=("Verdict: BUY. Technicals +0.50 (trend +0.0, RSI 59). "
                   "News read +2.00. Note: flagged for manual review."),
        raw_ai_response=json.dumps({"key_facts": ["Bullet A", "Bullet B"]}),
    )

    body = tc.get("/longterm", headers=_auth()).text
    assert 'colspan="11"' in body          # full-width panel row
    assert "Signal breakdown" in body
    assert "<li" in body and "Bullet A" in body and "Bullet B" in body
    assert "var(--green-hi)" in body       # BUY accent / positive scores
    assert "flagged for manual review." in body


def _wait_until(pred, timeout: float = 5.0) -> None:
    """Spin until ``pred()`` is true or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")
