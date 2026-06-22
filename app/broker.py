"""IBKR access via ib_async, isolated in its own asyncio loop + thread.

Why a dedicated thread?  ib_async is an asyncio library and FastAPI/uvicorn
already run an event loop.  Driving ib_async directly from request handlers
leads to "loop already running" conflicts.  Instead we run a single private
event loop in a background thread that owns the IB connection, and every public
method below submits a coroutine to that loop and blocks for the result.

ib_async is the maintained successor to ib_insync (drop-in compatible).
"""

from __future__ import annotations

import asyncio
import math
import threading
from typing import Any, Optional

from ib_async import IB, LimitOrder, MarketOrder, Stock


class BrokerError(RuntimeError):
    pass


class IBKRBroker:
    def __init__(self) -> None:
        self._ib = IB()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="ibkr-loop", daemon=True
        )
        self._thread.start()
        self._conn_lock = threading.Lock()

    # ---- loop plumbing ----------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro, timeout: float = 30.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ---- connection -------------------------------------------------------
    def is_connected(self) -> bool:
        return self._ib.isConnected()

    def connect(self, host: str, port: int, client_id: int) -> dict[str, Any]:
        with self._conn_lock:
            async def _c():
                if self._ib.isConnected():
                    self._ib.disconnect()
                    await asyncio.sleep(0.25)
                await self._ib.connectAsync(
                    host, port, clientId=client_id, timeout=15
                )
                # Allow delayed data so paper accounts without market-data
                # subscriptions can still price orders (3 = delayed, 4 = frozen).
                self._ib.reqMarketDataType(3)
                return True

            self._run(_c())
        return self.status()

    def disconnect(self) -> None:
        with self._conn_lock:
            self._run(self._disconnect_async())

    async def _disconnect_async(self) -> None:
        if self._ib.isConnected():
            self._ib.disconnect()

    def status(self) -> dict[str, Any]:
        connected = self._ib.isConnected()
        info: dict[str, Any] = {"connected": connected}
        if connected:
            try:
                info["accounts"] = self._ib.managedAccounts()
            except Exception:
                info["accounts"] = []
        return info

    def account_summary(self, account: str = "") -> dict[str, float]:
        if not self._ib.isConnected():
            return {}

        async def _s():
            rows = await self._ib.accountSummaryAsync(account or "All")
            out: dict[str, float] = {}
            for r in rows:
                if r.tag in ("NetLiquidation", "AvailableFunds", "BuyingPower",
                             "TotalCashValue"):
                    try:
                        out[r.tag] = float(r.value)
                    except ValueError:
                        pass
            return out

        return self._run(_s())

    # ---- market data / contracts -----------------------------------------
    async def _qualify(self, symbol: str, exchange: str, currency: str) -> Stock:
        contract = Stock(symbol, exchange, currency)
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            raise BrokerError(f"Could not qualify contract for {symbol}")
        return qualified[0]

    async def _price(self, contract: Stock) -> float:
        ticker = self._ib.reqMktData(contract, "", False, False)
        # Give IB a moment to populate a price.
        for _ in range(20):
            await asyncio.sleep(0.15)
            px = ticker.marketPrice()
            if px and not math.isnan(px):
                self._ib.cancelMktData(contract)
                return float(px)
            for cand in (ticker.last, ticker.close, ticker.bid, ticker.ask):
                if cand and not math.isnan(cand):
                    self._ib.cancelMktData(contract)
                    return float(cand)
        self._ib.cancelMktData(contract)
        raise BrokerError(f"No market price available for {contract.symbol}")

    # ---- positions --------------------------------------------------------
    def positions(self) -> list[dict[str, Any]]:
        if not self._ib.isConnected():
            return []

        async def _p():
            await self._ib.reqPositionsAsync()
            out = []
            for p in self._ib.positions():
                out.append({
                    "symbol": p.contract.symbol,
                    "qty": p.position,
                    "avg_cost": round(p.avgCost, 4),
                    "account": p.account,
                })
            return out

        return self._run(_p())

    def position_qty(self, symbol: str) -> float:
        for p in self.positions():
            if p["symbol"] == symbol.upper():
                return p["qty"]
        return 0.0

    def open_orders(self) -> list[dict[str, Any]]:
        if not self._ib.isConnected():
            return []

        async def _o():
            trades = self._ib.openTrades()
            out = []
            for t in trades:
                out.append({
                    "symbol": t.contract.symbol,
                    "action": t.order.action,
                    "qty": t.order.totalQuantity,
                    "type": t.order.orderType,
                    "status": t.orderStatus.status,
                })
            return out

        return self._run(_o())

    # ---- sizing -----------------------------------------------------------
    def compute_qty(
        self,
        contract: Stock,
        price: float,
        sizing_mode: str,
        sizing_value: float,
        max_position_value: float,
    ) -> int:
        if sizing_mode == "fixed_shares":
            qty = int(sizing_value)
            dollars = qty * price
        elif sizing_mode == "fixed_dollars":
            dollars = sizing_value
            qty = int(dollars // price)
        elif sizing_mode == "percent_equity":
            net_liq = self.account_summary().get("NetLiquidation", 0.0)
            dollars = net_liq * (sizing_value / 100.0)
            qty = int(dollars // price)
        else:
            raise BrokerError(f"Unknown sizing_mode: {sizing_mode}")

        # Enforce the per-position dollar cap.
        if max_position_value > 0 and qty * price > max_position_value:
            qty = int(max_position_value // price)
        return max(qty, 0)

    # ---- orders -----------------------------------------------------------
    def place_order(
        self,
        symbol: str,
        action: str,           # BUY | SELL
        *,
        qty: Optional[float] = None,
        sizing_mode: str = "fixed_dollars",
        sizing_value: float = 1000.0,
        max_position_value: float = 0.0,
        order_type: str = "market",
        limit_offset_pct: float = 0.1,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> dict[str, Any]:
        if not self._ib.isConnected():
            raise BrokerError("Not connected to IBKR")

        async def _place():
            contract = await self._qualify(symbol, exchange, currency)
            price = await self._price(contract)

            if qty and qty > 0:
                order_qty = int(qty)
            else:
                order_qty = self.compute_qty(
                    contract, price, sizing_mode, sizing_value, max_position_value
                )
            if order_qty <= 0:
                raise BrokerError(
                    f"Computed quantity is 0 for {symbol} at ~{price:.2f} "
                    f"(mode={sizing_mode}, value={sizing_value})"
                )

            if order_type == "limit":
                # Cross the spread slightly to favour a fill.
                off = price * (limit_offset_pct / 100.0)
                limit_px = round(price + off if action == "BUY" else price - off, 2)
                order = LimitOrder(action, order_qty, limit_px)
            else:
                order = MarketOrder(action, order_qty)

            trade = self._ib.placeOrder(contract, order)
            # Wait briefly for an ack / fill so we can report status.
            for _ in range(20):
                await asyncio.sleep(0.2)
                if trade.orderStatus.status in (
                    "Filled", "Submitted", "PreSubmitted", "Cancelled", "ApiCancelled"
                ):
                    break
            return {
                "symbol": symbol,
                "action": action,
                "qty": order_qty,
                "ref_price": round(price, 2),
                "order_type": order_type,
                "status": trade.orderStatus.status,
                "filled": trade.orderStatus.filled,
                "avg_fill_price": trade.orderStatus.avgFillPrice,
            }

        return self._run(_place(), timeout=45)

    def flatten(self, symbol: str, exchange: str = "SMART",
                currency: str = "USD") -> dict[str, Any]:
        qty = self.position_qty(symbol)
        if qty == 0:
            return {"symbol": symbol, "status": "no position"}
        action = "SELL" if qty > 0 else "BUY"
        return self.place_order(
            symbol, action, qty=abs(qty), order_type="market",
            exchange=exchange, currency=currency,
        )


# Singleton used across the app.
broker = IBKRBroker()
