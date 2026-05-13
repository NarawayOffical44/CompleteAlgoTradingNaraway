"""
Risk Engine — Portfolio-level risk management.
Thread-safe: all bots call approve_and_open() concurrently without race conditions.

Modes:
  NORMAL   → full size allowed
  REDUCED  → 50% size (DD > 8%) + aggressive agents PAUSED
  STOPPED  → kill switch — no trading until manual reset

Per-agent allocation:
  BotRunner sets allocation multiplier (0.0–1.0) before each tick.
  approve_and_open() applies it atomically inside the lock.
  No shared config mutation — no race conditions.
"""

import threading
from dataclasses import dataclass, field
from enum import Enum
from config import RiskConfig
from loguru import logger


AGGRESSIVE_COMBINED_CAP_PCT = 10.0


class RiskMode(Enum):
    NORMAL  = "normal"
    REDUCED = "reduced"
    STOPPED = "stopped"


@dataclass
class PortfolioState:
    capital:              float
    high_watermark:       float
    daily_start_capital:  float
    open_positions:       dict = field(default_factory=dict)    # "agent:trade_id" → risk_amount
    agent_pnl_history:    dict = field(default_factory=dict)    # agent_id → [daily_pnl]
    mode:                 RiskMode = RiskMode.NORMAL


