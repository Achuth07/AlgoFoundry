"""Technical-analysis leg tests (ALG-2).

All synthetic frames — no network. We assert against hand-checkable properties
(constructed uptrend -> positive score) and a Wilder RSI value computed against
the canonical Wilder worked example.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.longterm import technicals as ta


def _frame(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    if highs is None:
        highs = closes * 1.01
    if lows is None:
        lows = closes * 0.99
    if volumes is None:
        volumes = np.full(n, 1_000_000.0)
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes,
         "Volume": volumes},
        index=idx,
    )


# ---- RSI: canonical Wilder worked example --------------------------------
# Wilder's original 14-period example (from "New Concepts in Technical Trading
# Systems") yields an RSI of ~70.53 on the 15th close of this sequence.
_WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
    45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
]


def test_rsi_matches_wilder_reference():
    s = pd.Series(_WILDER_CLOSES)
    rsi = ta.rsi_wilder(s, 14)
    # Value on the 15th bar (index 14).
    assert rsi.iloc[14] == pytest.approx(70.53, abs=0.5)


def test_rsi_all_gains_is_100():
    s = pd.Series(np.arange(1.0, 40.0))  # strictly increasing
    rsi = ta.rsi_wilder(s, 14)
    assert rsi.iloc[-1] == pytest.approx(100.0, abs=1e-6)


# ---- Trend / score direction ---------------------------------------------
def test_constructed_uptrend_scores_positive():
    # Steady compounding uptrend over ~300 bars -> price>SMA50>SMA200, slopes+.
    closes = 100 * (1.004 ** np.arange(300))
    df = _frame(closes)
    res = ta.technical_score(df)
    assert res.status == "ok"
    assert res.score > 0
    assert res.summary["trend"]["sma200"] is not None
    assert res.summary["trend"]["price"] > res.summary["trend"]["sma50"]
    assert res.summary["trend"]["sma50"] > res.summary["trend"]["sma200"]


def test_constructed_downtrend_scores_negative():
    closes = 300 * (0.996 ** np.arange(300))
    df = _frame(closes)
    res = ta.technical_score(df)
    assert res.status == "ok"
    assert res.score < 0
    assert res.summary["trend"]["component"] < 0


def test_short_history_degrades_gracefully():
    # 120 bars: SMA200 unavailable, but scoring still works off SMA50.
    closes = 100 * (1.004 ** np.arange(120))
    df = _frame(closes)
    res = ta.technical_score(df)
    assert res.status == "ok"
    assert res.summary["trend"]["sma200"] is None
    assert any("SMA200" in note for note in res.summary["notes"])


def test_no_history_is_no_data():
    assert ta.technical_score(None).status == "no_data"
    assert ta.technical_score(_frame([100, 101, 102])).status == "no_data"


def test_drawdown_risk_modifier():
    # Rally then a >25% crash from the 52wk high -> drawdown risk penalty.
    up = 100 * (1.004 ** np.arange(200))
    down = up[-1] * (0.99 ** np.arange(1, 60))
    closes = np.concatenate([up, down])
    df = _frame(closes)
    res = ta.technical_score(df)
    assert res.summary["drawdown"]["drawdown_pct"] < -25
    assert res.summary["drawdown"]["risk_component"] <= -0.5


def test_indicator_snapshot_fields_present():
    closes = 100 * (1.002 ** np.arange(260))
    ind = ta.compute_indicators(_frame(closes))
    for key in ("rsi", "macd", "atr", "sma50", "sma200", "vol_trend",
                "drawdown_pct", "macd_cross"):
        assert key in ind


def test_case_insensitive_columns():
    closes = 100 * (1.003 ** np.arange(260))
    df = _frame(closes)
    df.columns = [c.lower() for c in df.columns]
    res = ta.technical_score(df)
    assert res.status == "ok"
