"""
BotRegistry — The school manager.

Manages all BotRunners:
  - Start all in parallel (each in its own thread)
  - Stop all / stop one / resume one
  - Provide unified status for dashboard and HeadAI
  - No bot knows about any other bot
"""

from loguru import logger


class BotRegistry:

    def __init__(self):
        self._runners: dict[str, object] = {}   # agent_id → BotRunner

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
        if agent_id in self._runners:
            self._runners[agent_id].stop()
            return True
        return False

    def resume_bot(self, agent_id: str) -> bool:
        if agent_id in self._runners:
            self._runners[agent_id].start()
            return True
        return False

    # ── Status ────────────────────────────────────────────────────────────
    def status_all(self) -> list[dict]:
        return [r.metrics() for r in self._runners.values()]

    def status_bot(self, agent_id: str) -> dict:
        runner = self._runners.get(agent_id)
        return runner.metrics() if runner else {}

    def alive_count(self) -> int:
        return sum(1 for r in self._runners.values() if r.is_alive())

    def bot_ids(self) -> list[str]:
        return list(self._runners.keys())
