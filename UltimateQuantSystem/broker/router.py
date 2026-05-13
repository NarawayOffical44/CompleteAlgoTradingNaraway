"""
Execution router.

Bots declare the venue they trade through the exchange argument they pass to
place_order(). The router maps that exchange to the correct execution adapter.
Shared risk/journal/HeadAI remain independent from execution details.
"""

from collections import OrderedDict
from loguru import logger

from config import config
from .dhan_client import DhanClient
from .paper_broker import PaperBroker
from .ccxt_client import CcxtBroker


class BrokerRouter:
    def __init__(self, default_broker=None):
        self.mode = config.trading_mode
        self._default = default_broker or PaperBroker("default-paper")
        self._routes: dict[str, object] = {}
        self._order_routes: dict[str, object] = {}

    def register(self, exchanges, broker) -> None:
        if isinstance(exchanges, str):
            exchanges = [exchanges]
        for exchange in exchanges:
            key = exchange.upper()
            self._routes[key] = broker
            logger.info(f"BrokerRouter | {key} -> {getattr(broker, 'name', broker.__class__.__name__)}")

    def broker_for(self, exchange: str):
        return self._routes.get((exchange or "").upper(), self._default)

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
        broker = self.broker_for(exchange)
        result = broker.place_order(
            symbol=symbol,
            exchange=exchange,
            order_type=order_type,
            quantity=quantity,
            price=price,
            product=product,
            order_mode=order_mode,
            client_order_id=client_order_id,
        )
        order_id = result.get("order_id") or client_order_id
        if order_id:
            self._order_routes[order_id] = broker
        if client_order_id:
            self._order_routes[client_order_id] = broker
        return result

    def cancel_order(self, order_id: str) -> dict:
        broker = self._order_routes.get(order_id, self._default)
        return broker.cancel_order(order_id)

    def get_positions(self) -> list:
        positions = []
        for name, broker in self._unique_brokers().items():
            try:
                for pos in broker.get_positions():
                    if isinstance(pos, dict):
                        pos = dict(pos)
                        pos.setdefault("broker", name)
                    positions.append(pos)
            except Exception as e:
                logger.warning(f"BrokerRouter | positions failed for {name}: {e}")
        return positions

    def get_order_history(self) -> list:
        orders = []
        for name, broker in self._unique_brokers().items():
            try:
                for order in broker.get_order_history():
                    if isinstance(order, dict):
                        order = dict(order)
                        order.setdefault("broker", name)
                    orders.append(order)
            except Exception as e:
                logger.warning(f"BrokerRouter | orders failed for {name}: {e}")
        return orders

    def routes(self) -> dict:
        return {
            exchange: getattr(broker, "name", broker.__class__.__name__)
            for exchange, broker in self._routes.items()
        }

    def _unique_brokers(self) -> OrderedDict:
        brokers = OrderedDict()
        brokers[getattr(self._default, "name", "default")] = self._default
        for broker in self._routes.values():
            brokers[getattr(broker, "name", broker.__class__.__name__)] = broker
        return brokers


def build_broker_router() -> BrokerRouter:
    paper = PaperBroker("shared-paper")
    router = BrokerRouter(default_broker=paper)

    if config.trading_mode == "live" and not (config.dhan_client_id and config.dhan_access_token):
        logger.warning("BrokerRouter | Dhan live keys missing; NSE/NFO/BSE/BFO routed to paper")
        dhan = paper
    else:
        dhan = DhanClient()
    router.register(["NSE", "NFO", "BSE", "BFO"], dhan)

    if config.trading_mode == "live" and CcxtBroker.env_has_keys("CRYPTO"):
        crypto = CcxtBroker.from_env("CRYPTO", default_exchange="binance", default_type="spot", name="crypto-live")
    else:
        crypto = paper
    router.register("CRYPTO", crypto)

    if config.trading_mode == "live" and CcxtBroker.env_has_keys("PERP"):
        perp = CcxtBroker.from_env("PERP", default_exchange="binance", default_type="future", name="perp-live")
    else:
        perp = paper
    router.register("PERP", perp)

    # These exchanges are routed to paper until concrete live adapters are added.
    # New adapters can be plugged in by calling router.register("EXCHANGE", adapter).
    router.register(["FOREX", "SOLANA", "POLY"], paper)

    logger.info(f"BrokerRouter | routes={router.routes()}")
    return router
