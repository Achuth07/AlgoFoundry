"""Trading 212 ticker -> market-data symbol mapping.

Trading 212 exposes instruments under its own ticker scheme, e.g.
``AAPL_US_EQ``, ``VUAGl_EQ``, ``BARCl_EQ``. Market-data providers (yfinance /
Finnhub) want plain exchange-suffixed symbols such as ``AAPL`` or ``VUAG.L``.

This module parses the T212 ticker into an :class:`Instrument`, with a
read-through cache backed by the ``longterm_instruments`` table in
:mod:`app.db`. Rows flagged ``manual_override`` are never clobbered by
auto-mapping.

The actual HTTP calls to Trading 212 live in a later ticket (ALG-1); the
optional :func:`enrich_from_t212` hook works entirely offline from a payload
that the caller has already fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .. import db


@dataclass
class Instrument:
    """Resolved market-data mapping for one Trading 212 ticker."""

    t212_ticker: str
    yf_symbol: str | None = None
    finnhub_symbol: str | None = None
    currency: str | None = None
    exchange: str | None = None
    instrument_type: str = "unknown"  # equity | etf | unknown


# Trailing single-letter listing markers that appear immediately before the
# ``_EQ`` suffix on non-US listings, e.g. ``VUAGl_EQ`` -> London.
# Maps marker -> (yfinance exchange suffix, human exchange label).
_LISTING_SUFFIXES: dict[str, tuple[str, str]] = {
    "l": (".L", "London"),
    "d": (".DE", "Xetra"),
    "a": (".AS", "Amsterdam"),
    "p": (".PA", "Paris"),
}

# ``AAPL_US_EQ`` -> base=AAPL ; ``MOG/A_US_EQ`` -> base=MOG/A (share class).
# Trading 212 separates a US share class with a slash, so the base may contain
# ``/`` in addition to alphanumerics and dots.
_US_EQ = re.compile(r"^(?P<base>[A-Za-z0-9./]+)_US_EQ$")
# ``VUAGl_EQ`` -> base=VUAG, marker=l  (marker is a trailing lowercase letter)
_INTL_EQ = re.compile(r"^(?P<base>[A-Za-z0-9.]*[A-Z0-9])(?P<marker>[ldap])_EQ$")


# Successor mapping for tickers that changed via merger / de-SPAC / rename.
# Keyed on the *US-equity base symbol* (the part before ``_US_EQ``). Trading 212
# and TradingView may keep surfacing the legacy ticker for a position that has
# already been through a corporate action, which leaves the market-data legs
# with a dead symbol and "no data". Mapping the legacy base to its live
# successor lets the existing sources fetch real data again.
#
# Extend this dict as new corporate actions are observed.
TICKER_ALIASES: dict[str, str] = {
    # Inflection Point Acquisition Corp. II merged into USA Rare Earth,
    # delisted 2025-03-14; the position now trades as USAR.
    "IPXX": "USAR",
}


def _us_symbols(base: str) -> tuple[str, str]:
    """Derive (yfinance, Finnhub) symbols from a US-equity base.

    A share-class base like ``MOG/A`` becomes ``MOG-A`` for yfinance (which uses
    a hyphen) and ``MOG.A`` for Finnhub / most REST providers (which use a dot).
    Plain bases pass through unchanged.
    """
    yf_symbol = base.replace("/", "-")
    finnhub_symbol = base.replace("/", ".")
    return yf_symbol, finnhub_symbol


def parse(t212_ticker: str) -> Instrument:
    """Parse a Trading 212 ticker into an :class:`Instrument` using suffix
    heuristics only (no DB, no network).

    Unrecognised patterns yield ``instrument_type='unknown'`` with the symbol
    fields left ``None`` — those need a manual override.
    """
    m = _US_EQ.match(t212_ticker)
    if m:
        # Resolve any known successor ticker before deriving provider symbols.
        base = TICKER_ALIASES.get(m.group("base"), m.group("base"))
        yf_symbol, finnhub_symbol = _us_symbols(base)
        return Instrument(
            t212_ticker=t212_ticker,
            yf_symbol=yf_symbol,
            finnhub_symbol=finnhub_symbol,
            currency="USD",
            exchange="US",
            instrument_type="equity",
        )

    m = _INTL_EQ.match(t212_ticker)
    if m:
        base = m.group("base")
        marker = m.group("marker")
        suffix, exchange = _LISTING_SUFFIXES[marker]
        return Instrument(
            t212_ticker=t212_ticker,
            yf_symbol=base + suffix,
            # Finnhub free tier is US-only, so non-US listings have no
            # Finnhub symbol.
            finnhub_symbol=None,
            currency=None,
            exchange=exchange,
            instrument_type="equity",
        )

    # Unknown pattern — leave mapping null, flag for manual override.
    return Instrument(t212_ticker=t212_ticker, instrument_type="unknown")


def _instrument_from_row(row: dict[str, Any]) -> Instrument:
    return Instrument(
        t212_ticker=row["t212_ticker"],
        yf_symbol=row.get("yf_symbol"),
        finnhub_symbol=row.get("finnhub_symbol"),
        currency=row.get("currency"),
        exchange=row.get("exchange"),
        instrument_type=row.get("instrument_type") or "unknown",
    )


def _should_reheal(inst: Instrument) -> bool:
    """Whether a cached *auto* mapping should be re-parsed.

    Old auto rows created before a parser/alias improvement can carry a stale or
    missing mapping (e.g. ``MOG/A`` cached as ``unknown`` before slash support,
    or ``IPXX`` cached before its successor alias existed). We re-heal only when
    the mapping is clearly deficient or a successor alias now applies, so we
    never clobber legitimately enriched auto rows (e.g. an ETF whose type was
    filled in from Trading 212 metadata).
    """
    if inst.instrument_type == "unknown" or not inst.yf_symbol:
        return True
    m = _US_EQ.match(inst.t212_ticker)
    if m:
        base = m.group("base")
        alias = TICKER_ALIASES.get(base)
        if alias is not None and inst.yf_symbol != _us_symbols(alias)[0]:
            return True
    return False


def resolve(t212_ticker: str) -> Instrument:
    """Resolve a Trading 212 ticker to an :class:`Instrument`.

    Order: cache -> parse -> persist. A manual-override row in the cache is
    always returned as-is and is never re-parsed or overwritten. Auto-mapped
    rows are self-healed (re-parsed and rewritten) when :func:`_should_reheal`
    detects a stale mapping, so parser/alias improvements reach existing rows
    without a manual migration.
    """
    row = db.get_instrument(t212_ticker)
    if row is not None:
        inst = _instrument_from_row(row)
        # Manual mappings win unconditionally.
        if row.get("manual_override"):
            return inst
        # Self-heal a stale auto mapping if the parser now does better.
        if _should_reheal(inst):
            reparsed = parse(t212_ticker)
            if reparsed.yf_symbol and (
                reparsed.yf_symbol != inst.yf_symbol
                or reparsed.finnhub_symbol != inst.finnhub_symbol
                or reparsed.instrument_type != inst.instrument_type
            ):
                db.upsert_instrument(
                    t212_ticker=reparsed.t212_ticker,
                    yf_symbol=reparsed.yf_symbol,
                    finnhub_symbol=reparsed.finnhub_symbol,
                    currency=reparsed.currency,
                    exchange=reparsed.exchange,
                    instrument_type=reparsed.instrument_type,
                    manual_override=0,
                )
                return reparsed
        return inst

    inst = parse(t212_ticker)
    # Persist the auto-mapping (manual_override=0). upsert_instrument protects
    # any pre-existing manual mapping, though there is none on this path.
    db.upsert_instrument(
        t212_ticker=inst.t212_ticker,
        yf_symbol=inst.yf_symbol,
        finnhub_symbol=inst.finnhub_symbol,
        currency=inst.currency,
        exchange=inst.exchange,
        instrument_type=inst.instrument_type,
        manual_override=0,
    )
    return inst


def set_manual_mapping(
    t212_ticker: str,
    *,
    yf_symbol: str | None = None,
    finnhub_symbol: str | None = None,
    currency: str | None = None,
    exchange: str | None = None,
    instrument_type: str = "equity",
) -> Instrument:
    """Set (or overwrite) a manual instrument mapping.

    Manual mappings are marked ``manual_override=1`` and are protected from
    being clobbered by subsequent auto-mapping in :func:`resolve`.
    """
    db.upsert_instrument(
        t212_ticker=t212_ticker,
        yf_symbol=yf_symbol,
        finnhub_symbol=finnhub_symbol,
        currency=currency,
        exchange=exchange,
        instrument_type=instrument_type,
        manual_override=1,
    )
    return Instrument(
        t212_ticker=t212_ticker,
        yf_symbol=yf_symbol,
        finnhub_symbol=finnhub_symbol,
        currency=currency,
        exchange=exchange,
        instrument_type=instrument_type,
    )


# ---- Optional enrichment from a T212 metadata payload ---------------------
# Maps the T212 ``type`` field to our instrument_type vocabulary.
_T212_TYPE_MAP = {
    "ETF": "etf",
    "STOCK": "equity",
}


def enrich_from_t212(instruments_payload: list[dict[str, Any]]) -> int:
    """Fill in currency/type from a T212 ``/equity/metadata/instruments``
    payload (a list of dicts with keys like ``ticker``, ``type``,
    ``currencyCode``, ``isin``, ``name``).

    Works entirely offline from the passed payload — no HTTP here. Rows are
    persisted through :func:`app.db.upsert_instrument`, which will not
    overwrite manual-override rows. Returns the number of rows written.

    For each entry we first resolve() the ticker (parse + persist if new),
    then merge the T212-provided currency/type onto the mapping without
    disturbing the parsed symbol fields.
    """
    written = 0
    for entry in instruments_payload or []:
        ticker = entry.get("ticker")
        if not ticker:
            continue

        existing = db.get_instrument(ticker)
        if existing is not None and existing.get("manual_override"):
            continue  # never touch a manual mapping

        # Ensure a base auto-mapping exists (parse + persist for new tickers).
        inst = resolve(ticker)

        currency = entry.get("currencyCode") or inst.currency
        t212_type = (entry.get("type") or "").upper()
        instrument_type = _T212_TYPE_MAP.get(t212_type, inst.instrument_type)

        db.upsert_instrument(
            t212_ticker=ticker,
            yf_symbol=inst.yf_symbol,
            finnhub_symbol=inst.finnhub_symbol,
            currency=currency,
            exchange=inst.exchange,
            instrument_type=instrument_type,
            manual_override=0,
        )
        written += 1
    return written
