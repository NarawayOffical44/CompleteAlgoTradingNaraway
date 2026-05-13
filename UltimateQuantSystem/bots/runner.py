"""
BotRunner — Wraps any BaseAgent and runs it independently in its own thread.

Each BotRunner:
  - Has its own market (NSE, Crypto, Forex, Solana — pluggable)
  - Checks if that market is open before each tick
  - Fetches its own data (5-min cache means no duplicate network calls)
  - Gets allocation from market × head_ai_mult (HeadAI can reduce/boost)
  - Runs the agent with correct risk scaling
  - Reports status, PnL, errors to BotRegistry

interval_s:
  Default 900s (15 min) for all standard bots.
  Set to 120s for MemeSniper (needs 2-min polling).
  Set to any value per-bot: BotRunner(agent=..., interval_s=60)

Suspension:
  HeadAI or BotRegistry can call:
    runner.suspended = True
    runner.suspended_reason = "..."
    runner.suspended_until = datetime(...)
  The runner then skips all ticks until resumed.

head_ai_mult:
  HeadAI sets runner.head_ai_mult (0.0–1.0).
  Final alloc = market_alloc × head_ai_mult.
  Persists across ticks until HeadAI changes it.
  Default 1.0 (no change).
"""

import time
import threading
from datetime import datetime
from loguru import logger

DEFAULT_INTERVAL_S = 15 * 60   # 15 minutes — standard bots
_STOP_CHECK_S      = 5          # check stop flag every 5s


class BotRunner:

    def __init__(self, agent, market, risk_engine, run_fn=None,
                 interval_s: int = DEFAULT_INTERVAL_S, resources=None):
        """
        agent      : any BaseAgent subclass (or options_bot / standalone)
        market     : BaseMarket subclass — defines hours, data, regime, alloc
        risk_engine: shared RiskEngine (thread-safe)
        run_fn     : optional callable(agent, market_data, regime) — for custom run signatures
                     defaults to agent.run(market_data, regime=regime)
        interval_s : seconds between signal checks (default 900 = 15 min;
                     use 120 for MemeSniper, 60 for scalpers)
        """
        self.agent       = agent
        self.agent_id    = getattr(agent, "agent_id", getattr(agent, "underlying", "unknown"))
        self.market      = market
        self.risk        = risk_engine
        self.resources   = resources
        self._run_fn     = run_fn or (lambda a, data, regime: a.run(data, regime=regime))
        self._interval_s = interval_s
        if resources is not None:
            setattr(self.agent, "resources", resources)

        # ── HeadAI controls ───────────────────────────────────────────────
        self.head_ai_mult    = 1.0
        self.head_ai_note    = ""

        # ── Suspension (set by HeadAI or BotRegistry) ────────────────────
        self.suspended        = False
        self.suspended_reason = ""
        self.suspended_until  = None

        # ── Status (read by registry + dashboard) ────────────────────────
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
        logger.info(f"BotRunner | {self.agent_id} | started on {self.market.market_id} | interval={self._interval_s}s")

    def stop(self, join: bool = False, timeout: float = 10.0):
        self._stop_event.set()
        logger.info(f"BotRunner | {self.agent_id} | stop requested")
        if join and self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

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
                logger.exception(f"BotRunner | {self.agent_id} | loop error: {e}")

            elapsed = 0
            while elapsed < self._interval_s and not self._stop_event.is_set():
                time.sleep(_STOP_CHECK_S)
                elapsed += _STOP_CHECK_S

        self.status = "stopped"
        logger.info(f"BotRunner | {self.agent_id} | stopped")

    # ── Single tick ───────────────────────────────────────────────────────
    def _tick(self):
        self.runs_total += 1
        self.last_run = datetime.now().strftime("%H:%M:%S")

        # 0. Suspension check
        if self.suspended:
            self.status = "suspended"
            if self.suspended_until and datetime.now() >= self.suspended_until:
                self.suspended        = False
                self.suspended_reason = ""
                self.suspended_until  = None
                self.head_ai_mult     = 1.0
                logger.info(f"BotRunner | {self.agent_id} | auto-resumed after suspension")
            else:
                logger.debug(f"BotRunner | {self.agent_id} | suspended ({self.suspended_reason}), skipping tick")
                return

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

        # 3. Fetch market data — cached, all bots in same market share cache
        market_data = self.market.get_data()
        market_data = dict(market_data)
        has_tradeable_data = any(
            not k.startswith("_") and isinstance(v, dict) and v
            for k, v in market_data.items()
        )
        if not has_tradeable_data:
            self.status = "no_data"
            logger.warning(f"{self.agent_id} | no tradeable market data from {self.market.market_id}")
            return

        # 4. Get regime
        regime = self.market.get_regime(market_data)

        # 5. Check market allocation for this bot in this regime
        market_alloc = self.market.get_allocation(self.agent_id, regime, market_data)

        # 6. Apply HeadAI multiplier
        alloc = market_alloc * self.head_ai_mult
        if alloc <= 0.0:
            reason = "ai_reduced" if self.head_ai_mult < 1.0 else regime
            self.status = f"skip:{reason}"
            logger.info(f"{self.agent_id} | alloc=0 — skipping")
            return

        # 7. Set per-agent allocation in risk engine
        self.risk.set_agent_alloc_mult(self.agent_id, alloc)

        # 8. Enrich market_data with context agents expect
        equity_syms = [k for k in market_data
                       if not k.startswith("_")
                       and k not in ("NIFTY", "VIX", "PCR", "FII_FLOW", "ADR", "DAYS_TO_EVENT")]

        market_data["_regime"]           = regime
        market_data["_fundamentals"]     = self.market.get_fundamentals(equity_syms)
        stock_sent, mkt_sent             = self.market.get_sentiment(equity_syms, market_data)
        market_data["_sentiment"]        = stock_sent
        market_data["_market_sentiment"] = mkt_sent

        # 9. Run agent
        self._run_fn(self.agent, market_data, regime)

        # 10. Update status
        elapsed              = time.time() - t0
        self.status          = "running"
        self.error_count     = 0
        self.last_error      = None
        self.last_run        = datetime.now().strftime("%H:%M:%S")
        self.last_run_time_s = round(elapsed, 1)

        logger.info(
            f"{self.agent_id} | tick done in {elapsed:.1f}s | regime={regime} | "
            f"alloc={alloc:.2f} (mkt={market_alloc:.2f} × ai={self.head_ai_mult:.2f})"
        )

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
            "suspended":        self.suspended,
            "suspended_reason": self.suspended_reason,
            "suspended_until":  self.suspended_until.strftime("%H:%M %d-%b") if self.suspended_until else None,
            "head_ai_mult":     round(self.head_ai_mult, 2),
            "head_ai_note":     self.head_ai_note,
            "error_count":      self.error_count,
            "last_error":       self.last_error,
            "last_run":         self.last_run,
            "last_run_time_s":  self.last_run_time_s,
            "runs_total":       self.runs_total,
            "performance_mult": perf_mult,
            **summary,
        }
