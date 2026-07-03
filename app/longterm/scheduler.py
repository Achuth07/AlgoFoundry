"""Daily orchestration for the long-term tracker (ALG-7).

Wires the whole long-term pipeline together:

    T212 portfolio -> snapshot -> per-holding legs (technical / fundamental /
    analyst / news / AI) -> scoring.evaluate_holding -> upsert_verdict -> one
    WhatsApp summary.

Design principles
-----------------
* **One T212 call per run.** The portfolio is fetched once; a T212 failure
  aborts the run cleanly (no fabricated verdicts).
* **Per-holding isolation.** Every holding is processed in its own
  try/except so one bad symbol never aborts the run — the failure is logged
  and the loop continues.
* **Idempotent, re-runnable.** ``lt_last_run_date`` guards a same-day rerun
  (bypassed with ``force=True``); ``lt_last_notify_date`` guards duplicate
  notifications (a forced rerun re-sends).
* **Sequential legs.** Data legs run one after another per holding — simple,
  and it respects the upstream rate limits (T212 / Finnhub / yfinance).

Scheduler / single-worker constraint
-------------------------------------
:func:`start_scheduler` runs an APScheduler ``BackgroundScheduler`` inside the
web process. It MUST run in exactly one worker — if you deploy behind gunicorn
with multiple workers the cron job would fire once per worker and process the
portfolio N times. Run the app single-worker (the default uvicorn invocation),
or gate the scheduler to a single instance via ``ALGOFOUNDRY_ENABLE_LT_SCHEDULER``.
"""

from __future__ import annotations

import datetime as _dt
import os

from .. import db
from . import ai_research, data_sources, instruments, scoring, t212, notifier

# Module-level guard so start_scheduler is a no-op if called twice.
_SCHEDULER = None


def _today() -> str:
    return _dt.date.today().isoformat()


def _drawdown_pct_vs_cost(avg_price, current_price) -> float | None:
    """Positive % underwater relative to cost basis, or None."""
    try:
        avg = float(avg_price)
        cur = float(current_price)
    except (TypeError, ValueError):
        return None
    if avg <= 0 or cur is None:
        return None
    if cur >= avg:
        return 0.0
    return (avg - cur) / avg * 100.0


