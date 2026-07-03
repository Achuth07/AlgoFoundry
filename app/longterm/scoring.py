"""Composite scoring + decision engine (ALG-5).

Pure, DB-free functions. Everything a decision needs is passed in explicitly so
each piece is trivially unit-testable. The only stateful concept is
:class:`ScoringConfig`, which is a plain dataclass; ``ScoringConfig.from_settings``
is the sole (thin) bridge to ``app.db`` and is never called by the pure logic.

Scales
------
Every leg score and the composite live on the same **-2..+2** scale. The
composite is a *weighted mean* of the available leg scores, so it stays inside
[-2, +2] no matter how many legs are present. Thresholds in the config are read
on this same scale.

Bands are **asymmetric**: ``threshold_sell`` (default 0.75) is larger than
``threshold_buy`` (default 0.5), which reads as "the SELL band sits further from
zero" — a negative signal must be stronger to trip a SELL than a positive one
needs to be to trip a BUY.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .data_sources import LegResult

try:  # keep scoring importable even if ai_research's deps are missing
    from .ai_research import PROMPT_VERSION as _PROMPT_VERSION
except Exception:  # pragma: no cover
    _PROMPT_VERSION = "1.0"

# The four legs, in the canonical order used for rationale + persistence.
LEG_KEYS = ("technical", "fundamental", "analyst", "news")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class ScoringConfig:
    weight_technical: float = 1.0
    weight_fundamental: float = 1.0
    weight_analyst: float = 1.0
    weight_news: float = 1.0
    threshold_buy: float = 0.5
    threshold_sell: float = 0.75
    hysteresis_days: int = 2
    hysteresis_margin: float = 0.15
    earnings_freeze_days: int = 3
    max_drawdown_pct: float = 25.0

    @property
    def weights(self) -> dict[str, float]:
        return {
            "technical": self.weight_technical,
            "fundamental": self.weight_fundamental,
            "analyst": self.weight_analyst,
            "news": self.weight_news,
        }

    @classmethod
    def from_settings(cls, db) -> "ScoringConfig":
        """Build a config from the settings table via an ``app.db``-like module.

        Passed the module explicitly (rather than importing it) so tests can
        hand in the temp-DB-bound reload from the conftest fixture.
        """
        g = db.get_setting
        return cls(
            weight_technical=float(g("lt_weight_technical", 1.0)),
            weight_fundamental=float(g("lt_weight_fundamental", 1.0)),
            weight_analyst=float(g("lt_weight_analyst", 1.0)),
            weight_news=float(g("lt_weight_news", 1.0)),
            threshold_buy=float(g("lt_threshold_buy", 0.5)),
            threshold_sell=float(g("lt_threshold_sell", 0.75)),
            hysteresis_days=int(g("lt_hysteresis_days", 2)),
            hysteresis_margin=float(g("lt_hysteresis_margin", 0.15)),
            earnings_freeze_days=int(g("lt_earnings_freeze_days", 3)),
            max_drawdown_pct=float(g("lt_max_drawdown_pct", 25.0)),
        )


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
def compute_composite(
    legs: dict[str, LegResult], weights: dict[str, float]
) -> tuple[float | None, str, list[str]]:
    """Combine leg scores into a single composite on the -2..+2 scale.

    Only ``status == 'ok'`` legs contribute. The composite is the weighted mean
    of the contributing legs' scores using ``weights`` restricted to those legs
    (so the result stays normalised to [-2, +2] regardless of how many legs are
    present).

    ``data_quality`` (an *applicable* leg is one whose status is not
    ``not_applicable``):
      * ``full``          — every applicable leg is ``ok``.
      * ``partial_data``  — at least one applicable leg is missing but >=1 ok.
      * ``no_data``       — no leg is ok.

    Returns ``(composite | None, data_quality, legs_used)``.
    """
    ok_legs: list[str] = []
    applicable = 0
    num = 0.0
    denom = 0.0

    for key in LEG_KEYS:
        leg = legs.get(key)
        if leg is None:
            continue
        if leg.status == "not_applicable":
            # Not applicable never counts for or against data fullness.
            continue
        applicable += 1
        if leg.status == "ok" and leg.score is not None:
            w = float(weights.get(key, 1.0))
            # A zero weight means the operator disabled the leg: don't let it
            # count as "used" (it contributes nothing and shouldn't skew data
            # quality either way — but it *is* applicable, so treat missing).
            if w == 0:
                continue
            ok_legs.append(key)
            num += w * leg.score
            denom += w

    if not ok_legs or denom == 0:
        return None, "no_data", []

    composite = num / denom
    composite = max(-2.0, min(2.0, composite))

    if len(ok_legs) == applicable:
        data_quality = "full"
    else:
        data_quality = "partial_data"

    return round(composite, 4), data_quality, ok_legs


# ---------------------------------------------------------------------------
# Decision (band + hysteresis + trend + confidence)
# ---------------------------------------------------------------------------
def _raw_band(composite: float, cfg: ScoringConfig) -> str:
    """Raw level->label mapping, ignoring history/trend."""
    if composite <= -cfg.threshold_sell:
        return "SELL"
    if composite >= cfg.threshold_buy:
        return "BUY"
    return "HOLD"


def _slope(values: list[float]) -> float:
    """Least-squares slope of ``values`` (index as x). 0 if <2 points."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    sx = sum(xs)
    sy = sum(values)
    sxx = sum(x * x for x in xs)
    sxy = sum(xs[i] * values[i] for i in range(n))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def decide(
    composite: float | None,
    history: list[dict],
    cfg: ScoringConfig,
) -> tuple[str, float, list[str]]:
    """Turn a composite + prior verdicts into a (label, confidence, flags).

    ``history`` is a list of prior verdict dicts, **most recent first**, each
    carrying at least ``label`` and ``composite``.

    Layers, in order:
      1. Raw band mapping (asymmetric — see module docstring).
      2. Hysteresis: a change away from the previous label is allowed only if
         the composite crosses the new band by ``hysteresis_margin`` beyond the
         threshold, OR the raw band has produced the same new label for
         ``hysteresis_days`` consecutive days (today included). Otherwise the
         previous label is held with flag ``hysteresis_hold``.
      3. Trend guard on SELL: a level-SELL is only emitted if the slope of the
         last 5-10 composites (today included) is negative, or the composite is
         *deeply* below the band (<= -(threshold_sell + 2*margin)). Otherwise it
         becomes HOLD with flag ``sell_suppressed_trend``.
      4. Confidence: base = min(1, |composite|/2), halved (with flag
         ``high_divergence``) when leg divergence is large — see
         :func:`_apply_divergence` which the caller layers on separately.
    """
    flags: list[str] = []

    if composite is None:
        return "HOLD", 0.0, ["no_data"]

    raw = _raw_band(composite, cfg)
    prev_label = history[0]["label"] if history else None

    label = raw

    # --- Hysteresis -------------------------------------------------------
    if prev_label is not None and raw != prev_label:
        allow_change = False

        # (a) margin crossing beyond the relevant threshold.
        if raw == "BUY" and composite >= cfg.threshold_buy + cfg.hysteresis_margin:
            allow_change = True
        elif raw == "SELL" and composite <= -(cfg.threshold_sell + cfg.hysteresis_margin):
            allow_change = True
        elif raw == "HOLD":
            # Moving *into* HOLD from a directional call: require the composite
            # to have retreated a margin's-worth inside the neutral zone.
            if prev_label == "BUY" and composite <= cfg.threshold_buy - cfg.hysteresis_margin:
                allow_change = True
            elif prev_label == "SELL" and composite >= -(cfg.threshold_sell - cfg.hysteresis_margin):
                allow_change = True

        # (b) same raw band for hysteresis_days consecutive days (incl. today).
        if not allow_change:
            consec = 1  # today
            for prior in history[: max(0, cfg.hysteresis_days - 1)]:
                prior_comp = prior.get("composite")
                if prior_comp is None:
                    break
                if _raw_band(float(prior_comp), cfg) == raw:
                    consec += 1
                else:
                    break
            if consec >= cfg.hysteresis_days:
                allow_change = True

        if not allow_change:
            label = prev_label
            flags.append("hysteresis_hold")

    # --- Trend guard on SELL ---------------------------------------------
    if label == "SELL":
        series = [composite]
        for prior in history[:9]:  # up to 10 points total incl. today
            pc = prior.get("composite")
            if pc is None:
                break
            series.append(float(pc))
        series.reverse()  # oldest -> newest for a sensible slope sign
        slope = _slope(series)
        deep = composite <= -(cfg.threshold_sell + 2 * cfg.hysteresis_margin)
        if not deep and slope >= 0:
            label = "HOLD"
            flags.append("sell_suppressed_trend")

    # --- Confidence -------------------------------------------------------
    confidence = min(1.0, abs(composite) / 2.0)

    return label, round(confidence, 4), flags


