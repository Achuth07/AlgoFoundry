"""Signal-handling logic — the bridge is the source of truth for position state.

Rules (long-only swing design):
  * BUY  is acted on only if the account is currently FLAT in that symbol.
  * SELL is acted on only if the account currently HOLDS that symbol (long).
  * The global kill switch, per-side toggles and max-positions cap are enforced
    here, before any order is sent.

Because we read live positions from IBKR on every signal, a restart or a manual
trade can't desync the bridge — it always reconciles against the broker.
"""

from __future__ import annotations

from typing import Any

from . import db
from .broker import BrokerError, broker
from .models import WebhookSignal


def handle_signal(sig: WebhookSignal) -> dict[str, Any]:
    s = db.get_all_settings()

    # --- Global guards -----------------------------------------------------
    if not s["trading_enabled"]:
        return _reject(sig, "trading disabled (kill switch OFF)")
    if sig.action == "buy" and not s["allow_buy"]:
        return _reject(sig, "buys disabled")
    if sig.action == "sell" and not s["allow_sell"]:
        return _reject(sig, "sells disabled")
    if not broker.is_connected():
        return _reject(sig, "not connected to IBKR")

    # --- Position-aware gating --------------------------------------------
    held = broker.position_qty(sig.symbol)

    if sig.action == "buy":
        if held > 0:
            return _reject(sig, f"already long {held} {sig.symbol}, ignoring buy")
        # Respect the max distinct-positions cap.
        open_symbols = {p["symbol"] for p in broker.positions() if p["qty"] != 0}
        if (sig.symbol not in open_symbols
                and len(open_symbols) >= int(s["max_positions"])):
            return _reject(sig, f"max_positions ({s['max_positions']}) reached")
        return _execute(sig, "BUY", s)

    # action == "sell": take-profit exit, only if we actually hold it
    if held <= 0:
        return _reject(sig, f"flat in {sig.symbol}, ignoring sell")
    # Exit the full long position regardless of the signal's qty.
    return _execute(sig, "SELL", s, override_qty=abs(held))


def _execute(sig: WebhookSignal, side: str, s: dict[str, Any],
             override_qty: float | None = None) -> dict[str, Any]:
    try:
        result = broker.place_order(
            sig.symbol,
            side,
            qty=override_qty if override_qty is not None else sig.qty,
            sizing_mode=s["sizing_mode"],
            sizing_value=float(s["sizing_value"]),
            max_position_value=float(s["max_position_value"]),
            order_type=s["order_type"],
            limit_offset_pct=float(s["limit_offset_pct"]),
            exchange=sig.exchange,
            currency=sig.currency,
        )
    except BrokerError as e:
        return _reject(sig, str(e))
    except Exception as e:  # noqa: BLE001 - surface anything unexpected to the log
        return _reject(sig, f"order error: {e}")

    db.log_event(
        "order", action=sig.action, symbol=sig.symbol,
        status=result.get("status", "?"),
        detail=(f"{side} {result.get('qty')} @~{result.get('ref_price')} "
                f"-> {result.get('status')} "
                f"(filled {result.get('filled')} @ {result.get('avg_fill_price')})"),
    )
    return {"accepted": True, **result}


def _reject(sig: WebhookSignal, reason: str) -> dict[str, Any]:
    db.log_event(
        "webhook", action=sig.action, symbol=sig.symbol,
        status="rejected", detail=reason,
    )
    return {"accepted": False, "reason": reason}
