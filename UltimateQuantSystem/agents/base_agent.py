"""
BaseAgent — All trading agents inherit from this.
Enforces: risk check → journal open → execute → journal close flow.
"""

from abc import ABC, abstractmethod
from datetime import datetime
import uuid
from risk import RiskEngine, RiskMode
from journal import TradeJournal
from broker import DhanClient
from loguru import logger


class BaseAgent(ABC):

    def __init__(self, agent_id: str, risk_engine: RiskEngine, journal: TradeJournal, broker: DhanClient):
        self.agent_id = agent_id
        self.risk = risk_engine
        self.journal = journal
        self.broker = broker
        self.active = True
        logger.info(f"Agent initialized | {agent_id}")

    # ── Implement in each agent ───────────────────────────────────────────
    @abstractmethod
    def generate_signals(self, market_data: dict) -> list[dict]:
        """Return list of signal dicts: {symbol, direction, entry_price, risk_amount, thesis}"""
        pass

    @abstractmethod
    def should_exit(self, trade_id: str, market_data: dict) -> tuple[bool, str]:
        """Return (exit_now, reason)"""
        pass

    # ── Common execution flow (do not override) ───────────────────────────
    def run(self, market_data: dict, regime: str = "unknown"):
        if not self.active:
            return

        if self.risk.state.mode == RiskMode.STOPPED:
            logger.warning(f"{self.agent_id} | Skipping — kill switch active")
            return

        # Check exits first
        self._check_exits(market_data)

        # Generate and act on new signals
        signals = self.generate_signals(market_data)
        for signal in signals:
            self._execute_signal(signal, regime)

    def _execute_signal(self, signal: dict, regime: str):
        symbol = signal["symbol"]
        direction = signal["direction"]
        entry_price = signal["entry_price"]
        risk_amount = signal["risk_amount"]
        quantity = signal.get("quantity", self._calc_quantity(risk_amount, entry_price))
        thesis = signal.get("thesis", "")

        # Gate 1: Risk engine approval
        approved, reason = self.risk.approve_trade(self.agent_id, risk_amount)
        if not approved:
            logger.info(f"{self.agent_id} | BLOCKED | {symbol} | {reason}")
            return

        # Execute order
        order_type = "BUY" if direction == "long" else "SELL"
        order = self.broker.place_order(
            symbol=symbol,
            exchange="NSE",
            order_type=order_type,
            quantity=int(quantity),
            price=entry_price,
        )

        trade_id = str(uuid.uuid4())[:8]

        # Register with risk engine + journal
        self.risk.register_open(self.agent_id, trade_id, risk_amount)
        self.journal.open_trade(
            trade_id=trade_id,
            agent_id=self.agent_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            quantity=quantity,
            risk_amount=risk_amount,
            thesis=thesis,
            regime=regime,
        )

        logger.info(f"{self.agent_id} | ENTERED | {symbol} {direction} @ {entry_price} | risk={risk_amount:.2f}")

    def _check_exits(self, market_data: dict):
        open_trades = [t for t in self.journal.trades.values()
                       if t.agent_id == self.agent_id and t.status == "open"]

        for trade in open_trades:
            should_exit, reason = self.should_exit(trade.trade_id, market_data)
            if should_exit:
                exit_price = market_data.get(trade.symbol, {}).get("ltp", trade.entry_price)
                order_type = "SELL" if trade.direction == "long" else "BUY"
                self.broker.place_order(
                    symbol=trade.symbol,
                    exchange="NSE",
                    order_type=order_type,
                    quantity=int(trade.quantity),
                    price=exit_price,
                )
                closed = self.journal.close_trade(trade.trade_id, exit_price, reason)
                self.risk.register_close(self.agent_id, trade.trade_id, closed.pnl)

    @staticmethod
    def _calc_quantity(risk_amount: float, entry_price: float) -> float:
        """Shares = risk / price (simplified — refine per strategy)"""
        return max(1, risk_amount / entry_price)