def apply_divergence(
    confidence: float, flags: list[str], legs: dict[str, LegResult]
) -> tuple[float, list[str]]:
    """Scale confidence down when the ok legs disagree strongly.

    ``divergence`` = max-min across ok leg scores. If >= 2.5, halve confidence
    and add a ``high_divergence`` flag (a manual-review nudge).
    """
    scores = [
        leg.score
        for leg in legs.values()
        if leg is not None and leg.status == "ok" and leg.score is not None
    ]
    out_flags = list(flags)
    if len(scores) >= 2:
        divergence = max(scores) - min(scores)
        if divergence >= 2.5:
            confidence *= 0.5
            if "high_divergence" not in out_flags:
                out_flags.append("high_divergence")
    return round(confidence, 4), out_flags


# ---------------------------------------------------------------------------
# Vetoes
# ---------------------------------------------------------------------------
def apply_vetoes(
    label: str,
    flags: list[str],
    ctx: dict[str, Any],
    cfg: ScoringConfig,
) -> tuple[str, list[str]]:
    """Apply ordered veto rules. Vetoes only add flags and/or *freeze* a label
    change back to the prior label — they never invent a BUY or SELL.

    ``ctx`` keys (all optional):
      * ``next_earnings_date``      — datetime.date | None
      * ``prev_label``              — str | None
      * ``ai_materiality``          — str | None
      * ``override_candidate``      — bool
      * ``drawdown_pct_vs_cost``    — float | None (positive = underwater %)
    """
    import datetime as _dt

    out_flags = list(flags)
    out_label = label
    prev_label = ctx.get("prev_label")

    # (1) Earnings freeze: within N days -> revert any label CHANGE to prev.
    next_earnings = ctx.get("next_earnings_date")
    if next_earnings is not None:
        try:
            days_out = (next_earnings - _dt.date.today()).days
        except Exception:
            days_out = None
        if days_out is not None and 0 <= days_out <= cfg.earnings_freeze_days:
            if prev_label is not None and out_label != prev_label:
                out_label = prev_label
            if "earnings_freeze" not in out_flags:
                out_flags.append("earnings_freeze")

    # (2) Material event / override -> manual review flag.
    materiality = ctx.get("ai_materiality")
    override = bool(ctx.get("override_candidate"))
    if materiality in ("ma", "regulatory") or override:
        if "manual_review" not in out_flags:
            out_flags.append("manual_review")

    # (3) Drawdown vs cost basis -> review flag.
    dd = ctx.get("drawdown_pct_vs_cost")
    if dd is not None:
        try:
            if float(dd) >= cfg.max_drawdown_pct:
                if "drawdown_review" not in out_flags:
                    out_flags.append("drawdown_review")
        except (TypeError, ValueError):
            pass

    return out_label, out_flags


