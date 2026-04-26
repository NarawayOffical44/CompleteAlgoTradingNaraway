"""
AI Orchestrator — Top layer. Coordinates the full pipeline:

  MarketData + Fundamentals + Sentiment
         ↓
  HMM regime (fast, every run — always runs, no API)
  Claude regime (deep, once/day — optional, graceful fallback)
         ↓
  Merged allocation multipliers
         ↓
  Enrich market_data with _regime, _fundamentals, _sentiment, _market_sentiment
         ↓
  Each agent filters using enriched data
         ↓
  Risk Engine is final gate on every trade
         ↓
  Options Bot (regime + sentiment double-gated)

Graceful degradation:
  - No API key   → HMM regime used, full allocations, bots run normally
  - Claude fails  → same fallback, logged as warning
  - News blocked  → Claude/HMM irrelevant, all agents skipped
  - Sentiment err → neutral (score=0), bots still run
"""

from datetime import date
from loguru import logger

from ai.regime_detector import RegimeDetector
from ai.hmm_regime import HMMRegimeDetector, HMMState
from ai.news_filter import NewsFilter
from ai.sentiment_engine import SentimentEngine
from data import MarketDataFetcher, FundamentalsData, NIFTY50_SYMBOLS


# Maps HMM state → Claude-compatible regime name
_HMM_TO_REGIME = {
    HMMState.BULL_LOW_VOL:  "BULL_LOW_VOL",
    HMMState.CHOPPY:        "CHOPPY",
    HMMState.BEAR_HIGH_VOL: "BEAR_HIGH_VOL",
}


