"""Trading 212 API client tests (ALG-1).

No real network calls: the module's single HTTP seam (``t212._get`` /
``urllib.request.urlopen``) is monkeypatched. Covers portfolio normalization,
auth failure surfacing (no key leakage), 429-then-success retry, demo/live
base-URL selection, and sync_instruments enrichment.
"""

from __future__ import annotations

import importlib
import io
import json
import urllib.error

import pytest


@pytest.fixture
def t212(db, instruments):
    """Reload ``app.longterm.t212`` bound to the temp-DB ``app.db`` and the
    reloaded instruments module. Resets throttle state so tests don't sleep."""
    import app.longterm.t212 as t212_mod

    t212_mod = importlib.reload(t212_mod)
    t212_mod._LAST_CALL.clear()
    return t212_mod


# ---- Sample payloads -------------------------------------------------------

SAMPLE_PORTFOLIO = [
    {
        "ticker": "AAPL_US_EQ",
        "quantity": 10,
        "averagePrice": 150.0,
        "currentPrice": 175.5,
        "ppl": 255.0,
        "fxPpl": 3.2,
    },
    {
        "ticker": "VUAGl_EQ",
        "quantity": 4.5,
        "averagePrice": 80.0,
        "currentPrice": 82.0,
        "ppl": 9.0,
        # no fxPpl on this one
    },
]


# ---- Portfolio normalization ----------------------------------------------


def test_fetch_portfolio_normalizes(t212, monkeypatch):
    monkeypatch.setattr(t212, "_get", lambda path, **kw: SAMPLE_PORTFOLIO)
    # Provide a key so _get's guard (if reached) is satisfied; _get is mocked
    # here but keep the setting realistic.
    holdings = t212.fetch_portfolio(api_key="secret-key", env="demo")

    assert len(holdings) == 2
    aapl = holdings[0]
    assert aapl.t212_ticker == "AAPL_US_EQ"
    assert aapl.quantity == 10.0
    assert aapl.avg_price == 150.0
    assert aapl.current_price == 175.5
    assert aapl.ppl == 255.0
    assert aapl.fx_ppl == 3.2
    # Currency resolved via instruments.resolve() (US equity -> USD).
    assert aapl.currency == "USD"

    vuag = holdings[1]
    assert vuag.t212_ticker == "VUAGl_EQ"
    assert vuag.fx_ppl is None
    # London listing has no currency from the parser -> stays None until
    # metadata enrichment fills it.
    assert vuag.currency is None


def test_fetch_portfolio_handles_empty(t212, monkeypatch):
    monkeypatch.setattr(t212, "_get", lambda path, **kw: None)
    assert t212.fetch_portfolio(api_key="k") == []


# ---- Auth failure surfacing -----------------------------------------------


def test_missing_key_raises_without_leak(t212):
    with pytest.raises(t212.T212Error) as exc:
        t212._get("/api/v0/equity/portfolio", api_key="")
    msg = str(exc.value)
    assert "check API key / environment" in msg


def test_401_raises_t212error_no_key_leak(t212, monkeypatch):
    SECRET = "super-secret-key-123"

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", hdrs={}, fp=io.BytesIO(b"")
        )

    monkeypatch.setattr(t212.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(t212.T212Error) as exc:
        t212.fetch_portfolio(api_key=SECRET, env="demo")

    msg = str(exc.value)
    assert "check API key / environment" in msg
    assert SECRET not in msg  # never leak the key

    # Error should have been logged, also without the key.
    events = t212.db.recent_events(5)
    assert any(
        e["kind"] == "error" and e["action"] == "t212_fetch" for e in events
    )
    for e in events:
        assert SECRET not in (e.get("detail") or "")


def test_403_raises_t212error(t212, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", hdrs={}, fp=io.BytesIO(b"")
        )

    monkeypatch.setattr(t212.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(t212.T212Error) as exc:
        t212._get("/api/v0/equity/portfolio", api_key="k")
    assert "check API key / environment" in str(exc.value)


# ---- Retry on 429 then success --------------------------------------------


def test_429_then_success_retries(t212, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                hdrs={"Retry-After": "0"}, fp=io.BytesIO(b""),
            )
        return _FakeResp(json.dumps(SAMPLE_PORTFOLIO).encode())

    monkeypatch.setattr(t212.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(t212.time, "sleep", lambda *_a, **_k: None)

    holdings = t212.fetch_portfolio(api_key="k", env="demo")
    assert calls["n"] == 2
    assert len(holdings) == 2


def test_5xx_exhausts_retries_and_raises(t212, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", hdrs={},
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(t212.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(t212.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(t212.T212Error) as exc:
        t212._get("/api/v0/equity/portfolio", api_key="k")
    assert calls["n"] == t212._MAX_TRIES
    assert "HTTP 503" in str(exc.value)


# ---- Environment / base URL selection -------------------------------------


def test_base_url_demo_vs_live(t212):
    assert t212._base_url("demo") == "https://demo.trading212.com"
    assert t212._base_url("live") == "https://live.trading212.com"
    # Unknown env falls back to demo.
    assert t212._base_url("garbage") == "https://demo.trading212.com"


def test_env_setting_drives_base_url(t212):
    t212.db.set_setting("lt_t212_env", "live")
    assert t212._base_url() == "https://live.trading212.com"
    t212.db.set_setting("lt_t212_env", "demo")
    assert t212._base_url() == "https://demo.trading212.com"


def test_get_targets_selected_env_url(t212, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _FakeResp(b"[]")

    monkeypatch.setattr(t212.urllib.request, "urlopen", fake_urlopen)
    t212._get("/api/v0/equity/portfolio", api_key="k", env="live")
    assert seen["url"].startswith("https://live.trading212.com")


def test_auth_header_uses_raw_key_no_bearer(t212, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        return _FakeResp(b"[]")

    monkeypatch.setattr(t212.urllib.request, "urlopen", fake_urlopen)
    t212._get("/api/v0/equity/portfolio", api_key="raw-key-xyz", env="demo")
    assert seen["auth"] == "raw-key-xyz"  # no "Bearer " prefix


# ---- sync_instruments ------------------------------------------------------


def test_sync_instruments_calls_enrich(t212, monkeypatch):
    payload = [
        {"ticker": "AAPL_US_EQ", "type": "STOCK", "currencyCode": "USD"},
        {"ticker": "VUAGl_EQ", "type": "ETF", "currencyCode": "GBX"},
    ]
    monkeypatch.setattr(t212, "_get", lambda path, **kw: payload)

    captured = {}
    real_enrich = t212.instruments.enrich_from_t212

    def spy_enrich(p):
        captured["payload"] = p
        return real_enrich(p)

    monkeypatch.setattr(t212.instruments, "enrich_from_t212", spy_enrich)

    count = t212.sync_instruments(api_key="k", env="demo")
    assert captured["payload"] == payload
    assert count == 2
    # Enrichment actually persisted.
    assert t212.db.get_instrument("AAPL_US_EQ")["currency"] == "USD"
    assert t212.db.get_instrument("VUAGl_EQ")["instrument_type"] == "etf"


# ---- Test helpers ----------------------------------------------------------


class _FakeResp:
    """Minimal stand-in for the urlopen context-manager response object."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