# ---------------------------------------------------------------------------
# Rationale
# ---------------------------------------------------------------------------
_FLAG_PHRASES = {
    "hysteresis_hold": "verdict held steady to avoid whipsaw",
    "sell_suppressed_trend": "a sell signal was suppressed because the trend is not falling",
    "high_divergence": "the signals disagree strongly, so manual review is advised",
    "manual_review": "a material event flags this for manual review",
    "earnings_freeze": "changes are frozen around the upcoming earnings date",
    "drawdown_review": "the position is deep underwater and flagged for review",
    "no_data": "insufficient data to score",
}


def _leg_oneliner(key: str, leg: LegResult) -> str | None:
    """A short, deterministic one-liner from a leg's summary dict."""
    if leg is None or leg.status != "ok":
        return None
    s = leg.summary or {}
    score = leg.score
    if key == "technical":
        trend = (s.get("trend") or {}).get("component")
        mom = (s.get("momentum") or {}).get("rsi")
        parts = []
        if trend is not None:
            parts.append(f"trend {trend:+.1f}")
        if mom is not None:
            parts.append(f"RSI {mom:.0f}")
        extra = ", ".join(parts)
        return f"Technicals {score:+.2f}" + (f" ({extra})" if extra else "") + "."
    if key == "fundamental":
        val = (s.get("valuation") or {}).get("forward_pe")
        growth = (s.get("growth") or {}).get("revenue_growth")
        parts = []
        if val is not None:
            parts.append(f"fwd P/E {val:.1f}")
        if growth is not None:
            parts.append(f"rev growth {growth * 100:.0f}%")
        extra = ", ".join(parts)
        return f"Fundamentals {score:+.2f}" + (f" ({extra})" if extra else "") + "."
    if key == "analyst":
        ratio = s.get("consensus_ratio")
        extra = f" (consensus {ratio:+.2f})" if ratio is not None else ""
        return f"Analyst view {score:+.2f}{extra}."
    if key == "news":
        return f"News read {score:+.2f}."
    return f"{key.capitalize()} {score:+.2f}."


