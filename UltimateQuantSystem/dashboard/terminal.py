"""
TerminalDashboard — Live terminal view of all bots + HeadAI insights + decisions.

Shows every refresh_s seconds:
  - Portfolio summary (capital, mode, drawdown, daily P&L)
  - Bot table: name, market, status, AI-mult, trades, PnL, win%, runs, errors
  - HeadAI decisions taken (SUSPEND / REDUCE / BOOST with reasons)
  - HeadAI insights
  - Active alerts
  - Suspended bots panel
  - Recent HeadAI decision audit log
"""

import os
import time
from datetime import datetime
from loguru import logger


_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_MAGENTA = "\033[95m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

W = 110     # terminal width


def _c(color: str, text: str) -> str:
    return f"{color}{text}{_RESET}"


def _status_fmt(m: dict) -> str:
    """Format bot status — suspension takes priority over normal status."""
    if m.get("suspended"):
        reason = m.get("suspended_reason", "")[:12]
        until  = m.get("suspended_until") or "indef"
        return _c(_MAGENTA, f"{'⏸ SUSP:' + reason:<18}")

    status = m.get("status", "?")
    if status == "running":
        ai = m.get("head_ai_mult", 1.0)
        tag = f"(x{ai:.1f})" if ai < 1.0 else ""
        return _c(_GREEN, f"{'● run' + tag:<18}")
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
    s = f"Rs{pnl:>+9,.0f}"
    return _c(_GREEN, s) if pnl >= 0 else _c(_RED, s)


def _action_icon(action: str) -> str:
    return {"SUSPEND": _c(_MAGENTA, "⏸ SUSPEND"), "REDUCE": _c(_YELLOW, "↓ REDUCE  "),
            "BOOST":   _c(_GREEN,   "↑ BOOST   "), "AUTO_RESUME": _c(_CYAN, "▶ RESUME  ")}.get(action, action)


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

        # ── Header ────────────────────────────────────────────────────────
        print(sep)
        print(f"  {_BOLD}UltimateQuantSystem  ·  Self-Governing Bot School{_RESET}"
              f"  │  {_BOLD}{now}{_RESET}")
        print(sep)

        # ── Portfolio row ──────────────────────────────────────────────────
        rs   = self.risk.status()
        mode = rs["mode"]
        mode_str    = _c(_RED, mode.upper()) if mode != "normal" else _c(_GREEN, "NORMAL")
        dd_str      = _c(_RED, f"{rs['drawdown_pct']:.1f}%") if rs['drawdown_pct'] > 5 else f"{rs['drawdown_pct']:.1f}%"
        daily_l_str = _c(_RED, f"{rs['daily_loss_pct']:.1f}%") if rs['daily_loss_pct'] > 2 else _c(_GREEN, f"{rs['daily_loss_pct']:.1f}%")

        print(f"  Capital: {_BOLD}Rs{rs['capital']:>12,.0f}{_RESET}  │  "
              f"HWM: Rs{rs['high_watermark']:>12,.0f}  │  "
              f"Mode: {mode_str}  │  DD: {dd_str}  │  Daily P&L: {daily_l_str}  │  "
              f"Open risk: Rs{rs['open_risk']:>8,.0f}  │  Pos: {rs['open_positions']}")
        print(sep)

        # ── Bot table ──────────────────────────────────────────────────────
        report  = self.head_ai.last_report()
        ranking = report.get("ranking") or self.registry.status_all()

        hdr = (f"  {'#':>2}  {'Bot':<22} {'Mkt':>6}  {'Status':<18}  {'AImult':>6}  "
               f"{'Trades':>6}  {'PnL':>12}  {'Win%':>5}  {'Runs':>5}  {'Err':>3}  {'Last Run':>8}")
        print(hdr)
        print(f"  {'─'*2}  {'─'*22} {'─'*6}  {'─'*18}  {'─'*6}  "
              f"{'─'*6}  {'─'*12}  {'─'*5}  {'─'*5}  {'─'*3}  {'─'*8}")

        for m in ranking:
            rank     = m.get("rank", "-")
            aid      = m.get("agent_id", "?")
            mkt      = m.get("market", "?")
            trades   = m.get("trades", 0)
            pnl      = m.get("total_pnl", 0)
            win      = m.get("win_rate", 0)
            runs     = m.get("runs_total", 0)
            errs     = m.get("error_count", 0)
            last_run = m.get("last_run") or "—"
            ai_mult  = m.get("head_ai_mult", 1.0)

            ai_str   = _c(_YELLOW, f"{ai_mult:.2f}") if ai_mult < 1.0 else f"{ai_mult:.2f}"
            err_str  = _c(_RED, f"{errs:>3}") if errs > 0 else f"{errs:>3}"

            print(f"  {rank:>2}  {aid:<22} {mkt:>6}  {_status_fmt(m)}  "
                  f"{ai_str:>14}  {trades:>6}  {_pnl_fmt(pnl):>21}  "
                  f"{win:>4.0f}%  {runs:>5}  {err_str}  {last_run:>8}")

        # Suspended bots footnote
        susp_count = self.registry.suspended_count()
        print(sep)
        active  = self.registry.alive_count()
        total   = len(self.registry.bot_ids())
        print(f"  {active}/{total} bots alive  │  {susp_count} suspended  │  "
              f"HeadAI last: {report.get('last_run') or 'not run yet'}  │  "
              f"Refreshing every {self.refresh_s}s  │  Ctrl+C to stop")
        print(sep)

        # ── HeadAI decisions ──────────────────────────────────────────────
        decisions = report.get("decisions", [])
        if decisions:
            print(f"  {_BOLD}{_CYAN}HeadAI Decisions  (this cycle){_RESET}")
            for d in decisions:
                print(f"  {_action_icon(d['action'])} {d['agent_id']:<22} {d.get('reason','')[:55]}")
            print(sep)

        # ── Alerts ────────────────────────────────────────────────────────
        alerts = report.get("alerts", [])
        if alerts:
            print(f"  {_BOLD}{_RED}ALERTS{_RESET}")
            for a in alerts:
                print(f"  ⚠  {a}")
            print(sep)

        # ── HeadAI insights ───────────────────────────────────────────────
        pnote = report.get("portfolio_note", "")
        print(f"  {_BOLD}{_CYAN}HeadAI Insights{_RESET}")
        if pnote:
            print(f"  {_BOLD}Portfolio:{_RESET} {pnote}")
        insights = report.get("insights", ["HeadAI hasn't run yet."])
        for ins in insights:
            if not ins.startswith("Portfolio:"):
                print(f"  {_CYAN}•{_RESET} {ins}")

        # ── Recent decisions audit log (last 5) ───────────────────────────
        recent = self.registry.recent_decisions(5)
        if recent:
            print(sep)
            print(f"  {_BOLD}{_DIM}HeadAI Decision Log (last 5){_RESET}")
            for d in recent:
                icon  = {"SUSPEND": "⏸", "REDUCE": "↓", "BOOST": "↑",
                         "AUTO_RESUME": "▶", "MAINTAIN": "="}.get(d.get("action", ""), "•")
                print(
                    f"  {_DIM}{d.get('time',''):>13}  {icon} {d.get('action',''):12}  "
                    f"{d.get('agent_id',''):22}  {d.get('reason','')[:45]}{_RESET}"
                )

        print(sep)
