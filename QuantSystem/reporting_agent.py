"""
ReportingAgent — Separate LLM monitoring + Telegram reporting bot.

Runs independently from the main trading system.
Reads TradeJournal CSV and RiskEngine state, uses Claude to write
a concise daily report, and sends it to Telegram.

Usage:
    python reporting_agent.py            # One-time report + send
    python reporting_agent.py --schedule # Run on schedule (8 PM daily + 2h alerts)
    python reporting_agent.py --alert    # Send real-time alert only (call from main if needed)

Setup:
    Add to .env:
        TELEGRAM_BOT_TOKEN=your_bot_token   (from BotFather)
        TELEGRAM_CHAT_ID=your_chat_id       (your personal chat or group)
"""

import sys
import json
import csv
import time
import schedule
import requests as req
from pathlib import Path
from datetime import datetime
from loguru import logger

# ── Try loading config + anthropic ────────────────────────────────────────────
try:
    from config import config
    STARTING_CAPITAL = config.starting_capital
    ANTHROPIC_KEY    = config.anthropic_api_key if hasattr(config, "anthropic_api_key") else None
    LOGS_DIR         = Path("logs")
except Exception:
    from dotenv import load_dotenv
    import os
    load_dotenv()
    STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", 10000))
    ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
    LOGS_DIR         = Path("logs")

import os
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logger.add(
    "logs/reporter_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="INFO",
    format="{time:HH:mm:ss} | {level} | {message}",
)


# ─────────────────────────────────────────────────────────────────────────────
# Data Collection
# ─────────────────────────────────────────────────────────────────────────────

def _read_trades() -> list[dict]:
    """Load all trades from CSV journal."""
    path = LOGS_DIR / "trades.csv"
    if not path.exists():
        return []
    trades = []
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            try:
                row["pnl"]         = float(row.get("pnl", 0))
                row["pnl_pct"]     = float(row.get("pnl_pct", 0))
                row["entry_price"] = float(row.get("entry_price", 0))
                row["exit_price"]  = float(row.get("exit_price", 0))
                row["quantity"]    = float(row.get("quantity", 0))
                trades.append(row)
            except Exception:
                pass
    return trades


def _build_performance_summary(trades: list[dict]) -> dict:
    """Aggregate performance per agent + overall."""
    today = datetime.now().strftime("%Y-%m-%d")
    closed = [t for t in trades if t.get("status") == "closed"]
    today_trades = [t for t in closed if t.get("entry_time", "").startswith(today)]

    def _agg(subset):
        if not subset:
            return {"trades": 0, "total_pnl": 0, "win_rate": 0, "best": 0, "worst": 0}
        pnls    = [t["pnl"] for t in subset]
        winners = [p for p in pnls if p > 0]
        return {
            "trades":    len(subset),
            "total_pnl": round(sum(pnls), 2),
            "win_rate":  round(len(winners) / len(pnls) * 100, 1),
            "best":      round(max(pnls), 2),
            "worst":     round(min(pnls), 2),
        }

    agents = list({t["agent_id"] for t in closed})
    by_agent = {a: _agg([t for t in closed if t["agent_id"] == a]) for a in agents}

    return {
        "overall":      _agg(closed),
        "today":        _agg(today_trades),
        "by_agent":     by_agent,
        "total_trades": len(closed),
        "open_trades":  len([t for t in trades if t.get("status") == "open"]),
    }


def _read_risk_state() -> dict:
    """Try to load risk state from logs (JSON snapshot if available)."""
    snap = LOGS_DIR / "risk_state.json"
    if snap.exists():
        try:
            return json.loads(snap.read_text())
        except Exception:
            pass
    return {"mode": "unknown", "capital": STARTING_CAPITAL,
            "drawdown_pct": 0, "daily_loss_pct": 0}


def _read_latest_log_lines(n: int = 20) -> str:
    """Return last N lines from today's trading log."""
    today = datetime.now().strftime("%Y-%m-%d")
    logs  = sorted(LOGS_DIR.glob(f"quant_{today}*.log"), reverse=True)
    if not logs:
        logs = sorted(LOGS_DIR.glob("quant_*.log"), reverse=True)
    if not logs:
        return "No log file found."
    lines = logs[0].read_text(errors="replace").strip().splitlines()
    return "\n".join(lines[-n:])


# ─────────────────────────────────────────────────────────────────────────────
# Claude Report Generator
# ─────────────────────────────────────────────────────────────────────────────