def render_rationale(
    label: str,
    legs: dict[str, LegResult],
    key_facts: list[str] | None,
    flags: list[str] | None,
) -> str:
    """Deterministic 2-4 sentence rationale: verdict, then ok-leg one-liners,
    then up to 3 key facts, then flags in plain language. No LLM."""
    key_facts = key_facts or []
    flags = flags or []

    sentences: list[str] = [f"Verdict: {label}."]

    for key in LEG_KEYS:
        leg = legs.get(key)
        line = _leg_oneliner(key, leg) if leg is not None else None
        if line:
            sentences.append(line)

    for fact in key_facts[:3]:
        fact = fact.strip().rstrip(".")
        if fact:
            sentences.append(f"News: {fact}.")

    plain_flags = [
        _FLAG_PHRASES[f] for f in flags if f in _FLAG_PHRASES
    ]
    if plain_flags:
        sentences.append("Note: " + "; ".join(plain_flags) + ".")

    return " ".join(sentences)


# ---------------------------------------------------------------------------
# Convenience wiring
# ---------------------------------------------------------------------------
def evaluate_holding(
    symbol: str,
    legs: dict[str, LegResult],
    cfg: ScoringConfig,
    *,
    history: list[dict] | None = None,
    ctx: dict[str, Any] | None = None,
    key_facts: list[str] | None = None,
    ai_result: Any = None,
    price_at_verdict: float | None = None,
) -> dict[str, Any]:
    """Wire composite -> decide -> divergence -> vetoes -> rationale into a dict
    shaped for :func:`app.db.upsert_verdict`.

    ``ai_result`` (an :class:`AIResult`-like object, optional) supplies
    ``model_used``, ``raw_response`` and, when ``key_facts`` isn't given, the
    key facts + materiality/override for the veto context.
    """
    history = history or []
    ctx = dict(ctx or {})

    # Fold AI outputs into ctx/key_facts if provided.
    if ai_result is not None:
        if key_facts is None:
            key_facts = list(getattr(ai_result, "key_facts", []) or [])
        ctx.setdefault("ai_materiality", getattr(ai_result, "materiality", None))
        ctx.setdefault(
            "override_candidate", bool(getattr(ai_result, "override_candidate", False))
        )

    composite, data_quality, legs_used = compute_composite(legs, cfg.weights)

    label, confidence, flags = decide(composite, history, cfg)
    confidence, flags = apply_divergence(confidence, flags, legs)

    ctx.setdefault("prev_label", history[0]["label"] if history else None)
    label, flags = apply_vetoes(label, flags, ctx, cfg)

    rationale = render_rationale(label, legs, key_facts, flags)

    def _leg_score(key: str) -> float | None:
        leg = legs.get(key)
        if leg is not None and leg.status == "ok":
            return leg.score
        return None

    override_flag = int(
        bool(getattr(ai_result, "override_candidate", False))
        or "manual_review" in flags
    )

    return {
        "symbol": symbol,
        "score_technical": _leg_score("technical"),
        "score_fundamental": _leg_score("fundamental"),
        "score_analyst": _leg_score("analyst"),
        "score_news": _leg_score("news"),
        "composite": composite,
        "label": label,
        "confidence": confidence,
        "rationale": rationale,
        "override_flag": override_flag,
        "review_flags": ",".join(flags),
        "data_quality": data_quality,
        "price_at_verdict": price_at_verdict,
        "model_used": getattr(ai_result, "model_used", None),
        # Persist the prompt version whenever an AI result was produced, so
        # verdicts are attributable to a prompt revision.
        "prompt_version": (_PROMPT_VERSION if ai_result is not None else None),
        "raw_ai_response": getattr(ai_result, "raw_response", None),
    }