def analyze_symbol(
    inst,
    cfg,
    *,
    history=None,
    avg_price=None,
    current_price=None,
    persist: bool = True,
    run_date: str | None = None,
    extra_flags=None,
) -> dict:
    """Run all legs + scoring for one resolved instrument.

    This is the shared body of both the daily pipeline (:func:`_process_holding`
    passes a T212-resolved instrument and the holding's prices) and the ad-hoc
    path (:func:`analyze_adhoc` passes a hand-built instrument). Every network
    seam it touches is a module-level function/attribute so both callers — and
    the existing test mocks — hit the same wiring.

    ``history`` is the prior-verdict list handed to scoring (defaults to a DB
    lookup keyed on the symbol). When ``persist`` is true the verdict is written
    via :func:`app.db.upsert_verdict` under ``run_date`` (defaults to today).
    ``extra_flags`` are appended to the verdict's ``review_flags`` (comma-joined,
    matching scoring's serialization) — used to stamp the ``adhoc`` marker.

    Returns the verdict dict.
    """
    symbol = inst.yf_symbol or inst.finnhub_symbol or inst.t212_ticker

    # ---- Technical leg (price history) ----------------------------------
    from .technicals import technical_score  # local import: pandas-heavy
    df = data_sources.fetch_ohlcv(inst.yf_symbol) if inst.yf_symbol else None
    legs = {"technical": technical_score(df)}

    # ---- Fundamentals (equity only) -------------------------------------
    from .fundamentals import fetch_fundamentals, fundamental_score
    if inst.instrument_type == "etf":
        legs["fundamental"] = fundamental_score(None, inst.instrument_type)
    else:
        metrics = fetch_fundamentals(inst.yf_symbol) if inst.yf_symbol else None
        legs["fundamental"] = fundamental_score(metrics, inst.instrument_type)

    # ---- Analyst / news / earnings (finnhub_symbol only) ----------------
    analyst_payload = None
    headlines: list[dict] = []
    next_earnings = None
    if inst.finnhub_symbol:
        analyst_payload = data_sources.fetch_analyst(inst.finnhub_symbol)
        news_leg = data_sources.fetch_news(
            inst.finnhub_symbol, instrument_type=inst.instrument_type
        )
        if news_leg.status == "ok":
            headlines = (news_leg.summary or {}).get("headlines", []) or []
        next_earnings = data_sources.fetch_earnings_calendar(
            inst.finnhub_symbol, instrument_type=inst.instrument_type
        )

    legs["analyst"] = data_sources.analyst_score(
        analyst_payload,
        finnhub_symbol=inst.finnhub_symbol,
        instrument_type=inst.instrument_type,
        current_price=current_price,
    )

    # ---- AI synthesis (only if headlines + a model configured) ----------
    ai_result = None
    have_model = bool(
        (db.get_setting("lt_openrouter_model", "") or "").strip()
        or (db.get_setting("lt_openrouter_fallback", "") or "").strip()
    )
    if headlines and have_model:
        try:
            ai_result = ai_research.analyze_holding(
                symbol,
                legs["technical"].summary if legs["technical"].status == "ok" else {},
                legs["fundamental"].summary
                if legs["fundamental"].status == "ok" else None,
                legs["analyst"].summary
                if legs["analyst"].status == "ok" else None,
                headlines,
            )
        except Exception as exc:  # AI misconfig / transport — treat as failed leg
            db.log_event("info", symbol=symbol, status="no_data",
                         detail=f"ai_research failed: {exc}")
            ai_result = ai_research.AIResult(status="failed", detail=str(exc))

    # Fold the AI news score into a news leg so it contributes to composite.
    if ai_result is not None and ai_result.status == "ok" \
            and ai_result.news_score is not None:
        legs["news"] = data_sources.LegResult(
            status="ok", score=float(ai_result.news_score),
            summary={"key_facts": ai_result.key_facts},
            detail="news score from AI synthesis",
        )

    # ---- Scoring --------------------------------------------------------
    if history is None:
        history = db.get_verdicts_for_symbol(symbol, limit=cfg.hysteresis_days + 10)
    ctx = {
        "next_earnings_date": next_earnings,
        "drawdown_pct_vs_cost": _drawdown_pct_vs_cost(avg_price, current_price),
    }
    verdict = scoring.evaluate_holding(
        symbol, legs, cfg,
        history=history,
        ctx=ctx,
        ai_result=ai_result if (ai_result and ai_result.status == "ok") else None,
        price_at_verdict=current_price,
    )

    # Append any extra review flags (e.g. the ad-hoc marker), matching the
    # comma-joined serialization scoring uses.
    if extra_flags:
        existing = [f for f in (verdict.get("review_flags") or "").split(",") if f]
        for flag in extra_flags:
            if flag and flag not in existing:
                existing.append(flag)
        verdict["review_flags"] = ",".join(existing)

    # Expose the per-leg LegResults so callers (the ad-hoc route) can render
    # chip status without re-deriving it. Not persisted (upsert_verdict ignores
    # unknown keys).
    verdict["legs"] = legs

    # Expose raw headlines + AI metadata for the UI news section.
    verdict["_headlines"] = headlines  # list of {headline, source, datetime, url}
    if ai_result is not None and ai_result.status == "ok":
        verdict["_ai_meta"] = {
            "key_facts": ai_result.key_facts or [],
            "materiality": ai_result.materiality,
            "override_candidate": ai_result.override_candidate,
            "override_reason": ai_result.detail or "",
        }
    else:
        verdict["_ai_meta"] = None

    if persist:
        db.upsert_verdict(date=run_date or _today(), **verdict)
    return verdict


def _process_holding(holding, cfg, run_date: str) -> dict | None:
    """Run all legs + scoring for one holding and persist the verdict.

    Returns the verdict dict on success, or None if the holding could not be
    mapped to a market-data symbol (still logged). Raises on unexpected errors
    so the caller's isolation wrapper records them per-symbol.

    Thin wrapper over :func:`analyze_symbol` — the daily pipeline's behavior is
    entirely defined there; this only resolves the T212 ticker and hands the
    holding's prices through.
    """
    inst = instruments.resolve(holding.t212_ticker)
    return analyze_symbol(
        inst, cfg,
        avg_price=holding.avg_price,
        current_price=holding.current_price,
        persist=True,
        run_date=run_date,
    )


# ---------------------------------------------------------------------------
# Ad-hoc single-symbol analysis (ALG-14)
# ---------------------------------------------------------------------------
def _detect_instrument_type(yf_symbol: str) -> tuple[str, str | None]:
    """Best-effort ``('etf'|'equity', note)`` from yfinance ``quoteType``.

    Cheap peek via the fundamentals fetch path's ``yf.Ticker(...).info``. Any
    failure defaults to ``'equity'`` with an explanatory note — the ad-hoc path
    must never raise here.
    """
    try:
        from . import fundamentals
        yf = getattr(fundamentals, "yf", None)
        if yf is None:
            return "equity", "instrument type unknown (yfinance unavailable); assumed equity"
        info = getattr(yf.Ticker(yf_symbol), "info", None) or {}
        qtype = (info.get("quoteType") or "").upper()
        if qtype == "ETF":
            return "etf", None
        if qtype:
            return "equity", None
        return "equity", "instrument type undetermined; assumed equity"
    except Exception as exc:  # never propagate — default to equity
        return "equity", f"instrument type detection failed ({exc}); assumed equity"