def _generate_report_with_claude(perf: dict, risk: dict, log_tail: str) -> str:
    """Use Claude to write a clean daily report from raw data."""
    if not ANTHROPIC_KEY or ANTHROPIC_KEY.startswith("sk-ant-your"):
        return _fallback_report(perf, risk)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        prompt = f"""You are a trading bot performance monitor for an NSE India algo trading system (QuantSystem).

Analyze this performance data and write a concise Telegram-friendly daily report.
Use plain text — no markdown headers (Telegram has limited formatting).
Keep it under 400 words. Be direct, like an analyst briefing.

PERFORMANCE DATA:
{json.dumps(perf, indent=2)}

RISK ENGINE STATE:
{json.dumps(risk, indent=2)}

RECENT LOG TAIL (last 20 lines):
{log_tail}

FORMAT:
📊 QuantSystem Daily Report — {datetime.now().strftime('%d %b %Y')}

OVERALL: [Total P&L, trades today, win rate]

AGENTS:
[One line per agent: name | trades | P&L | win rate | status]

RISK: [Mode, drawdown, capital, any alerts]

ISSUES: [Any errors or warnings from logs worth flagging, or "None"]

TOMORROW: [1-2 sentence recommendation based on today's performance]
"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    except Exception as e:
        logger.warning(f"Claude report generation failed: {e}")
        return _fallback_report(perf, risk)


def _fallback_report(perf: dict, risk: dict) -> str:
    """Plain-text report when Claude is unavailable."""
    o = perf["overall"]
    t = perf["today"]
    lines = [
        f"📊 QuantSystem Report — {datetime.now().strftime('%d %b %Y %H:%M')}",
        "",
        f"OVERALL: {o['trades']} trades | P&L ₹{o['total_pnl']:+,.0f} | Win {o['win_rate']}%",
        f"TODAY:   {t['trades']} trades | P&L ₹{t['total_pnl']:+,.0f}",
        f"OPEN:    {perf['open_trades']} positions",
        "",
        "AGENTS:",
    ]
    for agent, stats in perf["by_agent"].items():
        lines.append(f"  {agent}: {stats['trades']}T | ₹{stats['total_pnl']:+,.0f} | {stats['win_rate']}% WR")
    lines += [
        "",
        f"RISK: mode={risk.get('mode','?')} | capital=₹{risk.get('capital', STARTING_CAPITAL):,.0f}",
        f"      DD={risk.get('drawdown_pct', 0):.1f}% | daily_loss={risk.get('daily_loss_pct', 0):.1f}%",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram Sender
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """Send message via Telegram Bot API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env)")
        print("\n" + "─" * 60)
        print(message)
        print("─" * 60 + "\n")
        return False

    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    try:
        r = req.post(url, data=data, timeout=10)
        if r.status_code == 200:
            logger.info("Telegram report sent successfully")
            return True
        else:
            logger.warning(f"Telegram send failed: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Alert Logic
# ─────────────────────────────────────────────────────────────────────────────

def send_alert(message: str):
    """Send an urgent real-time alert (called externally or by scheduler)."""
    alert = f"🚨 ALERT — {datetime.now().strftime('%H:%M')} IST\n\n{message}"
    send_telegram(alert)
    logger.warning(f"Alert sent: {message}")


def _check_and_alert():
    """Check for problems and send alert if needed."""
    risk   = _read_risk_state()
    mode   = risk.get("mode", "normal")
    dd_pct = float(risk.get("drawdown_pct", 0))
    daily  = float(risk.get("daily_loss_pct", 0))

    alerts = []
    if mode == "stopped":
        alerts.append(f"Kill switch ACTIVE — trading STOPPED (DD {dd_pct:.1f}%)")
    elif mode == "reduced":
        alerts.append(f"Risk REDUCED mode — drawdown {dd_pct:.1f}%")
    if daily > 2.5:
        alerts.append(f"Daily loss warning: {daily:.1f}% (limit 3%)")

    if alerts:
        send_alert("\n".join(alerts))
    else:
        logger.info(f"Health check OK | mode={mode} | DD={dd_pct:.1f}% | daily={daily:.1f}%")


# ─────────────────────────────────────────────────────────────────────────────
# Main Report
# ─────────────────────────────────────────────────────────────────────────────

def run_daily_report():
    """Generate and send the full daily report."""
    logger.info("Generating daily report...")
    trades  = _read_trades()
    perf    = _build_performance_summary(trades)
    risk    = _read_risk_state()
    log_tail = _read_latest_log_lines(20)
    report  = _generate_report_with_claude(perf, risk, log_tail)
    send_telegram(report)
    logger.info("Daily report complete")


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if "--alert" in sys.argv:
        _check_and_alert()
        return

    if "--schedule" in sys.argv:
        logger.info("ReportingAgent scheduler started | daily report @ 20:00 | health check every 2h")
        schedule.every().day.at("20:00").do(run_daily_report)
        schedule.every(2).hours.do(_check_and_alert)
        # Send startup notification
        send_telegram(
            f"🤖 ReportingAgent started — {datetime.now().strftime('%d %b %Y %H:%M')} IST\n"
            f"Daily report: 8 PM | Alerts: every 2h"
        )
        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        # One-shot report
        run_daily_report()


if __name__ == "__main__":
    main()
