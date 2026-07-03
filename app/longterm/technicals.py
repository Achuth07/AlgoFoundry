"""Technical-analysis leg (ALG-2).

Pure pandas — no ``pandas-ta`` or other TA libraries. Given an OHLCV frame
(as produced by :func:`app.longterm.data_sources.fetch_ohlcv`), we compute a
standard indicator set and map it onto the shared -2..+2 :class:`LegResult`
rubric.

Indicators:
  * SMA50 / SMA200 and their 20-day slopes
  * RSI(14) using Wilder's smoothing
  * MACD(12,26,9) with a bullish/bearish cross state
  * ATR(14)
  * 20-day vs 60-day average-volume trend
  * drawdown from the 52-week high

The frame is expected to have (at least) columns ``Close``, ``High``, ``Low``,
``Volume`` — matching yfinance's output. Column lookup is case-insensitive so a
lower-cased test frame also works.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .data_sources import LegResult


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Case-insensitive column accessor (Close/close both work)."""
    for c in df.columns:
        if str(c).lower() == name.lower():
            return df[c]
    raise KeyError(f"column {name!r} not found in {list(df.columns)}")


# ---- Indicator primitives -------------------------------------------------
def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def slope(series: pd.Series, lookback: int = 20) -> float | None:
    """Per-bar slope of ``series`` over the last ``lookback`` points via a
    simple linear fit. Returns ``None`` if there isn't enough data."""
    s = series.dropna()
    if len(s) < lookback:
        return None
    y = s.iloc[-lookback:].to_numpy(dtype=float)
    x = list(range(lookback))
    n = lookback
    sx = sum(x)
    sy = float(y.sum())
    sxx = sum(i * i for i in x)
    sxy = float(sum(x[i] * y[i] for i in range(n)))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI using Wilder's smoothing (the classic definition).

    The first averaged gain/loss is a simple mean over ``period`` deltas; every
    subsequent value is smoothed as ``(prev*(period-1) + current)/period``.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing: the first averaged value is a simple mean over the
    # first ``period`` deltas (the SMA seed), then recursively smoothed as
    # avg = (avg_prev*(period-1) + current)/period. A plain adjust=False EWM
    # gets the seed wrong, so compute the recurrence explicitly.
    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # Where avg_loss == 0 the ratio is inf -> RSI 100; where both 0 -> NaN.
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's running average: SMA seed on the first ``period`` valid points,
    then ``avg = (avg_prev*(period-1) + current)/period``."""
    vals = series.to_numpy(dtype=float)
    out = pd.Series(index=series.index, dtype=float)
    # ``series`` here is a diff-derived series: index 0 is NaN. Find the first
    # ``period`` non-NaN entries to seed the SMA.
    valid = [i for i, v in enumerate(vals) if v == v]  # v==v filters NaN
    if len(valid) < period:
        return out  # all-NaN — not enough data
    seed_idx = valid[period - 1]
    seed = sum(vals[i] for i in valid[:period]) / period
    out.iloc[seed_idx] = seed
    prev = seed
    for i in range(seed_idx + 1, len(vals)):
        cur = vals[i]
        if cur != cur:  # NaN gap
            continue
        prev = (prev * (period - 1) + cur) / period
        out.iloc[i] = prev
    return out


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


# ---- Aggregate indicator snapshot ----------------------------------------
def compute_indicators(df: pd.DataFrame) -> dict[str, Any]:
    """Compute the full indicator snapshot from an OHLCV frame.

    Degrades gracefully when history is short: ``sma200`` (and its slope) are
    ``None`` when fewer than 200 rows are present, with a note recorded.
    """
    close = _col(df, "close").astype(float)
    high = _col(df, "high").astype(float)
    low = _col(df, "low").astype(float)
    try:
        volume = _col(df, "volume").astype(float)
    except KeyError:
        volume = None

    n = len(close)
    notes: list[str] = []
    price = float(close.iloc[-1]) if n else None

    sma50_series = sma(close, 50)
    sma200_series = sma(close, 200)
    sma50 = float(sma50_series.iloc[-1]) if n >= 50 and not pd.isna(
        sma50_series.iloc[-1]
    ) else None
    sma200 = float(sma200_series.iloc[-1]) if n >= 200 and not pd.isna(
        sma200_series.iloc[-1]
    ) else None
    if sma200 is None:
        notes.append("insufficient history for SMA200 (<200 rows)")

    sma50_slope = slope(sma50_series, 20) if sma50 is not None else None
    sma200_slope = slope(sma200_series, 20) if sma200 is not None else None

    rsi_series = rsi_wilder(close, 14)
    rsi = float(rsi_series.iloc[-1]) if n >= 15 and not pd.isna(
        rsi_series.iloc[-1]
    ) else None
    # RSI a few bars back, to detect "rising through 50".
    rsi_prev = (
        float(rsi_series.iloc[-4])
        if n >= 18 and not pd.isna(rsi_series.iloc[-4])
        else None
    )

    macd_line, signal_line, hist = macd(close)
    macd_val = float(macd_line.iloc[-1]) if n else None
    signal_val = float(signal_line.iloc[-1]) if n else None
    hist_val = float(hist.iloc[-1]) if n else None
    # Cross state: compare current vs previous histogram sign.
    cross = "none"
    if n >= 2 and not pd.isna(hist.iloc[-2]):
        prev_h = float(hist.iloc[-2])
        if prev_h <= 0 < hist_val:
            cross = "bullish"
        elif prev_h >= 0 > hist_val:
            cross = "bearish"
        elif hist_val > 0:
            cross = "bullish_hold"
        elif hist_val < 0:
            cross = "bearish_hold"

    atr_series = atr(high, low, close, 14)
    atr_val = float(atr_series.iloc[-1]) if n >= 15 and not pd.isna(
        atr_series.iloc[-1]
    ) else None
    atr_pct = (atr_val / price * 100.0) if atr_val and price else None
    # 6-month (~126 trading days) median ATR% as a volatility baseline.
    atr_pct_median = None
    if atr_val and price and n >= 21:
        atr_pct_series = (atr_series / close) * 100.0
        window = atr_pct_series.dropna().iloc[-126:]
        if len(window) >= 20:
            atr_pct_median = float(window.median())

    # Volume trend: 20d vs 60d average.
    vol20 = vol60 = vol_trend = None
    if volume is not None and n >= 60:
        vol20 = float(volume.iloc[-20:].mean())
        vol60 = float(volume.iloc[-60:].mean())
        if vol60:
            vol_trend = vol20 / vol60

    # Drawdown from 52-week (~252 trading days) high.
    window52 = close.iloc[-252:] if n >= 1 else close
    high52 = float(window52.max()) if len(window52) else None
    drawdown_pct = (
        (price - high52) / high52 * 100.0 if high52 and price else None
    )

    return {
        "n_rows": n,
        "price": price,
        "sma50": sma50,
        "sma200": sma200,
        "sma50_slope": sma50_slope,
        "sma200_slope": sma200_slope,
        "rsi": rsi,
        "rsi_prev": rsi_prev,
        "macd": macd_val,
        "macd_signal": signal_val,
        "macd_hist": hist_val,
        "macd_cross": cross,
        "atr": atr_val,
        "atr_pct": atr_pct,
        "atr_pct_median": atr_pct_median,
        "vol20": vol20,
        "vol60": vol60,
        "vol_trend": vol_trend,
        "high_52wk": high52,
        "drawdown_pct": drawdown_pct,
        "notes": notes,
    }


# ---- Scoring rubric -------------------------------------------------------
def technical_score(df: pd.DataFrame | None) -> LegResult:
    """Map an OHLCV frame onto the shared -2..+2 rubric.

    Trend (+/-1), momentum (+/-1), and a risk modifier (-1 max) are summed and
    clamped to [-2, +2]. See inline comments for the exact bands.
    """
    if df is None or len(df) == 0:
        return LegResult(status="no_data", detail="no price history")
    if len(df) < 30:
        # Not enough bars for RSI/MACD to be meaningful.
        return LegResult(
            status="no_data",
            detail=f"insufficient history ({len(df)} rows) for technical scoring",
        )

    ind = compute_indicators(df)
    price = ind["price"]
    sma50 = ind["sma50"]
    sma200 = ind["sma200"]
    rsi = ind["rsi"]
    cross = ind["macd_cross"]

    score = 0.0

    # --- Trend component (+/-1) -------------------------------------------
    trend = 0.0
    if sma50 is not None and sma200 is not None and price is not None:
        up_stack = price > sma50 > sma200
        down_stack = price < sma50 < sma200
        slopes_up = (ind["sma50_slope"] or 0) > 0 and (ind["sma200_slope"] or 0) > 0
        slopes_dn = (ind["sma50_slope"] or 0) < 0 and (ind["sma200_slope"] or 0) < 0
        if up_stack and slopes_up:
            trend = 1.0
        elif down_stack and slopes_dn:
            trend = -1.0
        elif up_stack:
            trend = 0.5
        elif down_stack:
            trend = -0.5
    elif sma50 is not None and price is not None:
        # Short-history fallback: use SMA50 only.
        if price > sma50 and (ind["sma50_slope"] or 0) > 0:
            trend = 0.5
        elif price < sma50 and (ind["sma50_slope"] or 0) < 0:
            trend = -0.5
    score += trend

    # --- Momentum component (+/-1) ----------------------------------------
    momentum = 0.0
    if rsi is not None:
        rising_through_50 = (
            ind["rsi_prev"] is not None
            and ind["rsi_prev"] < 50 <= rsi
        )
        if rsi < 30 and cross in ("bearish", "bearish_hold"):
            momentum = -1.0
        elif rsi < 40:
            momentum = -0.5
        elif rising_through_50 and cross in ("bullish", "bullish_hold"):
            momentum = 1.0
        elif 50 <= rsi <= 70:
            momentum = 0.5
        elif rsi > 70:
            # Overbought: mildly cautionary rather than bullish.
            momentum = 0.0
    momentum = max(-1.0, min(1.0, momentum))
    score += momentum

    # --- Risk modifier (down only, up to -1) ------------------------------
    risk = 0.0
    dd = ind["drawdown_pct"]
    if dd is not None and dd < -25.0:
        risk -= 0.5
    atr_pct = ind["atr_pct"]
    atr_med = ind["atr_pct_median"]
    if atr_pct is not None and atr_med is not None and atr_pct > atr_med * 1.5:
        # Volatility elevated vs its own 6-month baseline.
        risk -= 0.5
    score += risk

    score = max(-2.0, min(2.0, score))

    summary = {
        "trend": {
            "component": trend,
            "price": price,
            "sma50": sma50,
            "sma200": sma200,
            "sma50_slope": ind["sma50_slope"],
            "sma200_slope": ind["sma200_slope"],
        },
        "momentum": {
            "component": momentum,
            "rsi": rsi,
            "macd_cross": cross,
            "macd_hist": ind["macd_hist"],
        },
        "volatility": {
            "atr": ind["atr"],
            "atr_pct": atr_pct,
            "atr_pct_median": atr_med,
            "vol_trend": ind["vol_trend"],
        },
        "drawdown": {
            "drawdown_pct": dd,
            "high_52wk": ind["high_52wk"],
            "risk_component": risk,
        },
        "notes": ind["notes"],
    }
    detail = "technical score = trend + momentum + risk modifier (clamped -2..+2)"
    return LegResult(status="ok", score=round(score, 3), summary=summary, detail=detail)
