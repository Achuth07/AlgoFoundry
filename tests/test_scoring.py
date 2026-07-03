"""Composite scoring + decision engine tests (ALG-5).

All pure-function tests: LegResults are synthesised directly and ScoringConfig
is constructed in-test, so no DB is required except the one end-to-end
``from_settings`` check.
"""

from __future__ import annotations

import datetime as _dt
import importlib

import pytest

from app.longterm.data_sources import LegResult
from app.longterm import scoring


def _cfg(**over) -> scoring.ScoringConfig:
    base = dict(
        weight_technical=1.0, weight_fundamental=1.0, weight_analyst=1.0,
        weight_news=1.0, threshold_buy=0.5, threshold_sell=0.75,
        hysteresis_days=2, hysteresis_margin=0.15, earnings_freeze_days=3,
        max_drawdown_pct=25.0,
    )
    base.update(over)
    return scoring.ScoringConfig(**base)


def _ok(score, summary=None):
    return LegResult(status="ok", score=score, summary=summary or {})


def _na():
    return LegResult(status="not_applicable")


def _nodata():
    return LegResult(status="no_data")


# ---------------------------------------------------------------------------
# compute_composite
# ---------------------------------------------------------------------------
def test_composite_weighted_mean_all_legs():
    cfg = _cfg()
    legs = {
        "technical": _ok(2.0),
        "fundamental": _ok(0.0),
        "analyst": _ok(1.0),
        "news": _ok(-1.0),
    }
    comp, dq, used = scoring.compute_composite(legs, cfg.weights)
    assert comp == pytest.approx((2 + 0 + 1 - 1) / 4)
    assert dq == "full"
    assert set(used) == {"technical", "fundamental", "analyst", "news"}


def test_composite_stays_in_range():
    cfg = _cfg()
    legs = {"technical": _ok(2.0), "fundamental": _ok(2.0)}
    comp, dq, used = scoring.compute_composite(legs, cfg.weights)
    assert comp == 2.0  # weighted mean of two +2 legs, not a sum


def test_composite_etf_technical_only_is_full():
    """ETF: fundamental/analyst/news are not_applicable, technical ok. Because
    not_applicable legs don't count against fullness, data_quality is 'full'."""
    cfg = _cfg()
    legs = {
        "technical": _ok(1.0),
        "fundamental": _na(),
        "analyst": _na(),
        "news": _na(),
    }
    comp, dq, used = scoring.compute_composite(legs, cfg.weights)
    assert comp == 1.0
    assert dq == "full"
    assert used == ["technical"]


def test_composite_partial_when_applicable_leg_missing():
    cfg = _cfg()
    legs = {
        "technical": _ok(1.0),
        "fundamental": _nodata(),  # applicable but missing
        "analyst": _na(),
        "news": _na(),
    }
    comp, dq, used = scoring.compute_composite(legs, cfg.weights)
    assert dq == "partial_data"
    assert used == ["technical"]


def test_composite_no_data_path():
    cfg = _cfg()
    legs = {"technical": _nodata(), "fundamental": _nodata()}
    comp, dq, used = scoring.compute_composite(legs, cfg.weights)
    assert comp is None
    assert dq == "no_data"
    assert used == []


def test_composite_reweighting_respects_weights():
    cfg = _cfg(weight_technical=3.0, weight_news=1.0)
    legs = {"technical": _ok(2.0), "news": _ok(-2.0)}
    comp, dq, used = scoring.compute_composite(legs, cfg.weights)
    # (3*2 + 1*-2) / (3+1) = 4/4 = 1.0
    assert comp == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# decide — band mapping + asymmetry
# ---------------------------------------------------------------------------
def test_band_buy():
    cfg = _cfg()
    label, conf, flags = scoring.decide(0.6, [], cfg)
    assert label == "BUY"


def test_band_hold_between_thresholds():
    cfg = _cfg()
    # 0.6 magnitude on the negative side is NOT enough for SELL (needs 0.75).
    label, conf, flags = scoring.decide(-0.6, [], cfg)
    assert label == "HOLD"


def test_band_sell_asymmetry():
    cfg = _cfg()
    # prev already SELL (no hysteresis flip) + falling trend -> SELL stays.
    # History newest-first; oldest->newest is -0.4, -0.6, -0.8 (falling).
    hist = [
        {"label": "SELL", "composite": -0.6},
        {"label": "SELL", "composite": -0.4},
    ]
    label, conf, flags = scoring.decide(-0.8, hist, cfg)
    assert label == "SELL"


# ---------------------------------------------------------------------------
# decide — hysteresis
# ---------------------------------------------------------------------------
def test_hysteresis_hold_blocks_flip():
    cfg = _cfg(hysteresis_days=2, hysteresis_margin=0.15)
    # Was BUY; today raw maps to HOLD (0.4) but neither margin nor 2-consec-day
    # rule is satisfied -> hold previous BUY.
    hist = [{"label": "BUY", "composite": 0.55}]
    label, conf, flags = scoring.decide(0.45, hist, cfg)
    assert label == "BUY"
    assert "hysteresis_hold" in flags


