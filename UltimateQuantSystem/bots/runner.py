"""
BotRunner — Wraps any BaseAgent and runs it independently in its own thread.

Each BotRunner:
  - Has its own market (NSE, Crypto, Forex — pluggable)
  - Checks if that market is open before each tick
  - Fetches its own data (5-min cache means no duplicate network calls)
  - Gets allocation from its market based on regime
  - Runs the agent with correct risk scaling
  - Reports status, PnL, errors to BotRegistry

Adding a new bot to a new market:
  runner = BotRunner(agent=MyCryptoBot(), market=CryptoMarket(), risk=risk)
  registry.register(runner)
  Nothing else changes.
"""

import time
import threading
from datetime import datetime
from loguru import logger

SIGNAL_INTERVAL_S = 15 * 60    # run signals every 15 min
_STOP_CHECK_S     = 5           # check stop flag every 5s


class BotRunner:

    def __init__(self, agent, market, risk_engine, run_fn=None):
        """
        agent      : any BaseAgent subclass (or options_bot)
        market     : BaseMarket subclass — defines hours, data, regime, alloc
        risk_engine: shared RiskEngine (thread-safe)
        run_fn     : optional callable(agent, market_data, regime) — for custom run signatures
                     defaults to agent.run(market_data, regime=regime)
        """
        self.agent       = agent
        self.agent_id    = getattr(agent, "agent_id", getattr(agent, "underlying", "unknown"))
        self.market      = market
        self.risk        = risk_engine
        self._run_fn     = run_fn or (lambda a, data, regime: a.run(data, regime=regime))

        # Status (read by registry + dashboard)
        self.status          = "stopped"
        self.error_count     = 0
        self.last_error      = None
        self.last_run        = None
        self.last_run_time_s = None
        self.runs_total      = 0

        self._thread     = None
        self._stop_event = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning(f"BotRunner | {self.agent_id} already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"bot-{self.agent_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"BotRunner | {self.agent_id} | started on {self.market.market_id}")

    def stop(self):
        self._stop_event.set()
        logger.info(f"BotRunner | {self.agent_id} | stop requested")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Main loop ─────────────────────────────────────────────────────────
    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self.status      = "error"
                self.error_count += 1
                self.last_error  = str(e)
                logger.error(f"BotRunner | {self.agent_id} | loop error: {e}")

            # Wait SIGNAL_INTERVAL_S, checking stop flag every 5s
            elapsed = 0
            while elapsed < SIGNAL_INTERVAL_S and not self._stop_event.is_set():
                time.sleep(_STOP_CHECK_S)
                elapsed += _STOP_CHECK_S

        self.status = "stopped"
        logger.info(f"BotRunner | {self.agent_id} | stopped")

    # ── Single tick ───────────────────────────────────────────────────────
    def _tick(self):
        # 1. Check market hours
        if not self.market.is_open():
            self.status = "market_closed"
            return

        # 2. News / safety filter
        safe, reason = self.market.is_safe()
        if not safe:
            self.status = "news_block"
            logger.warning(f"{self.agent_id} | news block: {reason}")
            return

        t0 = time.time()

        # 3. Fetch market data — 5-min cached, all bots in same market share cache
        market_data = self.market.get_data()
        # Each bot gets its own copy to safely add _regime/_fundamentals etc.
        market_data = dict(market_data)

        # 4. Get regime
        regime = self.market.get_regime(market_data)

        # 5. Check allocation for this bot in this regime
        alloc = self.market.get_allocation(self.agent_id, regime, market_data)
        if alloc == 0.0:
            self.status = f"skip:{regime}"
            logger.info(f"{self.agent_id} | alloc=0 in {regime} — skipping")
            return

        # 6. Set per-agent allocation in risk engine (thread-safe)
        self.risk.set_agent_alloc_mult(self.agent_id, alloc)

        # 7. Enrich market_data with context agents expect
        equity_syms = [k for k in market_data
                       if not k.startswith("_")
                       and k not in ("NIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT")]

        market_data["_regime"]           = regime
        market_data["_fundamentals"]     = self.market.get_fundamentals(equity_syms)
        stock_sent, mkt_sent             = self.market.get_sentiment(equity_syms, market_data)
        market_data["_sentiment"]        = stock_sent
        market_data["_market_sentiment"] = mkt_sent

        # 8. Run agent (exits + signals + execution — self-contained)
        self._run_fn(self.agent, market_data, regime)

        # 9. Update status
        elapsed              = time.time() - t0
        self.status          = "running"
        self.error_count     = 0
        self.last_error      = None
        self.last_run        = datetime.now().strftime("%H:%M:%S")
        self.last_run_time_s = round(elapsed, 1)
        self.runs_total     += 1

        logger.info(f"{self.agent_id} | tick done in {elapsed:.1f}s | regime={regime} | alloc={alloc}")

    # ── Metrics (read by registry + HeadAI + dashboard) ──────────────────
    def metrics(self) -> dict:
        journal  = getattr(self.agent, "journal", None)
        try:
            summary = journal.summary(self.agent_id) if journal else {}
        except Exception:
            summary = {}
        perf_mult = self.risk.get_agent_performance_multiplier(self.agent_id)

        return {
            "agent_id":         self.agent_id,
            "market":           self.market.market_id,
            "status":           self.status,
            "error_count":      self.error_count,
            "last_error":       self.last_error,
            "last_run":         self.last_run,
            "last_run_time_s":  self.last_run_time_s,
            "runs_total":       self.runs_total,
            "performance_mult": perf_mult,
            **summary,
        }