class RiskEngine:

    def __init__(self, starting_capital: float, config: RiskConfig = None):
        from config import config as app_config
        self.config = config or app_config.risk
        self.state  = PortfolioState(
            capital=starting_capital,
            high_watermark=starting_capital,
            daily_start_capital=starting_capital,
        )
        self._aggressive_agents: set  = set()
        self._agent_alloc_mult:  dict = {}     # agent_id → float (0.0–1.0)
        self._lock = threading.Lock()
        logger.info(f"RiskEngine | capital={starting_capital} | mode=NORMAL | thread-safe=ON")

    # ── Agent registration ────────────────────────────────────────────────
    def register_aggressive_agent(self, agent_id: str):
        with self._lock:
            self._aggressive_agents.add(agent_id)
        logger.info(f"RiskEngine | {agent_id} registered as AGGRESSIVE")

    # ── Per-agent allocation (set by BotRunner before each tick) ─────────
    def set_agent_alloc_mult(self, agent_id: str, mult: float):
        """Thread-safe. BotRunner calls this before running the agent."""
        with self._lock:
            self._agent_alloc_mult[agent_id] = max(0.0, min(1.0, mult))

    # ── ATOMIC approve + register (no TOCTOU race) ────────────────────────
    def approve_and_open(self, agent_id: str, trade_id: str,
                         risk_amount: float) -> tuple[bool, str]:
        """
        Atomically:
          1. Check all risk conditions (with per-agent allocation applied)
          2. If approved, register the open position
          3. Return (approved, reason)

        Call this instead of separate approve_trade() + register_open().
        """
        with self._lock:
            approved, reason = self._check(agent_id, risk_amount)
            if approved:
                key = f"{agent_id}:{trade_id}"
                self.state.open_positions[key] = risk_amount
                logger.info(f"OPEN | {key} | risk={risk_amount:.2f}")
            return approved, reason

    def _check(self, agent_id: str, risk_amount: float) -> tuple[bool, str]:
        """Internal check — caller must hold self._lock."""
        if self.state.mode == RiskMode.STOPPED:
            return False, "KILL SWITCH ACTIVE — manual reset required"

        is_aggressive = agent_id in self._aggressive_agents

        if is_aggressive and self.state.mode == RiskMode.REDUCED:
            return False, (f"Aggressive agents paused — portfolio in REDUCED mode "
                           f"(DD >{self.config.drawdown_warning_pct}%)")

        # Apply per-agent allocation multiplier
        alloc_mult    = self._agent_alloc_mult.get(agent_id, 1.0)
        max_per_trade = self.config.max_trade_risk_pct * alloc_mult

        # Normal agents at 50% size in REDUCED mode
        if not is_aggressive and self.state.mode == RiskMode.REDUCED:
            max_per_trade *= 0.5

        # Aggressive agents: hard cap at 0.5% per trade
        if is_aggressive:
            max_per_trade = min(max_per_trade, 0.5)

        trade_risk_pct = (risk_amount / self.state.capital) * 100
        if trade_risk_pct > max_per_trade:
            return False, f"Trade risk {trade_risk_pct:.2f}% > limit {max_per_trade:.2f}%"

        # Total portfolio open risk cap
        open_risk     = sum(self.state.open_positions.values())
        portfolio_cap = self.state.capital * (self.config.max_portfolio_risk_pct / 100)
        if open_risk + risk_amount > portfolio_cap:
            return False, f"Portfolio risk would breach {self.config.max_portfolio_risk_pct}% cap"

        # Aggressive combined cap
        if is_aggressive:
            agg_open = sum(v for k, v in self.state.open_positions.items()
                           if k.split(":")[0] in self._aggressive_agents)
            agg_cap  = self.state.capital * (AGGRESSIVE_COMBINED_CAP_PCT / 100)
            if agg_open + risk_amount > agg_cap:
                return False, f"Aggressive combined risk {AGGRESSIVE_COMBINED_CAP_PCT}% cap breached"

        return True, "approved"

    # ── Legacy approve_trade (kept for base_agent compatibility) ──────────
    def approve_trade(self, agent_id: str, trade_risk_amount: float) -> tuple[bool, str]:
        """
        Non-atomic version — kept for backward compatibility.
        Prefer approve_and_open() for new code.
        """
        with self._lock:
            return self._check(agent_id, trade_risk_amount)

    # ── Trade lifecycle ───────────────────────────────────────────────────
    def register_open(self, agent_id: str, trade_id: str, risk_amount: float):
        """Legacy — kept for base_agent compatibility. Use approve_and_open() instead."""
        with self._lock:
            key = f"{agent_id}:{trade_id}"
            self.state.open_positions[key] = risk_amount
            logger.info(f"OPEN | {key} | risk={risk_amount:.2f}")

    def cancel_open(self, agent_id: str, trade_id: str, reason: str = ""):
        """Remove a reserved risk slot when order placement fails."""
        with self._lock:
            key = f"{agent_id}:{trade_id}"
            removed = self.state.open_positions.pop(key, None)
        if removed is not None:
            logger.warning(f"CANCEL OPEN | {key} | risk={removed:.2f} | reason={reason}")

    def register_close(self, agent_id: str, trade_id: str, pnl: float):
        with self._lock:
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
        with self._lock:
            self.state.daily_start_capital = self.state.capital
        logger.info(f"EOD | capital={self.state.capital:.2f} | mode={self.state.mode.value}")
        self._save_state_snapshot()

    def _save_state_snapshot(self):
        import json
        from pathlib import Path
        snap = {
            "mode":           self.state.mode.value,
            "capital":        round(self.state.capital, 2),
            "high_watermark": round(self.state.high_watermark, 2),
            "drawdown_pct":   round(self.status()["drawdown_pct"],  2),
            "daily_loss_pct": round(self.status()["daily_loss_pct"], 2),
            "open_positions": len(self.state.open_positions),
            "open_risk":      round(sum(self.state.open_positions.values()), 2),
            "timestamp":      __import__("datetime").datetime.now().isoformat(),
        }
        path = Path("logs/risk_state.json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(snap, indent=2))

    # ── Dynamic per-agent performance multiplier ──────────────────────────
    def get_agent_performance_multiplier(self, agent_id: str, lookback: int = 20) -> float:
        with self._lock:
            hist = self.state.agent_pnl_history.get(agent_id, [])
        if len(hist) < 5:
            return 1.0
        recent = hist[-lookback:]
        mean   = sum(recent) / len(recent)
        std    = (sum((x - mean) ** 2 for x in recent) / len(recent)) ** 0.5
        sharpe = (mean / std) * (252 ** 0.5) if std > 0 else 0.0
        if sharpe >= 1.5:
            return 1.0
        elif sharpe >= 0.8:
            return 0.7
        elif sharpe >= 0.0:
            return 0.5
        return 0.0

    # ── Correlation check ─────────────────────────────────────────────────
    def check_agent_correlation(self) -> dict:
        import itertools
        with self._lock:
            agents = list(self.state.agent_pnl_history.keys())
            hist   = {a: list(self.state.agent_pnl_history[a]) for a in agents}

        alerts = {}
        for a, b in itertools.combinations(agents, 2):
            ha, hb = hist[a][-30:], hist[b][-30:]
            if len(ha) < 10 or len(hb) < 10:
                continue
            n    = min(len(ha), len(hb))
            corr = self._pearson(ha[-n:], hb[-n:])
            if abs(corr) > self.config.max_agent_correlation:
                alerts[f"{a}↔{b}"] = round(corr, 3)
                logger.warning(f"CORRELATION ALERT | {a}↔{b} = {corr:.3f}")
        return alerts

    # ── Manual reset ──────────────────────────────────────────────────────
    def manual_reset(self, reason: str):
        with self._lock:
            self.state.mode = RiskMode.NORMAL
            self.state.daily_start_capital = self.state.capital
        logger.warning(f"MANUAL RESET | reason={reason}")

    # ── Status ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            cap    = self.state.capital
            hwm    = self.state.high_watermark
            dsc    = self.state.daily_start_capital
            mode   = self.state.mode.value
            open_p = len(self.state.open_positions)
            open_r = sum(self.state.open_positions.values())
            agg_r  = sum(v for k, v in self.state.open_positions.items()
                         if k.split(":")[0] in self._aggressive_agents)
            agg_a  = list(self._aggressive_agents)

        drawdown   = (hwm - cap) / hwm * 100 if hwm else 0
        daily_loss = (dsc - cap) / dsc * 100 if dsc else 0

        return {
            "mode":                 mode,
            "capital":              round(cap,        2),
            "high_watermark":       round(hwm,        2),
            "drawdown_pct":         round(drawdown,   2),
            "daily_loss_pct":       round(daily_loss, 2),
            "open_risk":            round(open_r,     2),
            "open_positions":       open_p,
            "aggressive_open_risk": round(agg_r,      2),
            "aggressive_agents":    agg_a,
        }

    # ── Internal ──────────────────────────────────────────────────────────
    def _evaluate_mode(self):
        """Must be called with self._lock held."""
        cap    = self.state.capital
        hwm    = self.state.high_watermark
        dsc    = self.state.daily_start_capital
        dd     = (hwm - cap) / hwm * 100 if hwm else 0
        dl     = (dsc - cap) / dsc * 100 if dsc else 0

        if dd >= self.config.drawdown_kill_pct or dl >= self.config.daily_loss_limit_pct:
            self.state.mode = RiskMode.STOPPED
            logger.critical(f"KILL SWITCH | dd={dd:.1f}% | daily_loss={dl:.1f}%")
        elif dd >= self.config.drawdown_warning_pct:
            if self.state.mode != RiskMode.REDUCED:
                logger.warning(f"REDUCED MODE | dd={dd:.1f}%")
            self.state.mode = RiskMode.REDUCED
        elif self.state.mode == RiskMode.REDUCED:
            recovery = (cap / (hwm * (1 - self.config.drawdown_warning_pct / 100)) - 1) * 100
            if recovery >= self.config.recovery_required_pct:
                self.state.mode = RiskMode.NORMAL
                logger.info("NORMAL MODE restored")

    @staticmethod
    def _pearson(x: list, y: list) -> float:
        n  = len(x)
        mx = sum(x) / n
        my = sum(y) / n
        num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
        den = (sum((x[i] - mx) ** 2 for i in range(n)) *
               sum((y[i] - my) ** 2 for i in range(n))) ** 0.5
        return num / den if den else 0.0
