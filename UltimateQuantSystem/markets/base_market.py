"""
BaseMarket — Abstract interface every market must implement.

To add a new market (Crypto, Forex, MCX):
  1. Create markets/crypto_market.py
  2. Inherit from BaseMarket
  3. Implement all abstract methods
  4. Register bots with that market in main.py
  Nothing else changes.
"""

from abc import ABC, abstractmethod


class BaseMarket(ABC):

    market_id: str = "base"   # override in subclass e.g. "NSE", "CRYPTO"

    @abstractmethod
    def is_open(self) -> bool:
        """Return True if this market is currently trading."""

    @abstractmethod
    def is_safe(self) -> tuple[bool, str]:
        """Return (safe, reason). If not safe, bots skip this cycle."""

    @abstractmethod
    def get_data(self) -> dict:
        """Return enriched market data dict (cached). All bots call this independently."""

    @abstractmethod
    def get_regime(self, market_data: dict = None) -> str:
        """Return current regime string e.g. BULL_LOW_VOL, CHOPPY, BEAR_HIGH_VOL."""

    @abstractmethod
    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        """Return allocation multiplier (0.0–1.0) for this agent in current regime."""

    @abstractmethod
    def get_fundamentals(self, symbols: list) -> dict:
        """Return fundamentals dict for given symbols (cached)."""

    @abstractmethod
    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        """Return (stock_sentiment, market_sentiment) dicts."""