def _adhoc_instrument(symbol: str) -> instruments.Instrument:
    """Build an ad-hoc :class:`Instrument`, bypassing T212 mapping.

    ``yf_symbol`` is the symbol as-typed. ``finnhub_symbol`` is set only for
    plain (no ``.`` suffix) symbols — the Finnhub free tier is US-only, so
    suffixed forms like ``VUAG.L`` get no Finnhub symbol. Instrument type is
    detected from yfinance ``quoteType`` where cheaply available.
    """
    finnhub_symbol = symbol if "." not in symbol else None
    instrument_type, _note = _detect_instrument_type(symbol)
    return instruments.Instrument(
        t212_ticker=symbol,
        yf_symbol=symbol,
        finnhub_symbol=finnhub_symbol,
        instrument_type=instrument_type,
    )


def _no_data_verdict(symbol: str) -> dict:
    """The NO_DATA-shaped verdict returned for unknown/invalid symbols."""
    return {
        "symbol": symbol,
        "score_technical": None, "score_fundamental": None,
        "score_analyst": None, "score_news": None,
        "composite": None, "label": "HOLD", "confidence": 0.0,
        "rationale": "Verdict: HOLD. Insufficient data to score.",
        "override_flag": 0, "review_flags": "no_data",
        "data_quality": "no_data", "price_at_verdict": None,
        "model_used": None, "prompt_version": None, "raw_ai_response": None,
        "legs": {},
    }


