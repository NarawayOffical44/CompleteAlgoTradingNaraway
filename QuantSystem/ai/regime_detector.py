"""
Regime Detector — Uses Claude to classify current market regime.
Output: BULL_LOW_VOL | BULL_HIGH_VOL | BEAR_LOW_VOL | BEAR_HIGH_VOL | CHOPPY

Each regime maps to allocation multipliers per agent.
AI only recommends. Risk engine + rules execute.
"""

import json
from anthropic import Anthropic
from config import config
from loguru import logger


REGIME_ALLOCATIONS = {
    "BULL_LOW_VOL":   {"pairs_trading": 1.0, "mean_reversion": 0.8, "momentum": 1.0, "momentum_scalper": 1.0, "options_bot": 1.0},
    "BULL_HIGH_VOL":  {"pairs_trading": 0.5, "mean_reversion": 0.4, "momentum": 0.7, "momentum_scalper": 0.5, "options_bot": 0.3},
    "BEAR_LOW_VOL":   {"pairs_trading": 0.7, "mean_reversion": 0.2, "momentum": 0.0, "momentum_scalper": 0.0, "options_bot": 0.5},
    "BEAR_HIGH_VOL":  {"pairs_trading": 0.3, "mean_reversion": 0.0, "momentum": 0.0, "momentum_scalper": 0.0, "options_bot": 0.0},
    "CHOPPY":         {"pairs_trading": 1.0, "mean_reversion": 0.8, "momentum": 0.0, "momentum_scalper": 0.0, "options_bot": 0.8},
    "UNKNOWN":        {"pairs_trading": 0.5, "mean_reversion": 0.5, "momentum": 0.0, "momentum_scalper": 0.0, "options_bot": 0.0},
}


class RegimeDetector:

    def __init__(self):
        api_key = config.anthropic_api_key or ""
        self._key_valid = bool(api_key and not api_key.startswith("sk-ant-your"))
        self.client = Anthropic(api_key=api_key) if self._key_valid else None
        self.current_regime = "UNKNOWN"
        self.last_analysis = {}

    def detect(self, market_snapshot: dict) -> dict:
        """
        market_snapshot: {
            "india_vix": 14.5,
            "india_vix_7d_change": 1.2,
            "nifty_1d_return": -0.8,
            "nifty_5d_return": 2.1,
            "nifty_vs_200dma_pct": 3.5,
            "put_call_ratio": 0.95,
            "fii_net_flow_cr": -1200,
            "advance_decline_ratio": 0.8,
            "days_to_next_major_event": 12,
        }
        """
        prompt = f"""You are a systematic trading regime classifier for Indian equity markets.

Current market data:
- India VIX: {market_snapshot.get('india_vix', 'N/A')}
- VIX 7-day change: {market_snapshot.get('india_vix_7d_change', 'N/A')}
- Nifty 1-day return: {market_snapshot.get('nifty_1d_return', 'N/A')}%
- Nifty 5-day return: {market_snapshot.get('nifty_5d_return', 'N/A')}%
- Nifty vs 200 DMA: {market_snapshot.get('nifty_vs_200dma_pct', 'N/A')}%
- Put/Call ratio: {market_snapshot.get('put_call_ratio', 'N/A')}
- FII net flow (Cr): {market_snapshot.get('fii_net_flow_cr', 'N/A')}
- Advance/Decline ratio: {market_snapshot.get('advance_decline_ratio', 'N/A')}
- Days to next major event: {market_snapshot.get('days_to_next_major_event', 'N/A')}

Classify the current regime as exactly one of:
BULL_LOW_VOL | BULL_HIGH_VOL | BEAR_LOW_VOL | BEAR_HIGH_VOL | CHOPPY

Return ONLY valid JSON:
{{
    "regime": "BULL_LOW_VOL",
    "confidence": 0.82,
    "key_factors": ["VIX below 15", "positive FII flows"],
    "risks": ["PCR elevated suggesting hedging"],
    "safe_to_trade_options": true,
    "recommendation": "One sentence on what to do."
}}"""

        if not self._key_valid:
            logger.debug("Regime detection skipped — ANTHROPIC_API_KEY not set")
            self.current_regime = "UNKNOWN"
            return {"regime": "UNKNOWN", "confidence": 0, "key_factors": [], "risks": ["no api key"]}

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                timeout=30,
                messages=[{"role": "user", "content": prompt}],
            )
            result = json.loads(response.content[0].text)
            self.current_regime = result["regime"]
            self.last_analysis = result
            logger.info(f"Regime: {self.current_regime} | confidence={result.get('confidence')}")
            return result

        except Exception as e:
            logger.warning(f"Regime detection failed: {e} — defaulting to UNKNOWN")
            self.current_regime = "UNKNOWN"
            return {"regime": "UNKNOWN", "confidence": 0, "key_factors": [], "risks": [str(e)]}

    def get_allocation_multipliers(self) -> dict:
        """Returns size multipliers for each agent based on current regime."""
        return REGIME_ALLOCATIONS.get(self.current_regime, REGIME_ALLOCATIONS["UNKNOWN"])
