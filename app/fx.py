"""Currency conversion for the cross-broker dashboard.

Holdings come back from each broker in their own trading currency (USD, EUR,
GBP, …). To show one comparable set of totals, the dashboard converts every
amount into a single display currency chosen by the user (GBP by default).

Rates are ECB reference rates fetched from ``frankfurter.app`` (no API key,
base GBP, published daily and including INR). They are cached in the settings
table with a TTL; if the network is unavailable the last cached table is reused
(flagged ``stale``) so the dashboard degrades gracefully instead of breaking.
"""

from __future__ import annotations

import time
from typing import Any

from . import db

# Currencies the UI lets the user pick as the display currency.
SUPPORTED_DISPLAY = ("GBP", "USD", "INR")

_FX_URL = "https://api.frankfurter.app/latest"
_CACHE_KEY = "fx_rates_cache"          # settings row holding the JSON table
_TTL_S = 6 * 3600                      # refresh at most every 6 hours
# Symbols to request against the GBP base — every currency a holding might be
# quoted in, plus the display options.
_SYMBOLS = "USD,EUR,INR,CAD,AUD,JPY,CHF,HKD,SGD,SEK,NOK,DKK,PLN,ZAR"


def _fetch_live() -> dict | None:
    """Fetch GBP-based rates from frankfurter.app, or ``None`` on any failure."""
    try:
        import requests

        resp = requests.get(
            _FX_URL, params={"from": "GBP", "to": _SYMBOLS}, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        rates = {
            str(k).upper(): float(v)
            for k, v in (data.get("rates") or {}).items()
        }
        if not rates:
            return None
        rates["GBP"] = 1.0  # base
        return {
            "base": "GBP",
            "rates": rates,
            "ts": time.time(),
            "date": data.get("date"),
        }
    except Exception as exc:  # noqa: BLE001 — never let FX break the dashboard
        db.log_event("error", action="fx_fetch", detail=f"FX fetch failed: {exc}")
        return None


def get_rates(force: bool = False) -> dict:
    """Return the current rate table, using the cache within its TTL.

    The returned dict always has ``base``, ``rates`` (``{CCY: per-1-GBP}``) and
    ``ts``. It may additionally carry ``stale=True`` (served from an expired
    cache after a failed refresh) or ``unavailable=True`` (no cache and no
    network — only GBP is convertible).
    """
    cache = db.get_setting(_CACHE_KEY)
    now = time.time()
    if (
        not force
        and isinstance(cache, dict)
        and cache.get("rates")
        and (now - float(cache.get("ts") or 0)) < _TTL_S
    ):
        return cache

    fresh = _fetch_live()
    if fresh:
        db.set_setting(_CACHE_KEY, fresh)
        return fresh

    if isinstance(cache, dict) and cache.get("rates"):
        stale = dict(cache)
        stale["stale"] = True
        return stale

    return {"base": "GBP", "rates": {"GBP": 1.0}, "ts": 0.0, "unavailable": True}


def convert(
    amount: float | None,
    frm: str | None,
    to: str,
    rates: dict | None = None,
) -> float | None:
    """Convert ``amount`` from currency ``frm`` to ``to``.

    Returns ``None`` when the amount is missing or either currency is unknown to
    the rate table, so callers can render an explicit "—" rather than a wrong
    number. Rates are expressed as units-per-1-GBP, so any cross rate is
    ``amount / rate[frm] * rate[to]``.
    """
    if amount is None:
        return None
    frm = (frm or "").upper()
    to = (to or "").upper()
    if not frm:
        return None
    if frm == to:
        return amount
    table = (rates or get_rates()).get("rates") or {}
    rf = table.get(frm)
    rt = table.get(to)
    if not rf or rt is None:
        return None
    return amount / rf * rt


def symbol(ccy: str) -> str:
    """Return a display symbol for a currency code (falls back to ``CODE ``)."""
    return {
        "USD": "$", "GBP": "£", "EUR": "€", "INR": "₹", "CAD": "C$",
        "AUD": "A$", "JPY": "¥", "CHF": "CHF ", "HKD": "HK$", "SGD": "S$",
    }.get((ccy or "").upper(), (f"{ccy} " if ccy else ""))