def analyze_adhoc(symbol: str, save: bool = False) -> dict:
    """Analyze a single manually-entered symbol for ad-hoc review.

    Runs the same legs as the daily pipeline via :func:`analyze_symbol`, but:
    bypasses T212 mapping, never writes a holdings snapshot, never touches
    ``lt_last_run_date`` / ``lt_last_notify_date``, and never notifies. When
    ``save`` is true the verdict is persisted (stamped with an ``adhoc`` review
    flag) under today's date; otherwise nothing is written.

    An unknown/invalid symbol (yfinance returns no price history) yields the
    NO_DATA-shaped verdict rather than raising, so the route can render it.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return _no_data_verdict("")

    inst = _adhoc_instrument(symbol)
    cfg = scoring.ScoringConfig.from_settings(db)

    # Probe price history up-front: no OHLCV means an unknown/invalid symbol.
    try:
        df = data_sources.fetch_ohlcv(inst.yf_symbol)
    except Exception as exc:
        db.log_event("info", symbol=symbol, status="no_data",
                     detail=f"adhoc fetch_ohlcv failed: {exc}")
        df = None
    if df is None:
        verdict = _no_data_verdict(symbol)
        if save:
            verdict = dict(verdict)
            verdict["review_flags"] = "adhoc,no_data"
            persist = {k: v for k, v in verdict.items() if k != "legs"}
            db.upsert_verdict(date=_today(), **persist)
        db.log_event("info", action="lt_adhoc", symbol=symbol, status="no_data",
                     detail="adhoc analysis: no market data")
        return verdict

    history = db.get_verdicts_for_symbol(symbol) if save else []
    verdict = analyze_symbol(
        inst, cfg,
        history=history,
        current_price=None,
        persist=save,
        extra_flags=["adhoc"] if save else None,
    )
    db.log_event("info", action="lt_adhoc", symbol=symbol,
                 status=(verdict.get("label") or "").lower() or "done",
                 detail=f"adhoc analysis: {verdict.get('label')} "
                        f"(saved={save})")
    return verdict


def run_daily_pipeline(force: bool = False, send_notification: bool = True) -> dict:
    """Run the full long-term pipeline once and return a summary dict.

    Summary keys: ``date``, ``processed``, ``counts`` (BUY/HOLD/SELL),
    ``errors`` (list of ``symbol: msg``), and ``skipped`` / ``error`` when the
    run short-circuits.
    """
    run_date = _today()

    if not force and db.get_setting("lt_last_run_date", "") == run_date:
        return {
            "skipped": True, "date": run_date, "processed": 0,
            "counts": {"BUY": 0, "HOLD": 0, "SELL": 0}, "errors": [],
        }

    db.log_event("info", action="lt_pipeline", status="start",
                 detail=f"long-term run starting for {run_date}")

    # ---- Single T212 fetch ----------------------------------------------
    try:
        holdings = t212.fetch_portfolio()
    except t212.T212Error as exc:
        db.log_event("error", action="lt_pipeline", status="failed",
                     detail=f"portfolio fetch failed: {exc}")
        return {
            "error": str(exc), "date": run_date, "processed": 0,
            "counts": {"BUY": 0, "HOLD": 0, "SELL": 0}, "errors": [],
        }

    # ---- Snapshot -------------------------------------------------------
    for h in holdings:
        try:
            inst = instruments.resolve(h.t212_ticker)
            symbol = inst.yf_symbol or inst.finnhub_symbol or h.t212_ticker
        except Exception:
            symbol = h.t212_ticker
        db.upsert_holdings_snapshot(
            date=run_date, t212_ticker=h.t212_ticker, symbol=symbol,
            qty=h.quantity, avg_price=h.avg_price,
            current_price=h.current_price, pnl=h.ppl, currency=h.currency,
        )

    cfg = scoring.ScoringConfig.from_settings(db)

    counts = {"BUY": 0, "HOLD": 0, "SELL": 0}
    errors: list[str] = []
    verdicts: list[dict] = []

    for h in holdings:
        try:
            verdict = _process_holding(h, cfg, run_date)
            if verdict is not None:
                verdicts.append(verdict)
                label = (verdict.get("label") or "").upper()
                if label in counts:
                    counts[label] += 1
        except Exception as exc:  # ISOLATED: one failure never aborts the run
            errors.append(f"{h.t212_ticker}: {exc}")
            db.log_event("error", action="lt_pipeline", symbol=h.t212_ticker,
                         status="failed", detail=f"holding failed: {exc}")

    db.set_setting("lt_last_run_date", run_date)

    # ---- Notification ---------------------------------------------------
    already_notified = db.get_setting("lt_last_notify_date", "") == run_date
    if send_notification and verdicts and (force or not already_notified):
        try:
            message = notifier.compose_summary(verdicts, run_date)
            if notifier.send_whatsapp(message):
                db.set_setting("lt_last_notify_date", run_date)
        except Exception as exc:  # notifier must never break the run
            db.log_event("error", action="lt_pipeline",
                         detail=f"notification failed: {exc}")

    summary = {
        "date": run_date, "processed": len(verdicts), "counts": counts,
        "errors": errors,
    }
    db.log_event(
        "info", action="lt_pipeline", status="done",
        detail=(
            f"run complete: {len(verdicts)} processed, "
            f"{counts['BUY']} BUY / {counts['HOLD']} HOLD / {counts['SELL']} SELL, "
            f"{len(errors)} errors"
        ),
    )
    return summary


# ---------------------------------------------------------------------------
# APScheduler integration
# ---------------------------------------------------------------------------
def start_scheduler(app=None):
    """Start a BackgroundScheduler that runs the daily pipeline on cron.

    Reads ``lt_schedule_time`` (HH:MM) and ``lt_schedule_tz`` from settings.
    Idempotent: a second call is a no-op. Disabled when the environment
    variable ``ALGOFOUNDRY_ENABLE_LT_SCHEDULER`` is "0". Returns the scheduler
    instance (or None if not started).

    Single-worker constraint: see the module docstring.
    """
    global _SCHEDULER

    if os.environ.get("ALGOFOUNDRY_ENABLE_LT_SCHEDULER", "1") == "0":
        return None
    if _SCHEDULER is not None:
        return _SCHEDULER

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception as exc:  # apscheduler missing — never block the app
        db.log_event("error", action="lt_scheduler",
                     detail=f"apscheduler unavailable: {exc}")
        return None

    schedule_time = db.get_setting("lt_schedule_time", "17:30") or "17:30"
    tz = db.get_setting("lt_schedule_tz", "America/New_York") or "America/New_York"
    try:
        hour_s, minute_s = str(schedule_time).split(":")
        hour, minute = int(hour_s), int(minute_s)
    except (ValueError, AttributeError):
        hour, minute = 17, 30

    try:
        scheduler = BackgroundScheduler(timezone=tz)
        scheduler.add_job(
            lambda: run_daily_pipeline(force=False, send_notification=True),
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id="lt_daily_pipeline",
            replace_existing=True,
        )
        scheduler.start()
    except Exception as exc:  # bad tz / start failure — never block the app
        db.log_event("error", action="lt_scheduler",
                     detail=f"scheduler start failed: {exc}")
        return None

    _SCHEDULER = scheduler
    db.log_event("info", action="lt_scheduler", status="started",
                 detail=f"long-term scheduler at {hour:02d}:{minute:02d} {tz}")
    return scheduler
