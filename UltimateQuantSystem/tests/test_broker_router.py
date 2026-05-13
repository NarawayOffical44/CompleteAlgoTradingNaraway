from agents.base_agent import BaseAgent
from broker import BrokerRouter, PaperBroker
from journal import TradeJournal
from risk.engine import RiskEngine


class DummyBroker:
    name = "dummy"

    def __init__(self):
        self.orders = []

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"order_id": "dummy-1", "status": "ok"}

    def cancel_order(self, order_id):
        return {"status": "cancelled", "order_id": order_id}

    def get_positions(self):
        return []

    def get_order_history(self):
        return self.orders


class FractionalAgent(BaseAgent):
    _exchange = "CRYPTO"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ran = False

    def generate_signals(self, market_data):
        if self._ran:
            return []
        self._ran = True
        return [{
            "symbol": "BTC/USDT",
            "direction": "long",
            "entry_price": 100.0,
            "quantity": 0.001,
            "risk_amount": 10.0,
            "thesis": "fractional test",
        }]

    def should_exit(self, trade_id, market_data):
        return False, ""


def test_router_routes_by_exchange():
    default = DummyBroker()
    crypto = DummyBroker()
    router = BrokerRouter(default_broker=default)
    router.register("CRYPTO", crypto)

    router.place_order("BTC/USDT", "CRYPTO", "BUY", 0.01, 100.0)
    router.place_order("RELIANCE", "NSE", "BUY", 1, 100.0)

    assert len(crypto.orders) == 1
    assert crypto.orders[0]["exchange"] == "CRYPTO"
    assert len(default.orders) == 1
    assert default.orders[0]["exchange"] == "NSE"


def test_paper_broker_accepts_fractional_quantity():
    broker = PaperBroker("test-paper")
    order = broker.place_order("BTC/USDT", "CRYPTO", "BUY", 0.001, 100.0)

    assert order["quantity"] == 0.001
    assert broker.get_positions()[0]["qty"] == 0.001


def test_base_agent_preserves_fractional_quantity(tmp_path):
    broker = DummyBroker()
    risk = RiskEngine(starting_capital=10_000)
    journal = TradeJournal(journal_dir=str(tmp_path))
    agent = FractionalAgent("fractional", risk, journal, broker)

    agent.run({"BTC/USDT": {"ltp": 100.0}})

    assert broker.orders[0]["quantity"] == 0.001
