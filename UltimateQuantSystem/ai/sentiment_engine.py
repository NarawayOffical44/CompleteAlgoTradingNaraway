"""
Sentiment Engine — Per-stock news + earnings sentiment scoring.
Upgrades the existing news_filter.py (market-level) to per-symbol scoring.

Output per symbol: sentiment_score (-1.0 to +1.0), sentiment_label, key_events
"""

import json
import time
import requests
from datetime import datetime
from anthropic import Anthropic
from config import config
from loguru import logger

CACHE_TTL_MINS = 120   # Refresh sentiment every 2 hours


class SentimentEngine:

    def __init__(self):
        api_key = config.anthropic_api_key or ""
        _key_valid = bool(api_key and not api_key.startswith("sk-ant-your"))
        self.client   = Anthropic(api_key=api_key) if _key_valid else None
        self.news_key = config.news_api_key
        self._cache: dict = {}   # symbol → {score, label, time, events}

    # ── Per-symbol sentiment ──────────────────────────────────────────────
    def get_sentiment(self, symbol: str, company_name: str = None) -> dict:
        """
        Returns:
        {
            sentiment_score:  float,   # -1.0 (very bearish) to +1.0 (very bullish)
            sentiment_label:  str,     # bullish | neutral | bearish | event_risk
            key_events:       list,    # ["earnings beat", "management change"]
            trade_bias:       str,     # long_bias | short_bias | neutral | avoid
            confidence:       float,
        }
        """
        cached = self._cache.get(symbol)
        if cached:
            age_mins = (time.time() - cached["ts"]) / 60
            if age_mins < CACHE_TTL_MINS:
                return cached["data"]

        headlines = self._fetch_headlines(symbol, company_name)
        if not headlines:
            return self._neutral(symbol)

        result = self._analyze(symbol, headlines)
        self._cache[symbol] = {"data": result, "ts": time.time()}
        return result

    # ── Batch: score entire universe ──────────────────────────────────────
    def get_batch(self, symbols: list) -> dict[str, dict]:
        results = {}
        # Batch analyze to minimize API calls (group 5 symbols per prompt)
        chunks = [symbols[i:i+5] for i in range(0, len(symbols), 5)]
        for chunk in chunks:
            headlines_by_sym = {sym: self._fetch_headlines(sym) for sym in chunk}
            batch_result     = self._analyze_batch(headlines_by_sym)
            results.update(batch_result)
            time.sleep(0.5)
        return results

    # ── Market-level sentiment (for orchestrator) ─────────────────────────
    def market_sentiment(self, market_data: dict) -> dict:
        """
        High-level market mood from FII flows + PCR + ADR.
        No API call needed — pure data-driven.
        """
        fii   = market_data.get("FII_FLOW", 0)
        pcr   = market_data.get("PCR", 1.0)
        adr   = market_data.get("ADR", 1.0)
        vix   = market_data.get("VIX", {}).get("ltp", 15)

        score = 0.0
        score += 0.3 if fii > 1000 else (-0.3 if fii < -1000 else 0)
        score += 0.2 if pcr < 0.8  else (-0.2 if pcr > 1.3 else 0)
        score += 0.2 if adr > 1.5  else (-0.2 if adr < 0.7 else 0)
        score += 0.1 if vix < 13   else (-0.3 if vix > 20 else 0)

        return {
            "score":  round(max(-1.0, min(1.0, score)), 2),
            "label":  "bullish" if score > 0.3 else ("bearish" if score < -0.3 else "neutral"),
            "fii_flow": fii,
            "pcr":      pcr,
            "vix":      vix,
        }

    # ── News fetch ────────────────────────────────────────────────────────
    def _fetch_headlines(self, symbol: str, company_name: str = None) -> list[str]:
        if not self.news_key:
            return []
        query = company_name or symbol
        try:
            url = (f"https://newsapi.org/v2/everything"
                   f"?q={query}+India+stock"
                   f"&language=en&sortBy=publishedAt&pageSize=10"
                   f"&apiKey={self.news_key}")
            r = requests.get(url, timeout=10)
            articles = r.json().get("articles", [])
            return [a["title"] for a in articles if a.get("title")]
        except Exception as e:
            logger.debug(f"News fetch for {symbol}: {e}")
            return []

    # ── Claude analysis: single symbol ───────────────────────────────────
    def _analyze(self, symbol: str, headlines: list[str]) -> dict:
        prompt = f"""Analyze these news headlines for {symbol} (Indian stock).

Headlines:
{chr(10).join(f'- {h}' for h in headlines[:10])}

Return ONLY valid JSON:
{{
    "sentiment_score": 0.0,
    "sentiment_label": "bullish|neutral|bearish|event_risk",
    "key_events": ["event1"],
    "trade_bias": "long_bias|short_bias|neutral|avoid",
    "confidence": 0.75,
    "reason": "One sentence."
}}

sentiment_score: -1.0 (very bearish) to +1.0 (very bullish)
trade_bias avoid: use when major event risk detected (earnings, mgmt change, regulatory)"""

        if not self.client:
            return self._neutral(symbol)

        try:
            resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                timeout=30,
                messages=[{"role": "user", "content": prompt}],
            )
            result = json.loads(resp.content[0].text)
            logger.debug(f"Sentiment {symbol}: {result['sentiment_label']} ({result['sentiment_score']})")
            return result
        except Exception as e:
            logger.warning(f"Sentiment analysis failed for {symbol}: {e}")
            return self._neutral(symbol)

    # ── Claude analysis: batch (cost-efficient) ───────────────────────────
    def _analyze_batch(self, headlines_by_sym: dict) -> dict:
        if not any(headlines_by_sym.values()):
            return {sym: self._neutral(sym) for sym in headlines_by_sym}

        sections = []
        for sym, heads in headlines_by_sym.items():
            if heads:
                sections.append(f"[{sym}]\n" + "\n".join(f"- {h}" for h in heads[:5]))

        prompt = f"""Analyze news sentiment for these Indian stocks.

{chr(10).join(sections)}

Return ONLY a JSON object keyed by symbol:
{{
    "RELIANCE": {{"sentiment_score": 0.5, "sentiment_label": "bullish", "trade_bias": "long_bias", "key_events": [], "confidence": 0.8}},
    "TCS": {{"sentiment_score": -0.2, "sentiment_label": "neutral", "trade_bias": "neutral", "key_events": ["weak guidance"], "confidence": 0.7}}
}}"""

        if not self.client:
            return {sym: self._neutral(sym) for sym in headlines_by_sym}

        try:
            resp = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                timeout=30,
                messages=[{"role": "user", "content": prompt}],
            )
            batch = json.loads(resp.content[0].text)
            # Fill missing symbols with neutral
            for sym in headlines_by_sym:
                if sym not in batch:
                    batch[sym] = self._neutral(sym)
            return batch
        except Exception as e:
            logger.warning(f"Batch sentiment failed: {e}")
            return {sym: self._neutral(sym) for sym in headlines_by_sym}

    @staticmethod
    def _neutral(symbol: str) -> dict:
        return {
            "sentiment_score": 0.0,
            "sentiment_label": "neutral",
            "key_events": [],
            "trade_bias": "neutral",
            "confidence": 0.5,
            "reason": "No news data available",
        }
