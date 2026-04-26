"""
Risk Engine — Portfolio-level risk management.
Every agent must call approve_trade() before placing any order.

Modes:
  NORMAL   → full size allowed
  REDUCED  → 50% size (DD > 8%) + aggressive agents PAUSED
  STOPPED  → kill switch — no trading until manual reset

Aggressive agent rules (extra layer on top of normal limits):
  - Max 0.5% risk per trade (half of normal 1%)
  - Max 10% combined portfolio allocation
  - First to be paused when DD > 8% (REDUCED mode)
"""

from dataclasses import dataclass, field
from enum import Enum
from config import RiskConfig
from loguru import logger


AGGRESSIVE_COMBINED_CAP_PCT = 10.0   # max % of portfolio in aggressive agents combined


class RiskMode(Enum):
    NORMAL  = "normal"
    REDUCED = "reduced"
    STOPPED = "stopped"


@dataclass
class PortfolioState:
    capital: float
    high_watermark: float
    daily_start_capital: float
    open_positions: dict = field(default_factory=dict)    # "agent:trade_id" → risk_amount
    agent_pnl_history: dict = field(default_factory=dict) # agent_id → [daily_pnl]
    mode: RiskMode = RiskMode.NORMAL


class RiskEngine:

    def __init__(self, starting_capital: float, config: RiskConfig = None):
        from config import config as app_config
        self.config = config or app_config.risk
        self.state  = PortfolioState(
            capital=starting_capital,
            high_watermark=starting_capital,
            daily_start_capital=starting_capital,
        )
        self._aggressive_agents: set = set()
        logger.info(f"RiskEngine initialized | capital={starting_capital} | mode=NORMAL")

    # ── Agent registration ────────────────────────────────────────────────
    def register_aggressive_agent(self, agent_id: str):
        """Register an agent as aggressive — tighter limits, paused first on drawdown."""
        self._aggressive_agents.add(agent_id)
        logger.info(f"RiskEngine | {agent_id} registered as AGGRESSIVE (cap={AGGRESSIVE_COMBINED_CAP_PCT}%)")

    # ── Primary gate — call before every trade ────────────────────────────
    def approve_trade(self, agent_id: str, trade_risk_amount: float) -> tuple[bool, str]:
        """
        Returns (approved, reason).
        trade_risk_amount = max you can lose on this trade (not notional size).
        """
        if self.state.mode == RiskMode.STOPPED:
            return False, "KILL SWITCH ACTIVE — manual reset required"

        is_aggressive = agent_id in self._aggressive_agents

        # Aggressive agents are paused first in REDUCED mode
        if is_aggressive and self.state.mode == RiskMode.REDUCED:
            return False, f"Aggressive agents paused — portfolio in REDUCED mode (DD >{self.config.drawdown_warning_pct}%)"

        trade_risk_pct = (trade_risk_amount / self.state.capital) * 100
        max_per_trade  = self.config.max_trade_risk_pct

        # Normal agents: 50% size in REDUCED mode
        if not is_aggressive and self.state.mode == RiskMode.REDUCED:
            max_per_trade *= 0.5

        # Aggressive agents: hard cap at 0.5% per trade regardless of mode
        if is_aggressive:
            max_per_trade = min(max_per_trade, 0.5)

        if trade_risk_pct > max_per_trade:
            return False, f"Trade risk {trade_risk_pct:.2f}% > limit {max_per_trade:.2f}%"

        # Total portfolio open risk cap (all agents)
        open_risk    = sum(self.state.open_positions.values())
        portfolio_cap = self.state.capital * (self.config.max_portfolio_risk_pct / 100)
        if open_risk + trade_risk_amount > portfolio_cap:
            return False, f"Portfolio risk would breach {self.config.max_portfolio_risk_pct}% cap"

        # Aggressive agents: combined cap check (10% of portfolio)
        if is_aggressive:
            agg_open = sum(v for k, v in self.state.open_positions.items()
                           if k.split(":")[0] in self._aggressive_agents)
            agg_cap  = self.state.capital * (AGGRESSIVE_COMBINED_CAP_PCT / 100)
            if agg_open + trade_risk_amount > agg_cap:
                return False, f"Aggressive combined risk {AGGRESSIVE_COMBINED_CAP_PCT}% cap breached"

        return True, "approved"

    # ── Trade lifecycle ───────────────────────────────────────────────────
    def register_open(self, agent_id: str, trade_id: str, risk_amount: float):
        key = f"{agent_id}:{trade_id}"
        self.state.open_positions[key] = risk_amount
        logger.info(f"OPEN | {key} | risk={risk_amount:.2f}")

    def register_close(self, agent_id: str, trade_id: str, pnl: float):
        key = f"{agent_id}:{trade_id}"
        self.state.open_positions.pop(key, None)
        self.state.capital += pnl

        if self.state.capital > self.state.high_watermark:
            self.state.high_watermark = self.state.capital

        self.state.agent_pnl_history.setdefault(agent_id, []).append(pnl)
        logger.info(f"CLOSE | {key} | pnl={pnl:.2f} | capital={self.state.capital:.2f}")
        self._evaluate_mode()

    # ── Daily reset ───────────────────────────────────────────────────────
    def end_of_day(self):
        self.state.daily_start_capital = self.state.capital
        logger.info(f"EOD | capital={self.state.capital:.2f} | mode={self.state.mode.value}")
        self._save_state_snapshot()

    def _save_state_snapshot(self):
        """Write current risk state to logs/risk_state.json for the reporting agent."""
        import json
        from pathlib import Path
        snap = {
            "mode":            self.state.mode.value,
            "capital":         round(self.state.capital, 2),
            "high_watermark":  round(self.state.high_watermark, 2),
            "drawdown_pct":    round(self.status()["drawdown_pct"], 2),
            "daily_loss_pct":  round(self.status()["daily_loss_pct"], 2),
            "open_positions":  len(self.state.open_positions),
            "open_risk":       round(sum(self.state.open_positions.values()), 2),
            "timestamp":       __import__("datetime").datetime.now().isoformat(),
        }
        path = Path("logs/risk_state.json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(snap, indent=2))

    # ── Dynamic per-agent allocation multiplier ───────────────────────────
    def get_agent_performance_multiplier(self, agent_id: str,
                                         lookback: int = 20) -> float:
        """
        Returns a 0.0–1.0 multiplier based on rolling Sharpe over last N trades.
        Well-performing agents get full allocation; underperformers are scaled down.
          Sharpe > 1.5  → 1.0 (full)
          Sharpe 0.8–1.5 → 0.7
          Sharpe 0.0–0.8 → 0.5
          Sharpe < 0.0   → 0.0 (pause)
        """
        hist = self.state.agent_pnl_history.get(agent_id, [])
        if len(hist) < 5:
            return 1.0  # not enough data — give benefit of the doubt

        recent = hist[-lookback:]
        mean   = sum(recent) / len(recent)
        std    = (sum((x - mean) ** 2 for x in recent) / len(recent)) ** 0.5
        sharpe = (mean / std) * (252 ** 0.5) if std > 0 else 0.0

        if sharpe >= 1.5:
            mult = 1.0
        elif sharpe >= 0.8:
            mult = 0.7
        elif sharpe >= 0.0:
            mult = 0.5
        else:
            mult = 0.0

        if mult < 1.0:
            logger.info(f"Dynamic alloc | {agent_id} | sharpe={sharpe:.2f} → mult={mult}")
        return mult

    # ── Correlation check (run daily) ─────────────────────────────────────
    def check_agent_correlation(self) -> dict[str, float]:
        """
        Returns dict of agent pairs whose correlation exceeds threshold.
        Also logs recommended action (reduce allocation of the more correlated agent).
        """
        import itertools
        alerts = {}
        agents = list(self.state.agent_pnl_history.keys())

        for a, b in itertools.combinations(agents, 2):
            hist_a = self.state.agent_pnl_history[a][-30:]
            hist_b = self.state.agent_pnl_history[b][-30:]
            if len(hist_a) < 10 or len(hist_b) < 10:
                continue
            n = min(len(hist_a), len(hist_b))
            corr = self._pearson(hist_a[-n:], hist_b[-n:])
            if abs(corr) > self.config.max_agent_correlation:
                alerts[f"{a}↔{b}"] = round(corr, 3)
                logger.warning(
                    f"CORRELATION ALERT | {a}↔{b} = {corr:.3f} — "
                    f"consider reducing allocation to one of these agents"
                )

        return alerts

    # ── Manual reset (human must review before calling) ───────────────────
    def manual_reset(self, reason: str):
        self.state.mode = RiskMode.NORMAL
        self.state.daily_start_capital = self.state.capital
        logger.warning(f"MANUAL RESET | reason={reason}")

    # ── Status ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        drawdown  = (self.state.high_watermark - self.state.capital) / self.state.high_watermark * 100
        daily_loss = (self.state.daily_start_capital - self.state.capital) / self.state.daily_start_capital * 100
        agg_open   = sum(v for k, v in self.state.open_positions.items()
                         if k.split(":")[0] in self._aggressive_agents)
        return {
            "mode":              self.state.mode.value,
            "capital":           round(self.state.capital, 2),
            "high_watermark":    round(self.state.high_watermark, 2),
            "drawdown_pct":      round(drawdown, 2),
            "daily_loss_pct":    round(daily_loss, 2),
            "open_risk":         round(sum(self.state.open_positions.values()), 2),
            "open_positions":    len(self.state.open_positions),
            "aggressive_open_risk": round(agg_open, 2),
            "aggressive_agents": list(self._aggressive_agents),
        }

    # ── Internal ──────────────────────────────────────────────────────────
    def _evaluate_mode(self):
        drawdown   = (self.state.high_watermark - self.state.capital) / self.state.high_watermark * 100
        daily_loss = (self.state.daily_start_capital - self.state.capital) / self.state.daily_start_capital * 100

        if drawdown >= self.config.drawdown_kill_pct or daily_loss >= self.config.daily_loss_limit_pct:
            self.state.mode = RiskMode.STOPPED
            logger.critical(f"KILL SWITCH | drawdown={drawdown:.1f}% | daily_loss={daily_loss:.1f}%")
        elif drawdown >= self.config.drawdown_warning_pct:
            if self.state.mode != RiskMode.REDUCED:
                logger.warning(
                    f"REDUCED MODE | drawdown={drawdown:.1f}% — "
                    f"aggressive agents paused: {self._aggressive_agents}"
                )
            self.state.mode = RiskMode.REDUCED
        elif self.state.mode == RiskMode.REDUCED:
            recovery = (
                self.state.capital /
                (self.state.high_watermark * (1 - self.config.drawdown_warning_pct / 100)) - 1
            ) * 100
            if recovery >= self.config.recovery_required_pct:
                self.state.mode = RiskMode.NORMAL
                logger.info("NORMAL MODE restored — aggressive agents re-enabled")

    @staticmethod
    def _pearson(x: list, y: list) -> float:
        n = len(x)
        mx, my = sum(x) / n, sum(y) / n
        num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
        den = (sum((x[i] - mx) ** 2 for i in range(n)) *
               sum((y[i] - my) ** 2 for i in range(n))) ** 0.5
        return num / den if den else 0.0
