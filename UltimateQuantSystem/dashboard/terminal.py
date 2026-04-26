"""
TerminalDashboard — Live terminal view of all bots + HeadAI insights.

Clears and redraws every refresh_s seconds.
Shows:
  - Portfolio summary (capital, mode, drawdown)
  - Bot table: name, market, status, trades, PnL, win rate, errors, last run
  - HeadAI ranking + insights
  - Active alerts
"""

import os
import time
from datetime import datetime
from loguru import logger


_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

W = 100     # terminal width


def _c(color: str, text: str) -> str:
    return f"{color}{text}{_RESET}"


def _status_fmt(status: str) -> str:
    if status == "running":
        return _c(_GREEN, f"{'● running':<18}")
    if status == "stopped":
        return _c(_DIM, f"{'○ stopped':<18}")
    if "error" in status:
        return _c(_RED, f"{'✖ error':<18}")
    if "market_closed" in status:
        return _c(_YELLOW, f"{'⏸ mkt closed':<18}")
    if "skip" in status:
        return _c(_YELLOW, f"{'→ ' + status:<18}")
    if "news_block" in status:
        return _c(_YELLOW, f"{'⛔ news block':<18}")
    return _c(_DIM, f"{status:<18}")


def _pnl_fmt(pnl: float) -> str:
    s = f"₹{pnl:>+9,.0f}"
    return _c(_GREEN, s) if pnl >= 0 else _c(_RED, s)


class TerminalDashboard:

    def __init__(self, registry, head_ai, risk_engine, refresh_s: int = 30):
        self.registry   = registry
        self.head_ai    = head_ai
        self.risk       = risk_engine
        self.refresh_s  = refresh_s

    def run(self):
        """Blocking loop — renders dashboard, sleeps, repeats. Ctrl+C to stop."""
        try:
            while True:
                self._render()
                time.sleep(self.refresh_s)
        except KeyboardInterrupt:
            print()
            logger.info("Dashboard stopped by user.")

    def _render(self):
        os.system("cls" if os.name == "nt" else "clear")
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        sep = "─" * W

        # ── Header ───────────────────────────────────────────────────────
        print(sep)
        print(f"  {_BOLD}UltimateQuantSystem  ·  Bot School{_RESET}"
              f"  │  {_BOLD}{now}{_RESET}")
        print(sep)

        # ── Portfolio row ─────────────────────────────────────────────────
        rs   = self.risk.status()
        mode = rs["mode"]
        mode_str = _c(_RED, mode.upper()) if mode != "normal" else _c(_GREEN, "NORMAL")
        dd_str   = _c(_RED, f"{rs['drawdown_pct']:.1f}%") if rs['drawdown_pct'] > 5 else f"{rs['drawdown_pct']:.1f}%"

        print(f"  Capital: {_BOLD}₹{rs['capital']:>12,.0f}{_RESET}  │  "
              f"HWM: ₹{rs['high_watermark']:>12,.0f}  │  "
              f"Mode: {mode_str}  │  DD: {dd_str}  │  "
              f"Open risk: ₹{rs['open_risk']:>8,.0f}  │  "
              f"Positions: {rs['open_positions']}")
        print(sep)

        # ── Bot table ─────────────────────────────────────────────────────
        hdr = (f"  {'#':>2}  {'Bot':<22} {'Mkt':>5}  {'Status':<18}  "
               f"{'Trades':>6}  {'PnL':>12}  {'Win%':>5}  {'Runs':>5}  "
               f"{'Errors':>6}  {'Last Run':>8}")
        print(hdr)
        print(f"  {'─'*2}  {'─'*22} {'─'*5}  {'─'*18}  "
              f"{'─'*6}  {'─'*12}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*8}")

        report  = self.head_ai.last_report()
        ranking = report.get("ranking") or self.registry.status_all()

        for m in ranking:
            rank     = m.get("rank", "-")
            aid      = m.get("agent_id", "?")
            mkt      = m.get("market", "?")
            status   = m.get("status", "?")
            trades   = m.get("trades", 0)
            pnl      = m.get("total_pnl", 0)
            win      = m.get("win_rate", 0)
            runs     = m.get("runs_total", 0)
            errs     = m.get("error_count", 0)
            last_run = m.get("last_run") or "—"
            err_str  = _c(_RED, f"{errs:>6}") if errs > 0 else f"{errs:>6}"

            print(f"  {rank:>2}  {aid:<22} {mkt:>5}  {_status_fmt(status)}  "
                  f"{trades:>6}  {_pnl_fmt(pnl):>21}  {win:>4.0f}%  {runs:>5}  "
                  f"{err_str}  {last_run:>8}")

        print(sep)

        # ── Alerts ───────────────────────────────────────────────────────
        alerts = report.get("alerts", [])
        if alerts:
            print(f"  {_BOLD}{_RED}ALERTS{_RESET}")
            for a in alerts:
                print(f"  {a}")
            print(sep)

        # ── HeadAI insights ───────────────────────────────────────────────
        last_run_ai = report.get("last_run") or "not run yet"
        print(f"  {_BOLD}{_CYAN}Head AI Insights{_RESET}  "
              f"{_DIM}(last analysis: {last_run_ai}){_RESET}")
        insights = report.get("insights", ["HeadAI hasn't run yet."])
        for ins in insights:
            print(f"  {_CYAN}•{_RESET} {ins}")

        print(sep)
        print(f"  Refreshing every {self.refresh_s}s  │  Ctrl+C to stop  │  "
              f"{self.registry.alive_count()}/{len(self.registry.bot_ids())} bots alive")
        print(sep)
