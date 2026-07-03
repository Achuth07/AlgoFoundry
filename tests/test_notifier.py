"""WhatsApp notifier tests (ALG-6).

The requests library is mocked at the module seam so no real HTTP happens.
Covers: summary formatting (alerts first, counts, truncation), unconfigured
no-op, HTTP success/failure paths, and the guarantee that the API key never
lands in a log event.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def notifier(db):
    """Reload the notifier bound to the temp-DB ``app.db``."""
    import app.longterm.notifier as mod
    return importlib.reload(mod)


def _v(symbol, label, composite, rationale="", flags="", dq="full", override=0):
    return {
        "symbol": symbol, "label": label, "composite": composite,
        "rationale": rationale, "review_flags": flags, "data_quality": dq,
        "override_flag": override,
    }


# ---- compose_summary -------------------------------------------------------
def test_compose_header_counts(notifier):
    verdicts = [
        _v("AAPL", "BUY", 0.9, "Verdict: BUY. Strong trend."),
        _v("MSFT", "HOLD", 0.1, "Verdict: HOLD."),
        _v("GOOG", "HOLD", 0.2, "Verdict: HOLD."),
        _v("XYZ", "SELL", -1.0, "Verdict: SELL. Breaking down."),
    ]
    msg = notifier.compose_summary(verdicts, "2026-07-02")
    assert "AlgoFoundry LT — 2026-07-02 — 4 holdings" in msg
    assert "1 BUY / 2 HOLD / 1 SELL" in msg


def test_compose_alerts_first(notifier):
    verdicts = [
        _v("AAPL", "HOLD", 0.1, "Verdict: HOLD."),
        _v("XYZ", "SELL", -1.0, "Verdict: SELL. Trend broke."),
        _v("ZZZ", "HOLD", 0.0, "Verdict: HOLD.", dq="no_data"),
        _v("REV", "HOLD", 0.0, "Verdict: HOLD.", flags="manual_review"),
    ]
    msg = notifier.compose_summary(verdicts, "2026-07-02")
    alerts_idx = msg.index("ALERTS:")
    holdings_idx = msg.index("HOLDINGS:")
    assert alerts_idx < holdings_idx
    alerts_block = msg[alerts_idx:holdings_idx]
    assert "XYZ SELL" in alerts_block
    assert "ZZZ NO_DATA" in alerts_block
    assert "REV REVIEW" in alerts_block
    # A plain HOLD must not appear in the alerts block.
    assert "AAPL" not in alerts_block


def test_compose_no_alerts_section_when_clean(notifier):
    verdicts = [_v("AAPL", "HOLD", 0.1, "Verdict: HOLD.")]
    msg = notifier.compose_summary(verdicts, "2026-07-02")
    assert "ALERTS:" not in msg


def test_compose_truncates_and_marks(notifier):
    verdicts = [
        _v(f"SYM{i}", "HOLD", 0.1, "Verdict: HOLD. " + ("x" * 200))
        for i in range(200)
    ]
    msg = notifier.compose_summary(verdicts, "2026-07-02")
    assert len(msg) <= 3500
    assert "…and" in msg and "more" in msg


def test_compose_rationale_first_sentence(notifier):
    verdicts = [_v("AAPL", "BUY", 0.9,
                   "Verdict: BUY. Second sentence should not appear.")]
    msg = notifier.compose_summary(verdicts, "2026-07-02")
    assert "Verdict: BUY." in msg
    assert "Second sentence should not appear" not in msg


# ---- send_whatsapp ---------------------------------------------------------
def test_send_unconfigured_returns_false_and_logs_info(notifier, db):
    assert notifier.send_whatsapp("hello") is False
    events = db.recent_events(10)
    assert any(
        e["kind"] == "info" and "not configured" in (e["detail"] or "")
        for e in events
    )


class _FakeResp:
    def __init__(self, status):
        self.status_code = status


def test_send_success(notifier, db, monkeypatch):
    db.set_setting("lt_callmebot_phone", "+15551234567")
    db.set_setting("lt_callmebot_key", "SECRETKEY123")
    captured = {}

    def fake_get(url, timeout=20):
        captured["url"] = url
        return _FakeResp(200)

    monkeypatch.setattr(notifier.requests, "get", fake_get)
    assert notifier.send_whatsapp("hello world") is True
    assert "api.callmebot.com" in captured["url"]


def test_send_http_failure_returns_false_and_logs_error(notifier, db, monkeypatch):
    db.set_setting("lt_callmebot_phone", "+15551234567")
    db.set_setting("lt_callmebot_key", "SECRETKEY123")
    monkeypatch.setattr(notifier.requests, "get", lambda url, timeout=20: _FakeResp(500))
    assert notifier.send_whatsapp("hello") is False
    events = db.recent_events(10)
    assert any(e["kind"] == "error" for e in events)


def test_send_transport_exception_swallowed(notifier, db, monkeypatch):
    db.set_setting("lt_callmebot_phone", "+15551234567")
    db.set_setting("lt_callmebot_key", "SECRETKEY123")

    def boom(url, timeout=20):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(notifier.requests, "get", boom)
    assert notifier.send_whatsapp("hello") is False  # never raises


def test_api_key_never_logged(notifier, db, monkeypatch):
    secret = "SUPER_SECRET_KEY_XYZ"
    db.set_setting("lt_callmebot_phone", "+15551234567")
    db.set_setting("lt_callmebot_key", secret)
    monkeypatch.setattr(notifier.requests, "get", lambda url, timeout=20: _FakeResp(500))
    notifier.send_whatsapp("hello")
    for e in db.recent_events(20):
        assert secret not in (e["detail"] or "")
