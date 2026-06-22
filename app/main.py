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
import os
import secrets
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


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "static", "favicon.ico"),
        media_type="image/x-icon",
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True, "connected": broker.is_connected(), "ts": time.time()}
