"""Instrument mapping tests (ALG-12)."""

from __future__ import annotations


def test_us_equity(instruments):
    inst = instruments.resolve("AAPL_US_EQ")
    assert inst.yf_symbol == "AAPL"
    assert inst.finnhub_symbol == "AAPL"
    assert inst.instrument_type == "equity"
    assert inst.currency == "USD"
    assert inst.exchange == "US"


def test_london_etf_symbol(instruments):
    inst = instruments.resolve("VUAGl_EQ")
    assert inst.yf_symbol == "VUAG.L"
    assert inst.finnhub_symbol is None  # free tier is US-only
    assert inst.exchange == "London"


def test_london_equity_barclays(instruments):
    inst = instruments.resolve("BARCl_EQ")
    assert inst.yf_symbol == "BARC.L"
    assert inst.finnhub_symbol is None


def test_other_european_listings(instruments):
    assert instruments.parse("SAPd_EQ").yf_symbol == "SAP.DE"
    assert instruments.parse("ASMLa_EQ").yf_symbol == "ASML.AS"
    assert instruments.parse("MCp_EQ").yf_symbol == "MC.PA"


def test_unknown_ticker(instruments):
    inst = instruments.resolve("WEIRD-TICKER")
    assert inst.instrument_type == "unknown"
    assert inst.yf_symbol is None
    assert inst.finnhub_symbol is None


def test_resolve_is_read_through_cached(instruments, db):
    inst = instruments.resolve("AAPL_US_EQ")
    assert inst.yf_symbol == "AAPL"
    # It should now be persisted.
    row = db.get_instrument("AAPL_US_EQ")
    assert row is not None
    assert row["yf_symbol"] == "AAPL"
    assert row["manual_override"] == 0


def test_manual_override_survives_resolve(instruments, db):
    instruments.set_manual_mapping(
        "WEIRD-TICKER", yf_symbol="XYZ", finnhub_symbol="XYZ",
        currency="USD", exchange="US", instrument_type="equity",
    )
    # resolve() must return the manual mapping, not re-parse to 'unknown'.
    inst = instruments.resolve("WEIRD-TICKER")
    assert inst.instrument_type == "equity"
    assert inst.yf_symbol == "XYZ"
    row = db.get_instrument("WEIRD-TICKER")
    assert row["manual_override"] == 1


def test_manual_mapping_not_clobbered_by_resolve(instruments, db):
    # A US ticker would auto-map to AAPL, but a manual mapping wins.
    instruments.set_manual_mapping("AAPL_US_EQ", yf_symbol="AAPL.CUSTOM")
    inst = instruments.resolve("AAPL_US_EQ")
    assert inst.yf_symbol == "AAPL.CUSTOM"


def test_enrich_from_t212(instruments, db):
    payload = [
        {"ticker": "AAPL_US_EQ", "type": "STOCK", "currencyCode": "USD",
         "name": "Apple Inc"},
        {"ticker": "VUAGl_EQ", "type": "ETF", "currencyCode": "GBX",
         "name": "Vanguard S&P 500"},
    ]
    written = instruments.enrich_from_t212(payload)
    assert written == 2

    aapl = db.get_instrument("AAPL_US_EQ")
    assert aapl["instrument_type"] == "equity"
    assert aapl["currency"] == "USD"
    assert aapl["yf_symbol"] == "AAPL"

    vuag = db.get_instrument("VUAGl_EQ")
    assert vuag["instrument_type"] == "etf"
    assert vuag["currency"] == "GBX"
    assert vuag["yf_symbol"] == "VUAG.L"


def test_enrich_skips_manual_override(instruments, db):
    instruments.set_manual_mapping(
        "AAPL_US_EQ", yf_symbol="AAPL.CUSTOM", instrument_type="equity",
    )
    instruments.enrich_from_t212(
        [{"ticker": "AAPL_US_EQ", "type": "ETF", "currencyCode": "EUR"}]
    )
    row = db.get_instrument("AAPL_US_EQ")
    assert row["yf_symbol"] == "AAPL.CUSTOM"
    assert row["manual_override"] == 1


# ---- Share-class (slash) tickers -----------------------------------------
def test_us_share_class_slash(instruments):
    # Moog Inc. Class A: T212 uses a slash, yfinance a hyphen, Finnhub a dot.
    inst = instruments.resolve("MOG/A_US_EQ")
    assert inst.yf_symbol == "MOG-A"
    assert inst.finnhub_symbol == "MOG.A"
    assert inst.instrument_type == "equity"
    assert inst.currency == "USD"


def test_us_share_class_parse_only(instruments):
    inst = instruments.parse("BRK/B_US_EQ")
    assert inst.yf_symbol == "BRK-B"
    assert inst.finnhub_symbol == "BRK.B"


# ---- Successor / alias resolution ----------------------------------------
def test_ticker_alias_maps_to_successor(instruments):
    # IPXX merged into USAR; the legacy ticker should resolve to the successor.
    inst = instruments.resolve("IPXX_US_EQ")
    assert inst.yf_symbol == "USAR"
    assert inst.finnhub_symbol == "USAR"
    assert inst.instrument_type == "equity"


def test_reheal_upgrades_stale_unknown_row(instruments, db):
    # Simulate an auto row cached before slash support: 'unknown' with no symbol.
    db.upsert_instrument(
        t212_ticker="MOG/A_US_EQ", yf_symbol=None, finnhub_symbol=None,
        currency=None, exchange=None, instrument_type="unknown",
        manual_override=0,
    )
    inst = instruments.resolve("MOG/A_US_EQ")
    assert inst.yf_symbol == "MOG-A"
    # The stale row is healed in place.
    row = db.get_instrument("MOG/A_US_EQ")
    assert row["yf_symbol"] == "MOG-A"
    assert row["instrument_type"] == "equity"


def test_reheal_applies_new_alias_to_old_row(instruments, db):
    # Simulate a pre-alias auto row that mapped IPXX to itself.
    db.upsert_instrument(
        t212_ticker="IPXX_US_EQ", yf_symbol="IPXX", finnhub_symbol="IPXX",
        currency="USD", exchange="US", instrument_type="equity",
        manual_override=0,
    )
    inst = instruments.resolve("IPXX_US_EQ")
    assert inst.yf_symbol == "USAR"
    row = db.get_instrument("IPXX_US_EQ")
    assert row["yf_symbol"] == "USAR"


def test_reheal_never_touches_manual_override(instruments, db):
    instruments.set_manual_mapping(
        "IPXX_US_EQ", yf_symbol="IPXX", finnhub_symbol="IPXX",
        currency="USD", exchange="US", instrument_type="equity",
    )
    inst = instruments.resolve("IPXX_US_EQ")
    # Manual mapping is preserved, alias does not override it.
    assert inst.yf_symbol == "IPXX"
    row = db.get_instrument("IPXX_US_EQ")
    assert row["manual_override"] == 1


def test_reheal_leaves_healthy_etf_row_untouched(instruments, db):
    # A London ETF enriched to type 'etf' must not be reparsed back to 'equity'.
    db.upsert_instrument(
        t212_ticker="VUAGl_EQ", yf_symbol="VUAG.L", finnhub_symbol=None,
        currency="GBX", exchange="London", instrument_type="etf",
        manual_override=0,
    )
    inst = instruments.resolve("VUAGl_EQ")
    assert inst.instrument_type == "etf"
    assert inst.yf_symbol == "VUAG.L"
