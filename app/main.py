"""FastAPI app: public webhook endpoint + password-protected web GUI.

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8000

Security model:
  * /webhook is the ONLY endpoint TradingView needs.  It is authenticated by the
    shared `secret` inside the JSON payload (and should additionally be
    IP-allowlisted at your reverse proxy to TradingView's published webhook IPs).
  * Everything else (the dashboard + control APIs) sits behind HTTP Basic auth.
  * Bind to 127.0.0.1 and expose the GUI only via a tunnel/VPN — never the open
    internet.
"""

from __future__ import annotations

import base64
import datetime as _dt
import os
import secrets
import threading
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .broker import BrokerError, broker
from .models import WebhookSignal
from .trading import handle_signal

app = FastAPI(title="AlgoFoundry")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
security = HTTPBasic()


def _datetimeformat(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (ValueError, TypeError):
        return str(ts)


def _logo_data_uri() -> str:
    """Base64 data URI of the wordmark, cached.

    Inlining the logo means it always renders with the page — no separate
    request that could 404 on a stale cache or an un-restarted server.
    """
    global _LOGO_URI_CACHE
    if _LOGO_URI_CACHE is None:
        path = os.path.join(os.path.dirname(__file__), "static", "logo_wordmark.png")
        try:
            with open(path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            _LOGO_URI_CACHE = f"data:image/png;base64,{b64}"
        except OSError:
            _LOGO_URI_CACHE = "/static/logo_wordmark.png"  # fallback to static
    return _LOGO_URI_CACHE


_LOGO_URI_CACHE: str | None = None


templates.env.filters["datetimeformat"] = _datetimeformat

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)

GUI_USER = os.environ.get("ALGOFOUNDRY_USER", "admin")
GUI_PASS = os.environ.get("ALGOFOUNDRY_PASSWORD", "change-me-now")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    db.log_event("info", detail="AlgoFoundry started")
    # Start the long-term daily scheduler, unless running under pytest or the
    # env kill-switch is set. Failures here must never block app startup.
    if "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            from .longterm.scheduler import start_scheduler
            start_scheduler(app)
        except Exception as exc:  # noqa: BLE001
            db.log_event("error", detail=f"lt scheduler init failed: {exc}")


# ---- auth ------------------------------------------------------------------
def require_login(
    creds: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    ok_user = secrets.compare_digest(creds.username, GUI_USER)
    ok_pass = secrets.compare_digest(creds.password, GUI_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


# ---- webhook (public, secret-authenticated) --------------------------------
@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        # TradingView sometimes sends text/plain JSON; parse the raw body.
        raw = (await request.body()).decode("utf-8", "ignore").strip()
        import json
        try:
            payload = json.loads(raw)
        except Exception:
            db.log_event("webhook", status="rejected", detail="bad JSON body")
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    try:
        sig = WebhookSignal(**payload)
    except Exception as e:  # validation error
        db.log_event("webhook", status="rejected", detail=f"bad payload: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    expected = db.get_setting("webhook_secret", "")
    if not expected or not secrets.compare_digest(sig.secret, expected):
        db.log_event("webhook", action=sig.action, symbol=sig.symbol,
                     status="rejected", detail="bad secret")
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    db.log_event("webhook", action=sig.action, symbol=sig.symbol,
                 status="received", detail=f"qty={sig.qty}")
    result = handle_signal(sig)
    return JSONResponse({"ok": True, "result": result})


# ---- dashboard -------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_login)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html",
        {"settings": db.get_all_settings(), "logo_uri": _logo_data_uri()},
    )


# ---- live data fragments (HTMX polling) ------------------------------------
@app.get("/api/status", response_class=HTMLResponse)
def api_status(request: Request, _: str = Depends(require_login)) -> HTMLResponse:
    st = broker.status()
    summary = broker.account_summary() if st["connected"] else {}
    return templates.TemplateResponse(
        request, "_status.html",
        {"st": st, "summary": summary, "settings": db.get_all_settings()},
    )


@app.get("/api/positions", response_class=HTMLResponse)
def api_positions(request: Request, _: str = Depends(require_login)) -> HTMLResponse:
    try:
        positions = broker.positions()
        orders = broker.open_orders()
    except Exception as e:  # noqa: BLE001
        positions, orders = [], []
        db.log_event("error", detail=f"positions fetch: {e}")
    return templates.TemplateResponse(
        request, "_positions.html",
        {"positions": positions, "orders": orders},
    )


@app.get("/api/events", response_class=HTMLResponse)
def api_events(request: Request, _: str = Depends(require_login)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_events.html", {"events": db.recent_events(60)},
    )


# ---- control actions -------------------------------------------------------
@app.post("/api/connect")
def api_connect(_: str = Depends(require_login)) -> RedirectResponse:
    s = db.get_all_settings()
    try:
        broker.connect(s["ibkr_host"], int(s["ibkr_port"]), int(s["ibkr_client_id"]))
        db.log_event("info", detail=f"connected {s['ibkr_host']}:{s['ibkr_port']}")
    except Exception as e:  # noqa: BLE001
        db.log_event("error", detail=f"connect failed: {e}")
    return RedirectResponse("/", status_code=303)


@app.post("/api/disconnect")
def api_disconnect(_: str = Depends(require_login)) -> RedirectResponse:
    try:
        broker.disconnect()
        db.log_event("info", detail="disconnected")
    except Exception as e:  # noqa: BLE001
        db.log_event("error", detail=f"disconnect failed: {e}")
    return RedirectResponse("/", status_code=303)


@app.post("/api/settings")
def api_settings(
    _: str = Depends(require_login),
    ibkr_host: str = Form(...),
    ibkr_port: int = Form(...),
    ibkr_client_id: int = Form(...),
    ibkr_account: str = Form(""),
    mode_label: str = Form("paper"),
    sizing_mode: str = Form(...),
    sizing_value: float = Form(...),
    order_type: str = Form("market"),
    limit_offset_pct: float = Form(0.1),
    max_positions: int = Form(5),
    max_position_value: float = Form(10000.0),
    trading_enabled: str = Form("off"),
    allow_buy: str = Form("off"),
    allow_sell: str = Form("off"),
) -> RedirectResponse:
    db.update_settings({
        "ibkr_host": ibkr_host,
        "ibkr_port": ibkr_port,
        "ibkr_client_id": ibkr_client_id,
        "ibkr_account": ibkr_account,
        "mode_label": mode_label,
        "sizing_mode": sizing_mode,
        "sizing_value": sizing_value,
        "order_type": order_type,
        "limit_offset_pct": limit_offset_pct,
        "max_positions": max_positions,
        "max_position_value": max_position_value,
        "trading_enabled": trading_enabled,
        "allow_buy": allow_buy,
        "allow_sell": allow_sell,
    })
    db.log_event("info", detail="settings updated")
    return RedirectResponse("/", status_code=303)


@app.post("/api/kill")
def api_kill(_: str = Depends(require_login)) -> RedirectResponse:
    """One-click emergency stop."""
    db.set_setting("trading_enabled", False)
    db.log_event("info", status="KILL", detail="kill switch engaged")
    return RedirectResponse("/", status_code=303)


@app.post("/api/manual")
def api_manual(
    _: str = Depends(require_login),
    symbol: str = Form(...),
    side: str = Form(...),     # buy | sell | flatten
    qty: float = Form(0.0),
) -> RedirectResponse:
    symbol = symbol.strip().upper()
    s = db.get_all_settings()
    try:
        if side == "flatten":
            res = broker.flatten(symbol)
        else:
            res = broker.place_order(
                symbol, side.upper(),
                qty=qty or None,
                sizing_mode=s["sizing_mode"],
                sizing_value=float(s["sizing_value"]),
                max_position_value=float(s["max_position_value"]),
                order_type=s["order_type"],
                limit_offset_pct=float(s["limit_offset_pct"]),
            )
        db.log_event("order", action=f"manual-{side}", symbol=symbol,
                     status=res.get("status", "?"), detail=str(res))
    except (BrokerError, Exception) as e:  # noqa: BLE001
        db.log_event("error", action=f"manual-{side}", symbol=symbol,
                     detail=str(e))
    return RedirectResponse("/", status_code=303)


@app.post("/api/regen-secret")
def api_regen_secret(_: str = Depends(require_login)) -> RedirectResponse:
    db.set_setting("webhook_secret", secrets.token_urlsafe(24))
    db.log_event("info", detail="webhook secret regenerated")
    return RedirectResponse("/", status_code=303)


# ---- long-term portfolio tracker (ALG-9) -----------------------------------
def _lt_rows(snapshot: list[dict], verdicts_by_symbol: dict[str, dict]) -> list[dict]:
    """Join snapshot holdings with their latest verdict into template rows."""
    rows: list[dict] = []
    for h in snapshot:
        symbol = h.get("symbol") or h.get("t212_ticker")
        v = verdicts_by_symbol.get(symbol) or {}
        legs_used = []
        for leg, key in (
            ("technical", "score_technical"), ("fundamental", "score_fundamental"),
            ("analyst", "score_analyst"), ("news", "score_news"),
        ):
            if v.get(key) is not None:
                legs_used.append(leg)
        rows.append({
            "symbol": symbol,
            "qty": h.get("qty"),
            "avg_price": h.get("avg_price"),
            "current_price": h.get("current_price"),
            "pnl": h.get("pnl"),
            "currency": h.get("currency"),
            "label": v.get("label"),
            "composite": v.get("composite"),
            "confidence": v.get("confidence"),
            "data_quality": v.get("data_quality"),
            "review_flags": v.get("review_flags"),
            "rationale": v.get("rationale"),
            "legs_used_label": "legs: " + ", ".join(legs_used) if legs_used else "",
        })
    return rows


@app.get("/longterm", response_class=HTMLResponse)
def longterm(request: Request, _: str = Depends(require_login)) -> HTMLResponse:
    # Use the latest snapshot date available (today if a run happened, else
    # the most recent past run).
    today = _dt.date.today().isoformat()
    snapshot = db.get_holdings_snapshot(today)
    snapshot_date = today
    if not snapshot:
        recent = db.recent_verdicts(1)
        # Fall back to the most recent snapshot date by scanning verdicts.
        with db._lock, db._conn() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT date FROM longterm_holdings_snapshot "
                "ORDER BY date DESC LIMIT 1"
            ).fetchone()
        snapshot_date = row["date"] if row else today
        snapshot = db.get_holdings_snapshot(snapshot_date) if row else []

    verdicts = db.get_verdicts_for_date(snapshot_date)
    verdict_date = snapshot_date
    if not verdicts:
        # Latest verdicts may be from an earlier date than today's snapshot.
        recent = db.recent_verdicts(1)
        if recent:
            verdict_date = recent[0]["date"]
            verdicts = db.get_verdicts_for_date(verdict_date)
    verdicts_by_symbol = {v["symbol"]: v for v in verdicts}

    rows = _lt_rows(snapshot, verdicts_by_symbol)
    return templates.TemplateResponse(
        request, "_longterm.html",
        {
            "rows": rows,
            "snapshot_date": snapshot_date if snapshot else None,
            "verdict_date": verdict_date if verdicts else None,
        },
    )


@app.post("/longterm/run", response_class=HTMLResponse)
def longterm_run(_: str = Depends(require_login)) -> HTMLResponse:
    """Kick off a forced pipeline run in a background thread and return at once."""
    def _bg() -> None:
        try:
            from .longterm.scheduler import run_daily_pipeline
            run_daily_pipeline(force=True)
        except Exception as exc:  # noqa: BLE001
            db.log_event("error", action="lt_pipeline", detail=f"manual run failed: {exc}")

    threading.Thread(target=_bg, daemon=True).start()
    db.log_event("info", action="lt_pipeline", detail="manual run started")
    return HTMLResponse(
        '<span class="mut">Run started — watch the Event Log for progress.</span>'
    )


@app.get("/longterm/history", response_class=HTMLResponse)
def longterm_history(
    request: Request, symbol: str, _: str = Depends(require_login)
) -> HTMLResponse:
    verdicts = db.get_verdicts_for_symbol(symbol.strip().upper(), limit=30)
    return templates.TemplateResponse(
        request, "_longterm_history.html",
        {"symbol": symbol.strip().upper(), "verdicts": verdicts},
    )


@app.post("/longterm/analyze", response_class=HTMLResponse)
def longterm_analyze(
    request: Request,
    _: str = Depends(require_login),
    symbol: str = Form(""),
    save: str = Form("off"),
) -> HTMLResponse:
    """Ad-hoc single-symbol analysis (ALG-14).

    Synchronous: runs the full long-term legs for one manually-entered symbol
    and renders the verdict fragment. Never touches the daily-run state, never
    notifies; persists only when the ``save`` checkbox is on (stamped ``adhoc``).
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return HTMLResponse(
            templates.get_template("_longterm_analysis.html").render(
                error="Enter a symbol to analyze (e.g. AAPL or VUAG.L)."
            ),
            status_code=400,
        )

    do_save = str(save).lower() in ("on", "true", "1", "yes")
    try:
        from .longterm.scheduler import analyze_adhoc
        verdict = analyze_adhoc(sym, save=do_save)
    except Exception as exc:  # noqa: BLE001 — never 500 to the user
        db.log_event("error", action="lt_adhoc", symbol=sym,
                     detail=f"adhoc analyze failed: {exc}")
        return HTMLResponse(
            templates.get_template("_longterm_analysis.html").render(
                error=f"Analysis failed: {exc}"
            ),
            status_code=200,
        )

    legs = verdict.get("legs") or {}
    leg_chips = []
    for name, key in (
        ("technical", "score_technical"), ("fundamental", "score_fundamental"),
        ("analyst", "score_analyst"), ("news", "score_news"),
    ):
        leg = legs.get(name)
        if leg is not None:
            status = getattr(leg, "status", None) or "no_data"
            score = getattr(leg, "score", None)
        else:
            # No LegResult (e.g. NO_DATA path or news leg absent): infer from the
            # persisted per-leg score.
            score = verdict.get(key)
            status = "ok" if score is not None else "no_data"
        leg_chips.append({"name": name, "status": status, "score": score})

    return templates.TemplateResponse(
        request, "_longterm_analysis.html",
        {
            "symbol": sym,
            "verdict": verdict,
            "leg_chips": leg_chips,
            "saved": do_save,
        },
    )


@app.post("/longterm/settings")
def longterm_settings(
    _: str = Depends(require_login),
    lt_t212_api_key: str = Form(""),
    lt_t212_env: str = Form("demo"),
    lt_finnhub_key: str = Form(""),
    lt_alpha_vantage_key: str = Form(""),
    lt_openrouter_model: str = Form(""),
    lt_openrouter_fallback: str = Form(""),
    lt_callmebot_phone: str = Form(""),
    lt_callmebot_key: str = Form(""),
    lt_weight_technical: float = Form(1.0),
    lt_weight_fundamental: float = Form(1.0),
    lt_weight_analyst: float = Form(1.0),
    lt_weight_news: float = Form(1.0),
    lt_threshold_buy: float = Form(0.5),
    lt_threshold_sell: float = Form(0.75),
    lt_schedule_time: str = Form("17:30"),
) -> RedirectResponse:
    db.update_settings({
        "lt_t212_api_key": lt_t212_api_key,
        "lt_t212_env": lt_t212_env,
        "lt_finnhub_key": lt_finnhub_key,
        "lt_alpha_vantage_key": lt_alpha_vantage_key,
        "lt_openrouter_model": lt_openrouter_model,
        "lt_openrouter_fallback": lt_openrouter_fallback,
        "lt_callmebot_phone": lt_callmebot_phone,
        "lt_callmebot_key": lt_callmebot_key,
        "lt_weight_technical": lt_weight_technical,
        "lt_weight_fundamental": lt_weight_fundamental,
        "lt_weight_analyst": lt_weight_analyst,
        "lt_weight_news": lt_weight_news,
        "lt_threshold_buy": lt_threshold_buy,
        "lt_threshold_sell": lt_threshold_sell,
        "lt_schedule_time": lt_schedule_time,
    })
    db.log_event("info", detail="long-term settings updated")
    return RedirectResponse("/", status_code=303)


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "static", "favicon.ico"),
        media_type="image/x-icon",
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True, "connected": broker.is_connected(), "ts": time.time()}
