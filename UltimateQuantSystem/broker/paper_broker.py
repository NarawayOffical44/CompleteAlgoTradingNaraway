"""
Generic paper execution adapter.

This is the safe fallback for any exchange route that does not have a live
adapter configured. It accepts fractional quantities, so crypto/FX/prediction
market bots can be tested without being forced through equity-style lot sizes.
"""

from datetime import datetime
import threading
from loguru import logger


class PaperBroker:
    def __init__(self, name: str = "paper"):
        self.name = name
        self.mode = "paper"
        self._positions: dict = {}
        self._orders: list = []
        self._seen_order_keys: set = set()
        self._lock = threading.Lock()
        logger.info(f"PaperBroker | {name} ready")

    def place_order(
        self,
        symbol: str,
        exchange: str,
        order_type: str,
        quantity: float,
        price: float,
        product: str = "CNC",
        order_mode: str = "LIMIT",
        client_order_id: str = "",
    ) -> dict:
        qty = float(quantity)
        if qty <= 0:
            raise ValueError(f"quantity must be > 0 for {exchange}:{symbol}")

        key = client_order_id or f"{exchange}:{symbol}:{order_type}:{qty}:{price}:{datetime.now().isoformat()}"
        order = {
            "order_id": client_order_id or f"PAPER_{datetime.now().strftime('%H%M%S%f')}",
            "client_order_id": key,
            "symbol": symbol,
            "exchange": exchange,
            "order_type": order_type,
            "quantity": qty,
            "price": float(price or 0),
            "product": product,
            "order_mode": order_mode,
            "status": "TRADED",
            "timestamp": datetime.now().isoformat(),
            "broker": self.name,
        }

        with self._lock:
            if key in self._seen_order_keys:
                logger.warning(f"PAPER DEDUPE | {key}")
                return {"status": "duplicate", "client_order_id": key, "broker": self.name}
            self._seen_order_keys.add(key)
            self._orders.append(order)
            self._update_position(exchange, symbol, order_type, qty, float(price or 0))

        logger.info(f"PAPER ORDER | {exchange} | {order_type} {qty:g} {symbol} @ {price}")
        return order

    def cancel_order(self, order_id: str) -> dict:
        logger.info(f"PAPER CANCEL | {order_id}")
        return {"status": "cancelled", "order_id": order_id, "broker": self.name}

    def get_positions(self) -> list:
        with self._lock:
            return list(self._positions.values())

    def get_order_history(self) -> list:
        with self._lock:
            return list(self._orders)

    def _update_position(self, exchange: str, symbol: str, order_type: str, qty: float, price: float):
        key = f"{exchange}:{symbol}"
        pos = self._positions.get(
            key,
            {"key": key, "exchange": exchange, "symbol": symbol, "qty": 0.0, "avg_price": 0.0},
        )

        current_qty = float(pos["qty"])
        signed_qty = qty if order_type.upper() == "BUY" else -qty
        new_qty = current_qty + signed_qty

        # Weighted average only when increasing exposure in the same direction.
        if current_qty == 0 or (current_qty > 0 and signed_qty > 0) or (current_qty < 0 and signed_qty < 0):
            total_abs = abs(current_qty) + abs(signed_qty)
            pos["avg_price"] = (
                (abs(current_qty) * pos["avg_price"] + abs(signed_qty) * price) / total_abs
                if total_abs > 0 else 0.0
            )
        elif new_qty == 0:
            pos["avg_price"] = 0.0

        pos["qty"] = new_qty
        self._positions[key] = pos