class Orchestrator:

    def __init__(self, risk_engine, agents: list, options_bot=None):
        self.risk           = risk_engine
        self.agents         = {a.agent_id: a for a in agents}
        self.options_bot    = options_bot

        # AI layer
        self.regime_claude  = RegimeDetector()
        self.regime_hmm     = HMMRegimeDetector()
        self.news_filter    = NewsFilter()
        self.sentiment      = SentimentEngine()

        # Data layer
        self.market_fetcher = MarketDataFetcher()
        self.fundamentals   = FundamentalsData()

        # State
        self.current_regime       = "UNKNOWN"
        self.current_hmm          = HMMState.BULL_LOW_VOL
        self._claude_active       = False    # True only when Claude gave a real regime
        self.allocation           = {}
        self._last_claude_run     = None
        self._last_fund_date      = None
        self._fundamentals_cache  = {}   # refreshed once per day

    # ── Main entry point ──────────────────────────────────────────────────
    def run(self, market_data: dict = None) -> dict:
        results = {"ran": [], "skipped": [], "reason": ""}

        # 1. Fetch fresh market data if not supplied
        if market_data is None:
            logger.info("Orchestrator | Fetching market data...")
            market_data = self.market_fetcher.get_market_data()

        # 2. News filter (market-level block)
        safe, news_reason = self.news_filter.is_safe_to_trade()
        if not safe:
            logger.warning(f"Orchestrator | NEWS BLOCK | {news_reason}")
            results["reason"] = news_reason
            results["skipped"] = list(self.agents.keys()) + (["options_bot"] if self.options_bot else [])
            return results

        # 3. HMM regime (every run — never fails, no API)
        nifty_closes = market_data.get("NIFTY", {}).get("closes", [])
        if len(nifty_closes) >= 20:
            self.current_hmm, hmm_conf = self.regime_hmm.predict(nifty_closes)
            logger.info(f"HMM | {self.current_hmm.name} | conf={hmm_conf:.2f}")

        # 4. Claude regime (once per day — optional, falls back to HMM)
        today = date.today()
        if self._last_claude_run != today:
            self._last_claude_run = today
            self._claude_active = False
            try:
                snapshot = self._build_snapshot(market_data)
                claude   = self.regime_claude.detect(snapshot)
                regime   = claude.get("regime", "UNKNOWN")
                if regime and regime != "UNKNOWN":
                    self.current_regime = regime
                    self._claude_active = True
                    logger.info(f"Claude regime | {self.current_regime}")
                else:
                    # Claude unavailable or returned UNKNOWN → use HMM
                    self.current_regime = _HMM_TO_REGIME.get(self.current_hmm, "BULL_LOW_VOL")
                    logger.info(f"Claude unavailable → HMM fallback regime: {self.current_regime}")
            except Exception as e:
                self.current_regime = _HMM_TO_REGIME.get(self.current_hmm, "BULL_LOW_VOL")
                logger.warning(f"Claude regime error: {e} → HMM fallback: {self.current_regime}")

        # 5. Merge allocations
        #    - Claude active:     min(HMM, Claude) — conservative
        #    - Claude unavailable: HMM alone — full capability, no penalty
        hmm_alloc = self.regime_hmm.get_allocations()
        if self._claude_active:
            claude_alloc = self.regime_claude.get_allocation_multipliers()
            all_agents   = set(list(hmm_alloc) + list(claude_alloc))
            self.allocation = {
                aid: min(hmm_alloc.get(aid, 1.0), claude_alloc.get(aid, 1.0))
                for aid in all_agents
            }
        else:
            # HMM-only mode: use HMM allocations directly
            self.allocation = dict(hmm_alloc)

        # 5b. Trend dampener for mean_reversion
        #     Strong trends (up or down) are hostile to single-stock mean reversion.
        #     PairsTrading is market-neutral — not affected.
        #     MomentumScalper gets a boost in strong up-trends (already regime-gated).
        nifty_5d = market_data.get("NIFTY", {}).get("5d_return", 0.0)
        if nifty_5d < -2.0:
            # Strong downtrend: completely suppress mean reversion (catching falling knives)
            self.allocation["mean_reversion"] = 0.0
            logger.info(f"Trend dampener | Nifty 5d={nifty_5d:.1f}% — mean_reversion → 0")
        elif nifty_5d < -1.0:
            # Moderate downtrend: halve mean reversion
            self.allocation["mean_reversion"] = min(self.allocation.get("mean_reversion", 1.0), 0.4)
            logger.info(f"Trend dampener | Nifty 5d={nifty_5d:.1f}% — mean_reversion → 0.4")
        elif nifty_5d > 2.5:
            # Strong uptrend: mean reversion less likely to pay off, reduce slightly
            self.allocation["mean_reversion"] = min(self.allocation.get("mean_reversion", 1.0), 0.5)
            logger.info(f"Trend dampener | Nifty 5d={nifty_5d:.1f}% strong bull — mean_reversion → 0.5")

        # 6. Fetch fundamentals (once per day)
        if self._last_fund_date != today:
            logger.info("Orchestrator | Fetching fundamentals...")
            symbols = [k for k in market_data if not k.startswith("_")
                       and k not in ("NIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT")]
            self._fundamentals_cache = self.fundamentals.get_batch(symbols)
            self._last_fund_date = today

        # 7. Fetch per-stock sentiment
        logger.info("Orchestrator | Fetching sentiment...")
        equity_symbols = [k for k in market_data if not k.startswith("_")
                          and k not in ("NIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT")]
        stock_sentiment = self.sentiment.get_batch(equity_symbols)
        mkt_sentiment   = self.sentiment.market_sentiment(market_data)

        # 8. Enrich market_data with all context (agents read from here)
        market_data["_regime"]           = self.current_regime
        market_data["_fundamentals"]     = self._fundamentals_cache
        market_data["_sentiment"]        = stock_sentiment
        market_data["_market_sentiment"] = mkt_sentiment

        logger.info(
            f"Orchestrator | regime={self.current_regime} (claude={'ON' if self._claude_active else 'OFF'}) "
            f"| hmm={self.current_hmm.name} | mkt_sent={mkt_sentiment['score']:.2f} "
            f"| alloc={self.allocation}"
        )

        # 9. Run each agent with scaled risk
        for agent_id, agent in self.agents.items():
            mult = self.allocation.get(agent_id, 1.0)
            if mult == 0.0:
                logger.info(f"{agent_id} | alloc=0 for {self.current_regime} — skip")
                results["skipped"].append(agent_id)
                continue

            original_max = self.risk.config.max_trade_risk_pct
            self.risk.config.max_trade_risk_pct = original_max * mult
            try:
                agent.run(market_data, regime=self.current_regime)
                results["ran"].append(agent_id)
            except Exception as e:
                logger.error(f"{agent_id} crashed: {e}")
            finally:
                self.risk.config.max_trade_risk_pct = original_max

        # 10. Options bot
        if self.options_bot:
            mult = self.allocation.get("options_bot", 0.0)
            if mult > 0:
                try:
                    self.options_bot.run(regime=self.current_regime, market_data=market_data)
                    results["ran"].append("options_bot")
                except Exception as e:
                    logger.error(f"options_bot crashed: {e}")
            else:
                results["skipped"].append("options_bot")

        # 11. Correlation check
        alerts = self.risk.check_agent_correlation()
        if alerts:
            logger.warning(f"Correlation alerts: {alerts}")
            results["correlation_alerts"] = alerts

        return results

    # ── HMM training ──────────────────────────────────────────────────────
    def train_hmm(self, historical_closes: list):
        self.regime_hmm.fit(historical_closes)
        logger.info("HMM trained")

    # ── Status ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "regime_claude":    self.current_regime,
            "claude_active":    self._claude_active,
            "regime_hmm":       self.current_hmm.name,
            "allocation":       self.allocation,
            "risk":             self.risk.status(),
            "market_sentiment": getattr(self.sentiment, "_cache", {}).get("_mkt", {}).get("data", {}),
        }

    # ── Internal ──────────────────────────────────────────────────────────
    @staticmethod
    def _build_snapshot(market_data: dict) -> dict:
        nifty = market_data.get("NIFTY", {})
        return {
            "india_vix":               market_data.get("VIX", {}).get("ltp", 15),
            "india_vix_7d_change":     market_data.get("VIX", {}).get("7d_change", 0),
            "nifty_1d_return":         nifty.get("1d_return", 0),
            "nifty_5d_return":         nifty.get("5d_return", 0),
            "nifty_vs_200dma_pct":     nifty.get("vs_200dma", 0),
            "put_call_ratio":          market_data.get("PCR", 1.0),
            "fii_net_flow_cr":         market_data.get("FII_FLOW", 0),
            "advance_decline_ratio":   market_data.get("ADR", 1.0),
            "days_to_next_major_event": market_data.get("DAYS_TO_EVENT", 15),
        }
