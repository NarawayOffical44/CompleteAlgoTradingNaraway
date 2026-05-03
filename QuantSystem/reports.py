"""
Daily performance reporter — Groq LLM writes the report, sent via Telegram.

What it does:
  1. Reads all closed trades from the journal
  2. Calculates post-tax P&L (STT + brokerage + STCG/slab tax)
  3. Analyzes per-agent signal rejection stats (why bots aren't trading)
  4. Calls Groq to write a plain-English diagnosis + recommendations
  5. Sends via Telegram

Self-optimization loop:
  - LLM sees what each agent evaluated vs filtered vs executed
  - It diagnoses which filter is blocking most signals
  - It suggests parameter changes (logged, not auto-applied yet)
  - Over time, these suggestions inform manual or automated tuning

Call at market close:
  from reports import send_daily_report
  send_daily_report(journal, risk, agents)
"""

import requests
from datetime import datetime
from loguru import logger

from tax import TaxCalculator
from notify import notify


_TAX = TaxCalculator(income_slab_pct=30.0)   # adjust to your slab


def send_daily_report(journal, risk, agents: dict = None):
    """
    Generate and send daily Telegram report.
    agents: dict of agent_id → agent object (to read signal_stats).
    """
    closed_trades = [t for t in journal.trades.values() if t.status == "closed"]
    tax_summary   = _TAX.from_journal(closed_trades)
    risk_status   = risk.status()
    agent_stats   = _collect_agent_stats(agents, journal)

    report = _generate_report(tax_summary, risk_status, agent_stats, len(closed_trades))
    notify(report)
    logger.info("Daily report sent via Telegram")


# ── Report generation ─────────────────────────────────────────────────────────

def _generate_report(tax: dict, risk: dict, agent_stats: list, total_trades: int) -> str:
    from config import config
    groq_key = config.groq_api_key or ""

    # Build agent stats summary for LLM
    agent_lines = []
    for a in agent_stats:
        agent_lines.append(
            f"  {a['agent_id']:20s} | trades={a['trades']:2d} | "
            f"pnl=Rs{a['pnl']:+8,.0f} | win={a['win_rate']:4.0f}% | "
            f"evaluated={a['evaluated']:3d} | signals={a['signals']:2d} | "
            f"top_filter={a['top_filter']}"
        )

    prompt = f"""You are a quant trading system analyst for an Indian stock market bot.
Analyze today's performance and write a concise Telegram report (max 12 lines, HTML format).

=== TODAY'S RESULTS ===
Total trades: {total_trades}
Gross P&L: Rs{tax['gross_pnl']:+,.0f}
Transaction costs (STT+brokerage+exchange): Rs{tax['transaction_costs']:,.0f}
Tax on profit (30% slab / STCG 20%): Rs{tax['tax_on_profit']:,.0f}
Net P&L after all deductions: Rs{tax['net_pnl']:+,.0f}
Capital: Rs{risk['capital']:,.0f}  Drawdown: {risk['drawdown_pct']:.1f}%  Mode: {risk['mode']}

=== PER-AGENT BREAKDOWN ===
{chr(10).join(agent_lines) if agent_lines else '  No agent data'}

=== DIAGNOSIS TASK ===
1. Give a one-line verdict on the day (good/bad/neutral/no-trades)
2. For any agent with 0 trades: explain the most likely reason based on top_filter
3. For any agent with trades: comment on win rate and P&L quality
4. Give 1-2 specific, actionable suggestions to improve performance tomorrow
5. Note net P&L after tax — this is real take-home profit

Format: HTML with <b>bold</b> for key numbers. Be direct, no fluff.
Start with: <b>Daily Report — {datetime.now().strftime('%d %b %Y')}</b>"""

    if groq_key:
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                    "temperature": 0.2,
                },
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Report | Groq failed: {e} — using template")

    # Template fallback
    verdict = ("Profitable" if tax["net_pnl"] > 0
               else "No trades" if total_trades == 0
               else "Loss day")
    lines = [
        f"<b>Daily Report — {datetime.now().strftime('%d %b %Y')}</b>",
        f"{verdict}",
        "",
        f"Trades: {total_trades}",
        f"Gross P&L: <b>Rs{tax['gross_pnl']:+,.0f}</b>",
        f"Tax + charges: Rs{tax['total_deductions']:,.0f}",
        f"Net P&L: <b>Rs{tax['net_pnl']:+,.0f}</b>",
        f"Capital: Rs{risk['capital']:,.0f}  DD: {risk['drawdown_pct']:.1f}%",
    ]
    return "\n".join(lines)


# ── Agent stats collector ─────────────────────────────────────────────────────

def _collect_agent_stats(agents: dict, journal) -> list:
    """Pull per-agent trade stats + signal_stats if available."""
    if not agents:
        return []

    stats = []
    for agent_id, agent in agents.items():
        closed = [t for t in journal.trades.values()
                  if t.agent_id == agent_id and t.status == "closed"]

        pnl      = sum(t.pnl for t in closed)
        winners  = [t for t in closed if t.pnl > 0]
        win_rate = len(winners) / len(closed) * 100 if closed else 0.0

        # Read signal_stats if agent tracks them
        sig  = getattr(agent, "signal_stats", {})
        top_filter = _top_filter(sig)

        stats.append({
            "agent_id":   agent_id,
            "trades":     len(closed),
            "pnl":        pnl,
            "win_rate":   win_rate,
            "evaluated":  sig.get("evaluated", 0),
            "signals":    sig.get("signals_generated", 0),
            "top_filter": top_filter,
        })

    return stats


def _top_filter(sig: dict) -> str:
    """Return the filter that rejected the most signals."""
    filters = {
        "volume":       sig.get("filtered_volume", 0),
        "fundamentals": sig.get("filtered_fundamentals", 0),
        "regime":       sig.get("filtered_regime", 0),
        "sentiment":    sig.get("filtered_sentiment", 0),
        "lgbm":         sig.get("filtered_lgbm", 0),
        "zscore":       sig.get("filtered_zscore", 0),
        "risk_engine":  sig.get("filtered_risk", 0),
    }
    top = max(filters, key=filters.get)
    return f"{top}({filters[top]})" if filters[top] > 0 else "none"