def test_hysteresis_margin_crossing_allows_change():
    cfg = _cfg(hysteresis_days=5, hysteresis_margin=0.15)
    # Was HOLD; composite 0.70 >= buy(0.5) + margin(0.15) -> allowed to flip BUY.
    hist = [{"label": "HOLD", "composite": 0.2}]
    label, conf, flags = scoring.decide(0.70, hist, cfg)
    assert label == "BUY"
    assert "hysteresis_hold" not in flags


def test_hysteresis_consecutive_days_allows_change():
    cfg = _cfg(hysteresis_days=2, hysteresis_margin=0.5)
    # Margin path is blocked (0.55 < 0.5+0.5), but the raw band was BUY
    # yesterday too -> 2 consecutive BUY days -> allowed to flip.
    hist = [{"label": "HOLD", "composite": 0.55}]
    label, conf, flags = scoring.decide(0.55, hist, cfg)
    assert label == "BUY"
    assert "hysteresis_hold" not in flags


# ---------------------------------------------------------------------------
# decide — trend guard on SELL
# ---------------------------------------------------------------------------
def test_sell_suppressed_by_flat_trend():
    cfg = _cfg()
    # Level says SELL (-0.8 <= -0.75) but the composite series is rising.
    hist = [
        {"label": "HOLD", "composite": -0.9},
        {"label": "HOLD", "composite": -1.0},
        {"label": "HOLD", "composite": -1.1},
    ]
    label, conf, flags = scoring.decide(-0.8, hist, cfg)
    assert label == "HOLD"
    assert "sell_suppressed_trend" in flags


def test_deep_sell_bypasses_trend():
    cfg = _cfg(threshold_sell=0.75, hysteresis_margin=0.15)
    # deep threshold = -(0.75 + 2*0.15) = -1.05. Composite -1.2 is deep enough
    # to SELL even with a flat/rising trend.
    hist = [
        {"label": "SELL", "composite": -1.3},
        {"label": "SELL", "composite": -1.4},
    ]
    label, conf, flags = scoring.decide(-1.2, hist, cfg)
    assert label == "SELL"
    assert "sell_suppressed_trend" not in flags


def test_sell_emitted_on_falling_trend():
    cfg = _cfg()
    # prev already SELL (no hysteresis flip). Newest-first history; oldest->
    # newest is 0.1, -0.2, -0.5, -0.8 i.e. steadily falling -> SELL kept.
    hist = [
        {"label": "SELL", "composite": -0.5},
        {"label": "SELL", "composite": -0.2},
        {"label": "SELL", "composite": 0.1},
    ]
    label, conf, flags = scoring.decide(-0.8, hist, cfg)
    assert label == "SELL"


# ---------------------------------------------------------------------------
# confidence + divergence
# ---------------------------------------------------------------------------
def test_confidence_base():
    cfg = _cfg()
    _, conf, _ = scoring.decide(1.0, [], cfg)
    assert conf == pytest.approx(0.5)  # |1|/2


def test_divergence_halves_confidence():
    legs = {"technical": _ok(2.0), "news": _ok(-1.0)}  # divergence 3.0 >= 2.5
    conf, flags = scoring.apply_divergence(0.8, [], legs)
    assert conf == pytest.approx(0.4)
    assert "high_divergence" in flags


def test_no_divergence_flag_when_close():
    legs = {"technical": _ok(1.0), "news": _ok(0.5)}  # divergence 0.5
    conf, flags = scoring.apply_divergence(0.8, [], legs)
    assert conf == pytest.approx(0.8)
    assert "high_divergence" not in flags


# ---------------------------------------------------------------------------
# vetoes
# ---------------------------------------------------------------------------
def test_veto_earnings_freeze_reverts_change():
    cfg = _cfg(earnings_freeze_days=3)
    ctx = {
        "next_earnings_date": _dt.date.today() + _dt.timedelta(days=1),
        "prev_label": "HOLD",
    }
    label, flags = scoring.apply_vetoes("BUY", [], ctx, cfg)
    assert label == "HOLD"  # change reverted
    assert "earnings_freeze" in flags


def test_veto_earnings_freeze_keeps_unchanged_label():
    cfg = _cfg(earnings_freeze_days=3)
    ctx = {
        "next_earnings_date": _dt.date.today() + _dt.timedelta(days=2),
        "prev_label": "BUY",
    }
    label, flags = scoring.apply_vetoes("BUY", [], ctx, cfg)
    assert label == "BUY"
    assert "earnings_freeze" in flags


def test_veto_earnings_freeze_ignores_distant_earnings():
    cfg = _cfg(earnings_freeze_days=3)
    ctx = {
        "next_earnings_date": _dt.date.today() + _dt.timedelta(days=10),
        "prev_label": "HOLD",
    }
    label, flags = scoring.apply_vetoes("BUY", [], ctx, cfg)
    assert label == "BUY"
    assert "earnings_freeze" not in flags


