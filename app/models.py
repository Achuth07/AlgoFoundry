"""Pydantic models for the TradingView webhook payload."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class WebhookSignal(BaseModel):
    """The JSON TradingView posts to /webhook.

    Example alert message in TradingView:
        {"action":"buy","symbol":"{{ticker}}","secret":"YOUR_SECRET"}
        {"action":"sell","symbol":"{{ticker}}","secret":"YOUR_SECRET"}

    `qty` is optional — if omitted, the bridge sizes the position from the
    GUI settings (fixed shares / fixed dollars / percent of equity).
    """

    action: str
    symbol: str
    secret: str
    qty: Optional[float] = None        # explicit share count (overrides sizing)
    exchange: str = "SMART"
    currency: str = "USD"

    @field_validator("action")
    @classmethod
    def _norm_action(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("buy", "sell"):
            raise ValueError("action must be 'buy' or 'sell'")
        return v

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return v.strip().upper()
