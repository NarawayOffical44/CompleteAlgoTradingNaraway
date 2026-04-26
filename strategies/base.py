"""
Base strategy class — all strategies inherit from this.
"""
import pandas as pd
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    signal: Signal
    entry_price: float
    stop_loss: float
    take_profit: float
    reason: str
    confidence: float = 1.0  # 0-1, for future ML use

    @property
    def risk(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward(self) -> float:
        return abs(self.take_profit - self.entry_price)

    @property
    def rr_ratio(self) -> float:
        return self.reward / self.risk if self.risk > 0 else 0


class BaseStrategy(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.name = "base"

    @abstractmethod
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add indicator columns to the DataFrame."""
        pass

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> TradeSignal:
        """Analyze the latest candle and return a TradeSignal."""
        pass

    def calculate_position_size(self, capital: float, entry: float, stop_loss: float, risk_pct: float) -> float:
        """
        Kelly-inspired position sizing.
        Risk a fixed % of capital per trade.
        Returns quantity of base asset to buy.
        """
        risk_amount = capital * risk_pct
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit == 0:
            return 0
        qty = risk_amount / risk_per_unit
        # Cap at 95% of capital to avoid over-leveraging
        max_qty = (capital * 0.95) / entry
        return min(qty, max_qty)
