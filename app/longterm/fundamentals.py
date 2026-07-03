"""Fundamentals leg (ALG-13).

Pulls a compact fundamentals snapshot from yfinance ``Ticker.info`` (with an
optional cheap peek at ``.financials``), caches it read-through in the
``longterm_fundamentals_cache`` table (stale after 7 days), and maps the
metrics onto the shared -2..+2 :class:`LegResult` rubric.

yfinance fundamentals are famously patchy, so the rubric scores from whatever
fields are present and *notes* which components it had to skip. If fewer than
two rubric components are available the leg returns ``no_data`` rather than a
misleadingly-confident score.
"""

from __future__ import annotations

import time
from typing import Any

from .. import db
from .data_sources import LegResult

try:  # pragma: no cover - import guard
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None  # type: ignore

_CACHE_TTL_SECONDS = 7 * 24 * 3600  # stale after 7 days


# ---- Fetch + cache --------------------------------------------------------
def _extract_metrics(info: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we score from a yfinance ``info`` dict.

    Missing fields come through as ``None`` — the rubric handles that.
    """
    def g(*keys: str) -> Any:
        for k in keys:
            v = info.get(k)
            if v is not None:
                return v
        return None

    return {
        "trailing_pe": g("trailingPE"),
        "forward_pe": g("forwardPE"),
        "revenue_growth": g("revenueGrowth"),
        "earnings_growth": g("earningsGrowth"),
        "profit_margin": g("profitMargins"),
        "operating_margin": g("operatingMargins"),
        "debt_to_equity": g("debtToEquity"),
        "free_cash_flow": g("freeCashflow"),
        "recommendation_key": g("recommendationKey"),
        "recommendation_mean": g("recommendationMean"),
        "market_cap": g("marketCap"),
        "sector": g("sector"),
    }


def _fetch_from_yf(yf_symbol: str) -> dict[str, Any] | None:
    """Live yfinance fetch. Isolated so tests can monkeypatch it."""
    if yf is None:  # pragma: no cover
        raise RuntimeError("yfinance is not installed")
    ticker = yf.Ticker(yf_symbol)
    info = getattr(ticker, "info", None)
    if not info:
        return None
    return _extract_metrics(info)


def _fetch_from_av(symbol: str) -> dict[str, Any] | None:
    """Alpha Vantage OVERVIEW fallback for fundamentals.

    Maps AV field names to the same keys as ``_extract_metrics`` so the scoring
    rubric works identically regardless of source.
    """
    from .data_sources import _av_get

    av_symbol = symbol.split(".")[0] if "." in symbol else symbol
    data = _av_get({"function": "OVERVIEW", "symbol": av_symbol})
    if not data or "Symbol" not in data:
        return None

    def _safe_float(key: str) -> Any:
        v = data.get(key)
        if v is None or v == "None" or v == "-":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # AV reports D/E as a ratio (e.g. 1.5), but yfinance reports as a
    # percentage (e.g. 150). Convert to match yfinance convention.
    de = _safe_float("DebtEquityRatio")
    if de is not None:
        de = de * 100.0  # AV ratio -> yfinance percentage

    return {
        "trailing_pe": _safe_float("TrailingPE"),
        "forward_pe": _safe_float("ForwardPE"),
        "revenue_growth": _safe_float("QuarterlyRevenueGrowthYOY"),
        "earnings_growth": _safe_float("QuarterlyEarningsGrowthYOY"),
        "profit_margin": _safe_float("ProfitMargin"),
        "operating_margin": _safe_float("OperatingMarginTTM"),
        "debt_to_equity": de,
        "free_cash_flow": None,  # AV OVERVIEW doesn't carry FCF
        "recommendation_key": None,
        "recommendation_mean": _safe_float("AnalystTargetPrice"),
        "market_cap": _safe_float("MarketCapitalization"),
        "sector": data.get("Sector"),
    }


def fetch_fundamentals(
    yf_symbol: str, *, force_refresh: bool = False
) -> dict[str, Any] | None:
    """Return a fundamentals metrics dict for ``yf_symbol``.

    Read-through cache: a fresh (<7d) cached payload is returned without a
    network call. On a cache miss/staleness the live fetch is attempted and its
    result written back. If the live fetch fails, Alpha Vantage OVERVIEW is
    tried as a fallback. If both fail but a *stale* cache exists, the stale
    payload is returned (better than nothing for the AI step).
    """
    if not yf_symbol:
        return None

    cached = db.get_fundamentals_cache(yf_symbol)
    if cached and cached.get("payload") and not force_refresh:
        age = time.time() - (cached.get("fetched_ts") or 0)
        if age < _CACHE_TTL_SECONDS:
            return cached["payload"]

    # ---- Primary: yfinance ------------------------------------------------
    try:
        metrics = _fetch_from_yf(yf_symbol)
    except Exception as exc:
        db.log_event(
            "info", symbol=yf_symbol, status="no_data",
            detail=f"fetch_fundamentals yfinance error: {exc}",
        )
        metrics = None

    if metrics:
        db.set_fundamentals_cache(yf_symbol, metrics)
        return metrics

    # ---- Fallback: Alpha Vantage ------------------------------------------
    try:
        metrics = _fetch_from_av(yf_symbol)
    except Exception as exc:
        db.log_event(
            "info", symbol=yf_symbol, status="no_data",
            detail=f"fetch_fundamentals AV error: {exc}",
        )
        metrics = None

    if metrics:
        db.log_event(
            "info", symbol=yf_symbol, status="ok",
            detail="fetch_fundamentals: yfinance failed, Alpha Vantage succeeded",
        )
        db.set_fundamentals_cache(yf_symbol, metrics)
        return metrics

    # Both failed — fall back to a stale cache if present.
    if cached and cached.get("payload"):
        return cached["payload"]
    return None


# ---- Scoring rubric -------------------------------------------------------
def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fundamental_score(
    metrics: dict[str, Any] | None, instrument_type: str
) -> LegResult:
    """Map fundamentals onto -2..+2.

    Three components, each contributing to the total; missing inputs skip a
    component and are noted. ETFs are ``not_applicable``. Fewer than two usable
    components -> ``no_data``.

    * Valuation: forward PE vs trailing (improving earnings) + absolute PE band.
    * Growth: revenue growth (with earnings growth as a minor tie-breaker).
    * Quality: profit margin and debt/equity.
    """
    if instrument_type == "etf":
        return LegResult(
            status="not_applicable",
            detail="fundamentals leg does not apply to ETFs",
        )
    if not metrics:
        return LegResult(status="no_data", detail="no fundamentals available")

    summary: dict[str, Any] = {}
    notes: list[str] = []
    components_used = 0
    score = 0.0

    # --- Valuation --------------------------------------------------------
    tpe = _num(metrics.get("trailing_pe"))
    fpe = _num(metrics.get("forward_pe"))
    val_score = None
    if fpe is not None or tpe is not None:
        components_used += 1
        val_score = 0.0
        ref = fpe if fpe is not None else tpe  # prefer forward for the band
        # Absolute PE band (only meaningful for positive PE).
        if ref is not None and ref > 0:
            if ref < 15:
                val_score += 0.5
            elif ref < 25:
                val_score += 0.25
            elif ref > 40:
                val_score -= 0.5
        elif ref is not None and ref <= 0:
            # Negative earnings — a valuation red flag.
            val_score -= 0.5
            notes.append("negative/zero PE (loss-making)")
        # Forward vs trailing: forward < trailing implies earnings growth.
        if fpe is not None and tpe is not None and fpe > 0 and tpe > 0:
            if fpe < tpe:
                val_score += 0.5
            elif fpe > tpe:
                val_score -= 0.25
        val_score = max(-1.0, min(1.0, val_score))
        score += val_score
        summary["valuation"] = {
            "trailing_pe": tpe, "forward_pe": fpe, "component": val_score,
        }
    else:
        notes.append("valuation skipped: no PE data")

    # --- Growth -----------------------------------------------------------
    rev_g = _num(metrics.get("revenue_growth"))
    earn_g = _num(metrics.get("earnings_growth"))
    growth_score = None
    if rev_g is not None:
        components_used += 1
        growth_score = 0.0
        if rev_g >= 0.20:
            growth_score += 0.75
        elif rev_g >= 0.08:
            growth_score += 0.5
        elif rev_g >= 0.0:
            growth_score += 0.1
        elif rev_g < -0.05:
            growth_score -= 0.75
        else:
            growth_score -= 0.25
        # Earnings growth as a minor tie-breaker.
        if earn_g is not None:
            if earn_g > 0:
                growth_score += 0.25
            elif earn_g < 0:
                growth_score -= 0.25
        growth_score = max(-1.0, min(1.0, growth_score))
        score += growth_score
        summary["growth"] = {
            "revenue_growth": rev_g, "earnings_growth": earn_g,
            "component": growth_score,
        }
    else:
        notes.append("growth skipped: no revenue growth data")

    # --- Quality (margins + leverage) -------------------------------------
    margin = _num(metrics.get("profit_margin"))
    de = _num(metrics.get("debt_to_equity"))
    quality_score = None
    if margin is not None or de is not None:
        components_used += 1
        quality_score = 0.0
        if margin is not None:
            if margin >= 0.20:
                quality_score += 0.5
            elif margin >= 0.05:
                quality_score += 0.25
            elif margin < 0:
                quality_score -= 0.5
        if de is not None:
            # yfinance reports D/E as a percentage (e.g. 150 == 1.5x).
            de_ratio = de / 100.0 if de > 5 else de
            if de_ratio <= 0.5:
                quality_score += 0.5
            elif de_ratio <= 1.5:
                quality_score += 0.1
            elif de_ratio > 2.5:
                quality_score -= 0.5
        quality_score = max(-1.0, min(1.0, quality_score))
        score += quality_score
        summary["quality"] = {
            "profit_margin": margin, "debt_to_equity": de,
            "component": quality_score,
        }
    else:
        notes.append("quality skipped: no margin/leverage data")

    # Cross-check field for the AI step (not scored numerically here).
    summary["analyst_cross_check"] = {
        "recommendation_key": metrics.get("recommendation_key"),
        "recommendation_mean": metrics.get("recommendation_mean"),
    }
    summary["components_used"] = components_used
    summary["notes"] = notes

    if components_used < 2:
        return LegResult(
            status="no_data",
            summary=summary,
            detail=f"only {components_used} fundamental component(s) available",
        )

    score = max(-2.0, min(2.0, score))
    return LegResult(
        status="ok", score=round(score, 3), summary=summary,
        detail="fundamental score = valuation + growth + quality (clamped -2..+2)",
    )
