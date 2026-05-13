"""
Shared services for the bot fleet.

These resources are intentionally outside individual bots. A bot may consume
them directly through agent.resources, or indirectly through BotRunner-enriched
market_data fields such as _sentiment and _market_sentiment.
"""

from ai.news_filter import NewsFilter
from ai.sentiment_engine import SentimentEngine


class SharedResourceHub:
    def __init__(self):
        self.news_filter = NewsFilter()
        self.sentiment_engine = SentimentEngine()

    def is_safe_to_trade(self) -> tuple[bool, str]:
        return self.news_filter.is_safe_to_trade()

    def get_symbol_sentiment(self, symbol: str, company_name: str = None) -> dict:
        return self.sentiment_engine.get_sentiment(symbol, company_name)

    def get_batch_sentiment(self, symbols: list) -> dict:
        return self.sentiment_engine.get_batch(symbols)

    def get_market_sentiment(self, market_data: dict) -> dict:
        return self.sentiment_engine.market_sentiment(market_data)
