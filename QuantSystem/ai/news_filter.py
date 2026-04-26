"""
News & Event Filter — Blocks trading before high-risk events.
Uses NewsAPI + Claude to assess event risk.
"""

import json
import requests
from datetime import datetime, timedelta
from anthropic import Anthropic
from config import config
from loguru import logger


# Known recurring high-risk events (India)
SCHEDULED_EVENTS = [
    {"name": "RBI Monetary Policy", "frequency": "bimonthly"},
    {"name": "Union Budget", "frequency": "annual", "month": 2},
    {"name": "US Fed Decision", "frequency": "8x per year"},
    {"name": "Nifty/BankNifty Expiry", "frequency": "weekly"},
    {"name": "India CPI", "frequency": "monthly"},
    {"name": "India GDP", "frequency": "quarterly"},
]


class NewsFilter:

    def __init__(self):
        api_key = config.anthropic_api_key or ""
        _key_valid = bool(api_key and not api_key.startswith("sk-ant-your"))
        self.client = Anthropic(api_key=api_key) if _key_valid else None
        self.news_api_key = config.news_api_key
        self._cache: dict = {}
        self._cache_time: datetime = None

    def is_safe_to_trade(self) -> tuple[bool, str]:
        """
        Returns (safe, reason).
        Cache refreshes every 4 hours.
        """
        now = datetime.now()
        if self._cache_time and (now - self._cache_time).seconds < 14400:
            return self._cache.get("safe", True), self._cache.get("reason", "cached")

        headlines = self._fetch_headlines()
        if not headlines:
            return True, "No news data — proceeding with caution"

        result = self._assess_risk(headlines)
        self._cache = result
        self._cache_time = now
        return result["safe"], result["reason"]

    def _fetch_headlines(self) -> list[str]:
        if not self.news_api_key:
            return []
        try:
            url = (
                f"https://newsapi.org/v2/top-headlines"
                f"?country=in&category=business&pageSize=20&apiKey={self.news_api_key}"
            )
            r = requests.get(url, timeout=10)
            articles = r.json().get("articles", [])
            return [a["title"] for a in articles if a.get("title")]
        except Exception as e:
            logger.warning(f"NewsAPI fetch failed: {e}")
            return []

    def _assess_risk(self, headlines: list[str]) -> dict:
        prompt = f"""You are a risk filter for a systematic trading bot operating in Indian equity markets.

Today: {datetime.now().strftime('%Y-%m-%d')}

Recent news headlines:
{chr(10).join(f'- {h}' for h in headlines[:15])}

Assess whether it is safe to run automated trading strategies TODAY.

High-risk conditions (return safe=false):
- RBI or Fed rate decision today or tomorrow
- Union Budget day
- Major geopolitical event (war escalation, sanctions)
- Indian market circuit breaker triggered recently
- Extreme volatility event in progress

Return ONLY valid JSON:
{{
    "safe": true,
    "risk_level": "low|medium|high|extreme",
    "reason": "One sentence explanation",
    "events_detected": ["event1"],
    "recommendation": "trade_normal|trade_reduced|avoid_options|stay_flat"
}}"""

        if not self.client:
            return {"safe": True, "risk_level": "unknown", "reason": "no api key", "recommendation": "trade_reduced"}

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                timeout=30,
                messages=[{"role": "user", "content": prompt}],
            )
            result = json.loads(response.content[0].text)
            logger.info(f"News filter: safe={result['safe']} | {result['reason']}")
            return result
        except Exception as e:
            logger.warning(f"News filter assessment failed: {e}")
            return {"safe": True, "risk_level": "unknown", "reason": str(e), "recommendation": "trade_reduced"}