def test_veto_materiality_manual_review():
    cfg = _cfg()
    for mat in ("ma", "regulatory"):
        label, flags = scoring.apply_vetoes("HOLD", [], {"ai_materiality": mat}, cfg)
        assert "manual_review" in flags
    # Non-material materiality doesn't trip it.
    label, flags = scoring.apply_vetoes("HOLD", [], {"ai_materiality": "earnings"}, cfg)
    assert "manual_review" not in flags


def test_veto_override_candidate_manual_review():
    cfg = _cfg()
    label, flags = scoring.apply_vetoes("HOLD", [], {"override_candidate": True}, cfg)
    assert "manual_review" in flags


def test_veto_drawdown_review():
    cfg = _cfg(max_drawdown_pct=25.0)
    label, flags = scoring.apply_vetoes("HOLD", [], {"drawdown_pct_vs_cost": 30.0}, cfg)
    assert "drawdown_review" in flags
    label, flags = scoring.apply_vetoes("HOLD", [], {"drawdown_pct_vs_cost": 10.0}, cfg)
    assert "drawdown_review" not in flags


def test_veto_never_invents_buy_or_sell():
    cfg = _cfg()
    # Even with material news + drawdown, a HOLD stays HOLD (vetoes only add
    # flags / freeze, never invent a directional call).
    ctx = {"ai_materiality": "ma", "drawdown_pct_vs_cost": 40.0}
    label, flags = scoring.apply_vetoes("HOLD", [], ctx, cfg)
    assert label == "HOLD"


# ---------------------------------------------------------------------------
# rationale
# ---------------------------------------------------------------------------
def test_rationale_contains_label_and_facts():
    legs = {
        "technical": _ok(1.0, {"trend": {"component": 1.0}, "momentum": {"rsi": 60}}),
        "fundamental": _ok(0.5, {"valuation": {"forward_pe": 20.0},
                                 "growth": {"revenue_growth": 0.15}}),
        "news": _ok(1.0, {}),
    }
    facts = ["Acme raised guidance", "Cloud revenue up 30%"]
    text = scoring.render_rationale("BUY", legs, facts, ["manual_review"])
    assert text.startswith("Verdict: BUY.")
    assert "Acme raised guidance" in text
    assert "Technicals" in text
    assert "manual review" in text.lower()


def test_rationale_skips_non_ok_legs_and_never_contradicts():
    legs = {"technical": _ok(-1.0, {}), "fundamental": _nodata()}
    text = scoring.render_rationale("SELL", legs, [], [])
    assert "Verdict: SELL." in text
    # The no_data fundamental leg is not described.
    assert "Fundamentals" not in text
    # No stray BUY appears when the verdict is SELL.
    assert "BUY" not in text


# ---------------------------------------------------------------------------
# evaluate_holding end-to-end
# ---------------------------------------------------------------------------
class _FakeAI:
    status = "ok"
    news_score = 1.0
    key_facts = ["Acme beat earnings"]
    materiality = "earnings"
    override_candidate = False
    model_used = "primary/model:free"
    raw_response = '{"news_score": 1}'


def test_evaluate_holding_end_to_end():
    cfg = _cfg()
    legs = {
        "technical": _ok(1.0, {"trend": {"component": 1.0}, "momentum": {"rsi": 60}}),
        "fundamental": _ok(1.0, {"valuation": {"forward_pe": 18.0},
                                 "growth": {"revenue_growth": 0.2}}),
        "analyst": _ok(1.0, {"consensus_ratio": 0.5}),
        "news": _ok(1.0, {}),
    }
    out = scoring.evaluate_holding(
        "ACME", legs, cfg,
        history=[],
        ai_result=_FakeAI(),
        price_at_verdict=190.0,
    )
    assert out["symbol"] == "ACME"
    assert out["label"] == "BUY"  # composite 1.0 >= 0.5
    assert out["composite"] == pytest.approx(1.0)
    assert out["data_quality"] == "full"
    assert out["score_technical"] == 1.0
    assert out["model_used"] == "primary/model:free"
    assert out["prompt_version"] == "1.0"
    assert out["raw_ai_response"] == '{"news_score": 1}'
    assert "Verdict: BUY." in out["rationale"]
    # Shape must be acceptable to db.upsert_verdict (allowed keys superset).
    assert set(out).issuperset(
        {"composite", "label", "confidence", "rationale", "data_quality"}
    )


def test_evaluate_holding_material_sets_manual_review():
    cfg = _cfg()

    class _AI(_FakeAI):
        materiality = "regulatory"
        override_candidate = True

    legs = {"technical": _ok(1.0, {}), "fundamental": _ok(1.0, {})}
    out = scoring.evaluate_holding("ACME", legs, cfg, ai_result=_AI())
    assert "manual_review" in out["review_flags"]
    assert out["override_flag"] == 1


# ---------------------------------------------------------------------------
# ScoringConfig.from_settings (only DB-touching test)
# ---------------------------------------------------------------------------
def test_config_from_settings(db):
    import app.longterm.scoring as sc

    sc = importlib.reload(sc)
    cfg = sc.ScoringConfig.from_settings(db)
    assert cfg.threshold_buy == 0.5
    assert cfg.threshold_sell == 0.75
    assert cfg.hysteresis_days == 2
    assert cfg.max_drawdown_pct == 25.0
