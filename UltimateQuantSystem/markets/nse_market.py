"""
NSEMarket — NSE India market implementation.

Handles:
  - Market hours (9:15–15:30 IST, weekdays only)
  - Data: NSE → Yahoo Finance fallback (5-min cache)
  - Regime: HMM (always) + Claude (optional, once/day)
  - Fundamentals: Screener.in (24h cache)
  - Sentiment: NewsAPI (2h cache)
  - Allocation per agent per regime + trend dampener
  - News filter (market-level safety check)

Add a new market: copy this file, inherit BaseMarket, change everything below.
"""

import threading
from datetime import datetime, date
from loguru import logger

from markets.base_market import BaseMarket
from ai.hmm_regime import HMMRegimeDetector, HMMState
from ai.regime_detector import RegimeDetector
from ai.news_filter import NewsFilter
from ai.sentiment_engine import SentimentEngine
from data import MarketDataFetcher, FundamentalsData


_HMM_TO_REGIME = {
    HMMState.BULL_LOW_VOL:  "BULL_LOW_VOL",
    HMMState.CHOPPY:        "CHOPPY",
    HMMState.BEAR_HIGH_VOL: "BEAR_HIGH_VOL",
}

# Allocation per agent per regime
# To change allocation for a specific bot, edit here only.
_ALLOC_TABLE = {
    "BULL_LOW_VOL": {
        "pairs_trading":    1.0,
        "mean_reversion":   0.8,
        "momentum":         1.0,
        "momentum_scalper": 1.0,
        "options_bot":      1.0,
    },
    "CHOPPY": {
        "pairs_trading":    1.0,
        "mean_reversion":   1.0,
        "momentum":         0.3,
        "momentum_scalper": 0.0,
        "options_bot":      0.8,
    },
    "BEAR_HIGH_VOL": {
        "pairs_trading":    0.7,
        "mean_reversion":   0.0,
        "momentum":         0.0,
        "momentum_scalper": 0.0,
        "options_bot":      0.0,
    },
}


class NSEMarket(BaseMarket):

    market_id = "NSE"

    def __init__(self):
        self.market_fetcher = MarketDataFetcher()
        self.fundamentals   = FundamentalsData()
        self._sentiment     = SentimentEngine()
        self.news_filter    = NewsFilter()
        self.regime_hmm     = HMMRegimeDetector()
        self.regime_claude  = RegimeDetector()

        self.current_regime   = "BULL_LOW_VOL"
        self.current_hmm      = HMMState.BULL_LOW_VOL
        self._claude_active   = False
        self._last_claude_run = None
        self._last_fund_date  = None
        self._fund_cache      = {}

        self._lock = threading.Lock()

    # ── Market hours ──────────────────────────────────────────────────────
    def is_open(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:          # Saturday / Sunday
            return False
        t = now.hour * 60 + now.minute
        return 9 * 60 + 15 <= t <= 15 * 60 + 30

    # ── Safety check ──────────────────────────────────────────────────────
    def is_safe(self) -> tuple[bool, str]:
        return self.news_filter.is_safe_to_trade()

    # ── Market data (5-min cached — all bots share same cached response) ──
    def get_data(self) -> dict:
        return self.market_fetcher.get_market_data()

    # ── Fundamentals (24h cached) ─────────────────────────────────────────
    def get_fundamentals(self, symbols: list) -> dict:
        today = date.today()
        with self._lock:
            if self._last_fund_date != today:
                self._fund_cache     = self.fundamentals.get_batch(symbols)
                self._last_fund_date = today
        return self._fund_cache

    # ── Sentiment ─────────────────────────────────────────────────────────
    def get_sentiment(self, symbols: list, market_data: dict) -> tuple[dict, dict]:
        stock_sent = self._sentiment.get_batch(symbols)
        mkt_sent   = self._sentiment.market_sentiment(market_data)
        return stock_sent, mkt_sent

    # ── Regime (HMM always + Claude once/day) ────────────────────────────
    def get_regime(self, market_data: dict = None) -> str:
        if market_data is None:
            market_data = self.get_data()

        nifty_closes = market_data.get("NIFTY", {}).get("closes", [])
        if len(nifty_closes) >= 20:
            self.current_hmm, hmm_conf = self.regime_hmm.predict(nifty_closes)
            logger.debug(f"NSEMarket | HMM={self.current_hmm.name} conf={hmm_conf:.2f}")

        today = date.today()
        with self._lock:
            run_claude = (self._last_claude_run != today)
            if run_claude:
                self._last_claude_run = today
                self._claude_active   = False

        if run_claude:
            try:
                snapshot = self._build_snapshot(market_data)
                result   = self.regime_claude.detect(snapshot)
                regime   = result.get("regime", "UNKNOWN")
                if regime and regime != "UNKNOWN":
                    with self._lock:
                        self.current_regime = regime
                        self._claude_active = True
                    logger.info(f"NSEMarket | Claude regime={regime}")
                else:
                    with self._lock:
                        self.current_regime = _HMM_TO_REGIME.get(self.current_hmm, "BULL_LOW_VOL")
                    logger.info(f"NSEMarket | Claude→UNKNOWN, HMM fallback={self.current_regime}")
            except Exception as e:
                with self._lock:
                    self.current_regime = _HMM_TO_REGIME.get(self.current_hmm, "BULL_LOW_VOL")
                logger.warning(f"NSEMarket | Claude error: {e} → HMM fallback={self.current_regime}")

        return self.current_regime

    # ── Allocation (regime table + trend dampener) ────────────────────────
    def get_allocation(self, agent_id: str, regime: str, market_data: dict) -> float:
        alloc = _ALLOC_TABLE.get(regime, {}).get(agent_id, 1.0)

        # Trend dampener — only affects mean_reversion
        if agent_id == "mean_reversion":
            nifty_5d = market_data.get("NIFTY", {}).get("5d_return", 0.0)
            if nifty_5d < -2.0:
                alloc = 0.0
                logger.info(f"NSEMarket | trend dampener: Nifty 5d={nifty_5d:.1f}% → mean_reversion=0")
            elif nifty_5d < -1.0:
                alloc = min(alloc, 0.4)
                logger.info(f"NSEMarket | trend dampener: Nifty 5d={nifty_5d:.1f}% → mean_reversion=0.4")
            elif nifty_5d > 2.5:
                alloc = min(alloc, 0.5)

        return alloc

    # ── Internal ──────────────────────────────────────────────────────────
    @staticmethod
    def _build_snapshot(market_data: dict) -> dict:
        nifty = market_data.get("NIFTY", {})
        return {
            "india_vix":                market_data.get("VIX",   {}).get("ltp",      15),
            "india_vix_7d_change":      market_data.get("VIX",   {}).get("7d_change", 0),
            "nifty_1d_return":          nifty.get("1d_return",  0),
            "nifty_5d_return":          nifty.get("5d_return",  0),
            "nifty_vs_200dma_pct":      nifty.get("vs_200dma",  0),
            "put_call_ratio":           market_data.get("PCR",          1.0),
            "fii_net_flow_cr":          market_data.get("FII_FLOW",     0),
            "advance_decline_ratio":    market_data.get("ADR",          1.0),
            "days_to_next_major_event": market_data.get("DAYS_TO_EVENT", 15),
        }
