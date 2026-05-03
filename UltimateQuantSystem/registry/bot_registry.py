"""
BotRegistry — The school manager.

Manages all BotRunners:
  - Start all in parallel (each in its own thread)
  - Stop all / stop one / resume one
  - Suspend a bot (HeadAI decision) with optional duration + reason
  - Auto-resume bots after suspension period expires
  - Set HeadAI allocation multiplier per bot (persists across ticks)
  - Provide unified status for dashboard and HeadAI
  - Maintain decision audit log (what HeadAI decided + when + why)
  - No bot knows about any other bot
"""

from datetime import datetime, timedelta
from loguru import logger


class BotRegistry:

    def __init__(self):
        self._runners: dict[str, object] = {}   # agent_id → BotRunner
        self._decisions_log: list[dict]  = []   # audit trail of HeadAI decisions

    # ── Registration ──────────────────────────────────────────────────────
    def register(self, runner) -> None:
        self._runners[runner.agent_id] = runner
        logger.info(f"BotRegistry | registered {runner.agent_id} on {runner.market.market_id}")

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def start_all(self) -> None:
        logger.info(f"BotRegistry | launching {len(self._runners)} bots in parallel")
        for runner in self._runners.values():
            runner.start()

    def stop_all(self) -> None:
        for runner in self._runners.values():
            runner.stop()
        logger.info("BotRegistry | all bots signalled to stop")

    def stop_bot(self, agent_id: str) -> bool:
        """Hard stop — kills the thread. Use suspend_bot() for temporary pause."""
        if agent_id in self._runners:
            self._runners[agent_id].stop()
            return True
        return False

    def resume_bot(self, agent_id: str) -> bool:
        """Start (or restart) a stopped/suspended bot."""
        runner = self._runners.get(agent_id)
        if not runner:
            return False
        # Clear any suspension
        runner.suspended        = False
        runner.suspended_reason = ""
        runner.suspended_until  = None
        runner.head_ai_mult     = 1.0
        if not runner.is_alive():
            runner.start()
        logger.info(f"BotRegistry | {agent_id} manually resumed")
        return True

    # ── HeadAI suspension controls ────────────────────────────────────────
    def suspend_bot(self, agent_id: str, reason: str, hours: float = 24.0) -> bool:
        """
        Suspend a bot — it will skip all ticks until resumed.
        hours=0 means indefinite (manual resume required).
        """
        runner = self._runners.get(agent_id)
        if not runner:
            logger.warning(f"BotRegistry | suspend_bot: unknown agent {agent_id}")
            return False

        runner.suspended        = True
        runner.suspended_reason = reason[:80]
        runner.suspended_until  = (datetime.now() + timedelta(hours=hours)) if hours > 0 else None
        runner.head_ai_mult     = 0.0

        self._log_decision(agent_id, "SUSPEND", reason, suspend_hours=hours)
        logger.warning(
            f"BotRegistry | SUSPENDED {agent_id} | reason={reason} | "
            f"until={'indefinite' if hours == 0 else runner.suspended_until.strftime('%H:%M %d-%b')}"
        )
        return True

    def set_ai_mult(self, agent_id: str, mult: float, reason: str = "") -> bool:
        """
        Set HeadAI allocation multiplier for a bot.
        mult=1.0 → full market allocation (BOOST/MAINTAIN)
        mult=0.5 → half market allocation (REDUCE)
        mult=0.0 → effectively skip (same as suspend but thread keeps running)
        """
        runner = self._runners.get(agent_id)
        if not runner:
            return False

        mult = max(0.0, min(1.0, mult))
        runner.head_ai_mult = mult
        runner.head_ai_note = reason[:60] if reason else ""

        action = "BOOST" if mult >= 1.0 else ("REDUCE" if mult < 1.0 else "MAINTAIN")
        self._log_decision(agent_id, action, reason, new_mult=mult)
        logger.info(f"BotRegistry | {action} {agent_id} | mult={mult:.2f} | reason={reason}")
        return True

    def check_auto_resumes(self) -> list[str]:
        """
        Called by HeadAI each cycle. Resumes bots whose suspension period has expired.
        Returns list of agent_ids that were auto-resumed.
        """
        resumed = []
        now = datetime.now()
        for runner in self._runners.values():
            if runner.suspended and runner.suspended_until and now >= runner.suspended_until:
                runner.suspended        = False
                runner.suspended_reason = ""
                runner.suspended_until  = None
                runner.head_ai_mult     = 1.0
                self._log_decision(runner.agent_id, "AUTO_RESUME", "Suspension period expired")
                resumed.append(runner.agent_id)
                logger.info(f"BotRegistry | AUTO_RESUMED {runner.agent_id}")
        return resumed

    def is_suspended(self, agent_id: str) -> bool:
        runner = self._runners.get(agent_id)
        return runner.suspended if runner else False

    # ── Status ────────────────────────────────────────────────────────────
    def status_all(self) -> list[dict]:
        return [r.metrics() for r in self._runners.values()]

    def status_bot(self, agent_id: str) -> dict:
        runner = self._runners.get(agent_id)
        return runner.metrics() if runner else {}

    def alive_count(self) -> int:
        return sum(1 for r in self._runners.values() if r.is_alive())

    def suspended_count(self) -> int:
        return sum(1 for r in self._runners.values() if r.suspended)

    def bot_ids(self) -> list[str]:
        return list(self._runners.keys())

    # ── Decision audit log ────────────────────────────────────────────────
    def recent_decisions(self, n: int = 10) -> list[dict]:
        """Return last n HeadAI decisions (most recent first)."""
        return list(reversed(self._decisions_log[-n * 2:]))[:n]

    def _log_decision(self, agent_id: str, action: str, reason: str,
                      suspend_hours: float = None, new_mult: float = None):
        entry = {
            "time":     datetime.now().strftime("%H:%M %d-%b"),
            "agent_id": agent_id,
            "action":   action,
            "reason":   reason,
        }
        if suspend_hours is not None:
            entry["suspend_hours"] = suspend_hours
        if new_mult is not None:
            entry["new_mult"] = new_mult
        self._decisions_log.append(entry)
        # Keep last 200 decisions
        if len(self._decisions_log) > 200:
            self._decisions_log = self._decisions_log[-200:]
