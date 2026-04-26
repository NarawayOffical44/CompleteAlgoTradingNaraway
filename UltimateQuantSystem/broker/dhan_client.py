"""
Dhan API wrapper.
Handles paper trading mode transparently — same interface, no real orders sent.
"""

from datetime import datetime
from config import config
from loguru import logger


class DhanClient:

    def __init__(self):
        self.mode = config.trading_mode   # paper | live
        self._paper_positions: dict = {}
        self._paper_orders: list = []

        if self.mode == "live":
            try:
                from dhanhq import dhanhq
                self._client = dhanhq(config.dhan_client_id, config.dhan_access_token)
                logger.info("Dhan LIVE client connected")
            except Exception as e:
                logger.error(f"Dhan connection failed: {e}")
                raise
        else:
            self._client = None
            logger.info("Dhan PAPER mode — no real orders will be placed")

    # ── Market data ───────────────────────────────────────────────────────
    def get_ltp(self, symbol: str, exchange: str = "NSE") -> float:
        """Last traded price."""
        if self.mode == "paper":
            # In paper mode caller must supply price or use data module
            raise NotImplementedError("Use data.market_data for prices in paper mode")
        quote = self._client.get_quote(symbol, exchange)
        return float(quote["data"]["last_price"])

    def get_option_chain(self, symbol: str, expiry: str) -> dict:
        if self.mode == "paper":
            raise NotImplementedError("Use data.market_data for option chain in paper mode")
        return self._client.option_chain(symbol, expiry)

    # ── Order management ──────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        exchange: str,
        order_type: str,          # BUY | SELL
        quantity: int,
        price: float,
        product: str = "CNC",     # CNC | INTRADAY | MF
        order_mode: str = "LIMIT",
    ) -> dict:
        order = {
            "order_id":   f"PAPER_{datetime.now().strftime('%H%M%S%f')}",
            "symbol":     symbol,
            "exchange":   exchange,
            "order_type": order_type,
            "quantity":   quantity,
            "price":      price,
            "product":    product,
            "status":     "TRADED",
            "timestamp":  datetime.now().isoformat(),
        }

        if self.mode == "paper":
            self._paper_orders.append(order)
            self._update_paper_position(symbol, order_type, quantity, price)
            logger.info(f"PAPER ORDER | {order_type} {quantity} {symbol} @ {price}")
            return order

        # Live
        result = self._client.place_order(
            security_id=symbol,
            exchange_segment=exchange,
            transaction_type=order_type,
            quantity=quantity,
            order_type=order_mode,
            product_type=product,
            price=price,
        )
        logger.info(f"LIVE ORDER | {order_type} {quantity} {symbol} @ {price} | id={result.get('orderId')}")
        return result

    def cancel_order(self, order_id: str) -> dict:
        if self.mode == "paper":
            logger.info(f"PAPER CANCEL | {order_id}")
            return {"status": "cancelled", "order_id": order_id}
        return self._client.cancel_order(order_id)

    # ── Positions ─────────────────────────────────────────────────────────
    def get_positions(self) -> list:
        if self.mode == "paper":
            return list(self._paper_positions.values())
        return self._client.get_positions().get("data", [])

    def get_order_history(self) -> list:
        if self.mode == "paper":
            return self._paper_orders
        return self._client.get_order_list().get("data", [])

    # ── Internal paper position tracker ───────────────────────────────────
    def _update_paper_position(self, symbol: str, order_type: str, qty: int, price: float):
        pos = self._paper_positions.get(symbol, {"symbol": symbol, "qty": 0, "avg_price": 0})
        if order_type == "BUY":
            total_cost = pos["avg_price"] * pos["qty"] + price * qty
            pos["qty"] += qty
            pos["avg_price"] = total_cost / pos["qty"] if pos["qty"] else 0
        else:
            pos["qty"] -= qty
        self._paper_positions[symbol] = pos
