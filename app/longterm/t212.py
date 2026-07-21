"""Trading 212 REST API client (long-term portfolio tracker).

Thin, dependency-free client over the Trading 212 public API. It reads its
API key/secret pair and environment (demo/live) from the settings table in
:mod:`app.db` and exposes two read-only endpoints used by the long-term
feature:

* :func:`fetch_portfolio`  -> ``/api/v0/equity/portfolio``
* :func:`fetch_instruments_metadata` -> ``/api/v0/equity/metadata/instruments``

Design notes
------------
* stdlib only (``urllib.request`` + ``json``); no third-party HTTP library.
* All network access funnels through the private :func:`_get` seam so tests
  can monkeypatch a single function instead of the socket layer.
* Per-endpoint min-interval throttling (T212 rate limits: portfolio ~1 req/5s,
  instruments metadata ~1 req/50s) via module-level timestamps.
* Exponential backoff with a small retry budget on 429 / 5xx, honoring the
  ``Retry-After`` header when present.
* Auth: T212 issues an API Key + API Secret pair. They are sent as HTTP
  Basic auth (``base64("KEY:SECRET")``) — the key is the "username", the
  secret is the "password". Neither is ever written to a log event or an
  exception message.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .. import db
from . import instruments

# ---- Configuration ---------------------------------------------------------

_BASE_URLS = {
    "demo": "https://demo.trading212.com",
    "live": "https://live.trading212.com",
}

_TIMEOUT_S = 30

# Per-endpoint minimum interval between requests (seconds). Keys are the API
# paths; anything not listed is unthrottled.
_MIN_INTERVAL = {
    "/api/v0/equity/portfolio": 5.0,
    "/api/v0/equity/metadata/instruments": 50.0,
}

# Retry policy for transient failures (429 / 5xx).
_MAX_TRIES = 3
_BACKOFF_BASE_S = 1.0  # exponential: base * 2**attempt

# Module-level throttle bookkeeping: path -> monotonic timestamp of last call.
_LAST_CALL: dict[str, float] = {}


class T212Error(Exception):
    """Raised for any Trading 212 client failure surfaced to callers.

    The message is safe to log/display: it never contains the API key.
    """


# ---- Data model ------------------------------------------------------------


@dataclass
class Holding:
    """One open position from the T212 portfolio endpoint (normalized)."""

    t212_ticker: str
    quantity: float | None = None
    avg_price: float | None = None
    current_price: float | None = None
    ppl: float | None = None            # unrealized profit/loss
    currency: str | None = None         # filled from instrument mapping if known
    fx_ppl: float | None = None         # FX component of P&L, if T212 reports it


# ---- Config helpers --------------------------------------------------------


def _resolve_env(env: str | None) -> str:
    """Return a validated environment name ("demo"/"live")."""
    val = (env if env is not None else db.get_setting("lt_t212_env", "demo"))
    val = (val or "demo").strip().lower()
    return val if val in _BASE_URLS else "demo"


def _base_url(env: str | None = None) -> str:
    return _BASE_URLS[_resolve_env(env)]


def _api_key(api_key: str | None = None) -> str:
    key = api_key if api_key is not None else db.get_setting("lt_t212_api_key", "")
    return (key or "").strip()


def _api_secret(api_secret: str | None = None) -> str:
    secret = (
        api_secret
        if api_secret is not None
        else db.get_setting("lt_t212_api_secret", "")
    )
    return (secret or "").strip()


def _basic_auth_header(key: str, secret: str) -> str:
    """Build the ``Authorization: Basic ...`` header value for a key/secret pair."""
    token = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


# ---- HTTP seam -------------------------------------------------------------


def _throttle(path: str) -> None:
    """Sleep just enough to honor the per-endpoint min interval for ``path``."""
    interval = _MIN_INTERVAL.get(path)
    if not interval:
        return
    last = _LAST_CALL.get(path)
    if last is not None:
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value (delta-seconds form only)."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        return None


def _get(
    path: str,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    env: str | None = None,
):
    """GET ``path`` against the selected T212 environment and return decoded
    JSON.

    This is the single network seam for the module. Tests monkeypatch this
    function directly. It applies per-endpoint throttling, retries transient
    errors (429 / 5xx) with exponential backoff, and maps auth/permanent
    failures onto :class:`T212Error` with key-free messages.
    """
    key = _api_key(api_key)
    secret = _api_secret(api_secret)
    if not key or not secret:
        raise T212Error(
            "Trading 212 API key/secret is not configured — check API key / "
            "secret / environment"
        )

    url = _base_url(env) + path
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            # T212 public API pairs a key + secret, sent as HTTP Basic auth.
            "Authorization": _basic_auth_header(key, secret),
            "Accept": "application/json",
        },
    )

    last_exc: Exception | None = None
    for attempt in range(_MAX_TRIES):
        _throttle(path)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                _LAST_CALL[path] = time.monotonic()
                raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            _LAST_CALL[path] = time.monotonic()
            code = exc.code
            if code in (401, 403):
                # Permanent auth failure — do not retry, never leak the key/secret.
                raise T212Error(
                    f"Trading 212 authentication failed (HTTP {code}) — "
                    "check API key / secret / environment"
                ) from None
            if code == 429 or 500 <= code < 600:
                last_exc = exc
                if attempt < _MAX_TRIES - 1:
                    retry_after = _parse_retry_after(
                        exc.headers.get("Retry-After") if exc.headers else None
                    )
                    delay = (
                        retry_after
                        if retry_after is not None
                        else _BACKOFF_BASE_S * (2 ** attempt)
                    )
                    time.sleep(delay)
                    continue
                break
            # Other 4xx — permanent, not worth retrying.
            raise T212Error(
                f"Trading 212 request failed for {path} (HTTP {code})"
            ) from None
        except urllib.error.URLError as exc:
            # Network-level error (DNS, connection, timeout). Retry a few times.
            _LAST_CALL[path] = time.monotonic()
            last_exc = exc
            if attempt < _MAX_TRIES - 1:
                time.sleep(_BACKOFF_BASE_S * (2 ** attempt))
                continue
            break

    # Retries exhausted.
    detail = ""
    if isinstance(last_exc, urllib.error.HTTPError):
        detail = f" (HTTP {last_exc.code})"
    raise T212Error(
        f"Trading 212 request failed for {path} after {_MAX_TRIES} attempts"
        f"{detail}"
    )


# ---- Normalization ---------------------------------------------------------


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_holding(item: dict) -> Holding:
    """Turn one raw T212 portfolio item into a :class:`Holding`.

    T212 fields: ticker, quantity, averagePrice, currentPrice, ppl, fxPpl.
    Currency is filled from the instrument mapping when we can resolve it.
    """
    ticker = item.get("ticker") or ""
    holding = Holding(
        t212_ticker=ticker,
        quantity=_to_float(item.get("quantity")),
        avg_price=_to_float(item.get("averagePrice")),
        current_price=_to_float(item.get("currentPrice")),
        ppl=_to_float(item.get("ppl")),
        fx_ppl=_to_float(item.get("fxPpl")),
    )
    if ticker:
        try:
            inst = instruments.resolve(ticker)
            holding.currency = inst.currency
        except Exception:
            # Instrument resolution is best-effort; a mapping miss must never
            # break portfolio normalization. Currency stays None.
            holding.currency = None
    return holding


# ---- Public API ------------------------------------------------------------


def fetch_portfolio(
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    env: str | None = None,
) -> list[Holding]:
    """Fetch open positions and return them as normalized :class:`Holding` rows.

    On failure logs an ``error`` event (without the API key/secret) and
    re-raises :class:`T212Error`.
    """
    try:
        data = _get(
            "/api/v0/equity/portfolio",
            api_key=api_key,
            api_secret=api_secret,
            env=env,
        )
    except T212Error as exc:
        db.log_event(
            "error", action="t212_fetch", status="failed", detail=str(exc)
        )
        raise
    items = data or []
    return [_normalize_holding(item) for item in items if isinstance(item, dict)]


def fetch_instruments_metadata(
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    env: str | None = None,
) -> list[dict]:
    """Fetch the full instruments metadata list (large payload).

    Used to enrich the instrument cache via :func:`sync_instruments`.
    """
    try:
        data = _get(
            "/api/v0/equity/metadata/instruments",
            api_key=api_key,
            api_secret=api_secret,
            env=env,
        )
    except T212Error as exc:
        db.log_event(
            "error", action="t212_fetch", status="failed", detail=str(exc)
        )
        raise
    return data or []


def sync_instruments(
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    env: str | None = None,
) -> int:
    """Fetch instruments metadata and enrich the instrument cache.

    Returns the number of instrument rows written by
    :func:`app.longterm.instruments.enrich_from_t212`.
    """
    payload = fetch_instruments_metadata(
        api_key=api_key, api_secret=api_secret, env=env
    )
    return instruments.enrich_from_t212(payload)
