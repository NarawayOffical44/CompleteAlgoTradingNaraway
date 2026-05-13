"""
CCXT execution adapter for crypto-style venues.

Used by BrokerRouter when TRADING_MODE=live and the required exchange API keys
are present in the environment. Paper mode is handled by PaperBroker instead.
"""

import os
import re
import threading
from datetime import datetime
from loguru import logger


class CcxtBroker:
    def __init__(
        self,
        exchange_id: str,
        api_key: str,
        secret: str,
        password: str = "",
        default_type: str = "spot",
        name: str = "",
    ):
        if not api_key or not secret:
            raise ValueError(f"{exchange_id} API key and secret are required for live CCXT execution")

        try:
            import ccxt
        except ImportError as e:
            raise RuntimeError("ccxt is required for live crypto/perp execution") from e

        exchange_cls = getattr(ccxt, exchange_id)
        cfg = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": default_type},
        }
        if password:
            cfg["password"] = password

        self.name = name or f"ccxt:{exchange_id}:{default_type}"
        self.mode = "live"
        self.exchange_id = exchange_id
        self.default_type = default_type
        self._exchange = exchange_cls(cfg)
        self._lock = threading.Lock()
        logger.info(f"CcxtBroker | {self.name} configured")

    @classmethod
    def from_env(cls, prefix: str, default_exchange: str, default_type: str, name: str):
        exchange_id = os.getenv(f"{prefix}_EXCHANGE_ID", default_exchange).strip() or default_exchange
        api_key = (
            os.getenv(f"{prefix}_API_KEY", "").strip()
            or os.getenv("BINANCE_API_KEY", "").strip()
        )
        secret = (
            os.getenv(f"{prefix}_API_SECRET", "").strip()
            or os.getenv(f"{prefix}_SECRET", "").strip()
            or os.getenv("BINANCE_API_SECRET", "").strip()
            or os.getenv("BINANCE_SECRET", "").strip()
        )
        password = os.getenv(f"{prefix}_API_PASSWORD", "").strip()
        return cls(exchange_id, api_key, secret, password, default_type, name)

    @staticmethod
    def env_has_keys(prefix: str) -> bool:
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip() or os.getenv("BINANCE_API_KEY", "").strip()
        secret = (
            os.getenv(f"{prefix}_API_SECRET", "").strip()
            or os.getenv(f"{prefix}_SECRET", "").strip()
            or os.getenv("BINANCE_API_SECRET", "").strip()
            or os.getenv("BINANCE_SECRET", "").strip()
        )
        return bool(api_key and secret)

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
        amount = float(quantity)
        if amount <= 0:
            raise ValueError(f"quantity must be > 0 for {exchange}:{symbol}")

        side = "buy" if order_type.upper() == "BUY" else "sell"
        order_kind = "market" if order_mode.upper() == "MARKET" else "limit"
        order_price = None if order_kind == "market" else float(price)
        if order_kind == "limit" and (order_price is None or order_price <= 0):
            raise ValueError(f"limit price must be > 0 for {exchange}:{symbol}")

        params = {}
        client_id = self._safe_client_order_id(client_order_id)
        if client_id and self.exchange_id == "binance":
            params["newClientOrderId"] = client_id

        with self._lock:
            result = self._exchange.create_order(symbol, order_kind, side, amount, order_price, params)

        normalized = {
            "order_id": result.get("id") or client_order_id or f"LIVE_{datetime.now().strftime('%H%M%S%f')}",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "exchange": exchange,
            "order_type": order_type,
            "quantity": amount,
            "price": order_price or 0,
            "product": product,
            "order_mode": order_mode,
            "status": result.get("status", "submitted"),
            "timestamp": datetime.now().isoformat(),
            "broker": self.name,
            "raw": result,
        }
        logger.info(f"LIVE CCXT ORDER | {exchange} | {order_type} {amount:g} {symbol} @ {price}")
        return normalized

    def cancel_order(self, order_id: str) -> dict:
        raise NotImplementedError("cancel_order requires symbol-specific routing for CCXT")

    def get_positions(self) -> list:
        try:
            if hasattr(self._exchange, "fetch_positions"):
                return self._exchange.fetch_positions()
        except Exception as e:
            logger.debug(f"CcxtBroker | fetch_positions failed: {e}")
        return []

    def get_order_history(self) -> list:
        try:
            return self._exchange.fetch_orders()
        except Exception as e:
            logger.debug(f"CcxtBroker | fetch_orders failed: {e}")
        return []

    @staticmethod
    def _safe_client_order_id(client_order_id: str) -> str:
        if not client_order_id:
            return ""
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", client_order_id)
        return cleaned[:36]
