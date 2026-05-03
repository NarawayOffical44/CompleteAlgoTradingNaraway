"""
Regime Detector — Classifies market regime using LLM.
Provider priority: Groq → OpenRouter → HMM fallback (no API needed).

Model: llama-3.1-8b-instant on Groq (fast, near-free).
Output: BULL_LOW_VOL | BULL_HIGH_VOL | BEAR_LOW_VOL | BEAR_HIGH_VOL | CHOPPY
"""

import json
import requests
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

_PROMPT_TEMPLATE = """You are a systematic trading regime classifier for Indian equity markets.

Current market data:
- India VIX: {india_vix}
- VIX 7-day change: {india_vix_7d_change}
- Nifty 1-day return: {nifty_1d_return}%
- Nifty 5-day return: {nifty_5d_return}%
- Nifty vs 200 DMA: {nifty_vs_200dma_pct}%
- Put/Call ratio: {put_call_ratio}
- FII net flow (Cr): {fii_net_flow_cr}
- Advance/Decline ratio: {advance_decline_ratio}
- Days to next major event: {days_to_next_major_event}

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


def _call_openai_compat(base_url: str, api_key: str, model: str, prompt: str) -> dict:
    """Call any OpenAI-compatible endpoint."""
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


class RegimeDetector:

    def __init__(self):
        self._groq_key       = config.groq_api_key or ""
        self._openrouter_key = config.openrouter_api_key or ""
        self.current_regime  = "UNKNOWN"
        self.last_analysis   = {}

        if self._groq_key:
            logger.info("RegimeDetector | provider=Groq | model=llama-3.1-8b-instant")
        elif self._openrouter_key:
            logger.info("RegimeDetector | provider=OpenRouter | model=meta-llama/llama-3.1-8b-instruct")
        else:
            logger.info("RegimeDetector | no LLM key — HMM fallback only")

    def detect(self, market_snapshot: dict) -> dict:
        prompt = _PROMPT_TEMPLATE.format(
            india_vix=market_snapshot.get("india_vix", "N/A"),
            india_vix_7d_change=market_snapshot.get("india_vix_7d_change", "N/A"),
            nifty_1d_return=market_snapshot.get("nifty_1d_return", "N/A"),
            nifty_5d_return=market_snapshot.get("nifty_5d_return", "N/A"),
            nifty_vs_200dma_pct=market_snapshot.get("nifty_vs_200dma_pct", "N/A"),
            put_call_ratio=market_snapshot.get("put_call_ratio", "N/A"),
            fii_net_flow_cr=market_snapshot.get("fii_net_flow_cr", "N/A"),
            advance_decline_ratio=market_snapshot.get("advance_decline_ratio", "N/A"),
            days_to_next_major_event=market_snapshot.get("days_to_next_major_event", "N/A"),
        )

        result = self._try_groq(prompt) or self._try_openrouter(prompt)

        if result:
            self.current_regime = result.get("regime", "UNKNOWN")
            self.last_analysis  = result
            logger.info(f"Regime: {self.current_regime} | confidence={result.get('confidence')}")
            return result

        logger.warning("RegimeDetector | all providers failed — returning UNKNOWN")
        self.current_regime = "UNKNOWN"
        return {"regime": "UNKNOWN", "confidence": 0, "key_factors": [], "risks": ["no llm available"]}

    def get_allocation_multipliers(self) -> dict:
        return REGIME_ALLOCATIONS.get(self.current_regime, REGIME_ALLOCATIONS["UNKNOWN"])

    # ── Providers ─────────────────────────────────────────────────────────
    def _try_groq(self, prompt: str) -> dict | None:
        if not self._groq_key:
            return None
        try:
            result = _call_openai_compat(
                base_url="https://api.groq.com/openai/v1",
                api_key=self._groq_key,
                model="llama-3.1-8b-instant",
                prompt=prompt,
            )
            logger.debug("RegimeDetector | Groq OK")
            return result
        except Exception as e:
            logger.warning(f"RegimeDetector | Groq failed: {e}")
            return None

    def _try_openrouter(self, prompt: str) -> dict | None:
        if not self._openrouter_key:
            return None
        try:
            result = _call_openai_compat(
                base_url="https://openrouter.ai/api/v1",
                api_key=self._openrouter_key,
                model="meta-llama/llama-3.1-8b-instruct:free",
                prompt=prompt,
            )
            logger.debug("RegimeDetector | OpenRouter OK")
            return result
        except Exception as e:
            logger.warning(f"RegimeDetector | OpenRouter failed: {e}")
            return None
