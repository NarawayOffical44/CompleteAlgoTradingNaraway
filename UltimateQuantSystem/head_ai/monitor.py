"""
HeadAI — Claude-powered head agent that monitors all bots.

Runs periodically (every ANALYZE_INTERVAL_S seconds) and:
  1. Reads all bot metrics from BotRegistry
  2. Ranks bots by performance (PnL, Sharpe, win rate)
  3. Calls Claude Haiku to generate insights + recommendations
  4. Sends Telegram alerts for critical events (errors, big losses)
  5. Stores last report for dashboard to display

Adding Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
Adding Claude:   set ANTHROPIC_API_KEY in .env
"""

import json
import time
import threading
import requests
from datetime import datetime
from loguru import logger


ANALYZE_INTERVAL_S = 15 * 60   # analyze every 15 min
ALERT_THRESHOLDS = {
    "error_count_critical": 3,          # 3+ consecutive errors → alert
    "pnl_loss_alert":       -5000,      # ₹5000 loss on any bot → alert
    "perf_mult_warn":       0.0,        # performance_mult = 0 → alert
}


class HeadAI:

    def __init__(self, registry, anthropic_api_key: str = "",
                 telegram_token: str = "", telegram_chat_id: str = ""):
        self.registry        = registry
        self.api_key         = anthropic_api_key
        self.tg_token        = telegram_token
        self.tg_chat_id      = telegram_chat_id

        self._last_insights  = ["HeadAI not yet run."]
        self._last_ranking   = []
        self._last_alerts    = []
        self._last_run       = None

        self._thread         = None
        self._stop_event     = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def start(self):
        """Start HeadAI as a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="head-ai",
            daemon=True,
        )
        self._thread.start()
        logger.info("HeadAI | started")

    def stop(self):
        self._stop_event.set()

    # ── Main loop ─────────────────────────────────────────────────────────
    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self.analyze()
            except Exception as e:
                logger.error(f"HeadAI | analysis error: {e}")

            elapsed = 0
            while elapsed < ANALYZE_INTERVAL_S and not self._stop_event.is_set():
                time.sleep(5)
                elapsed += 5

    # ── Core analysis ─────────────────────────────────────────────────────
    def analyze(self) -> dict:
        """
        Full analysis cycle:
          metrics → rank → alerts → Claude insights → Telegram
        Returns report dict.
        """
        metrics  = self.registry.status_all()
        ranking  = self._rank_bots(metrics)
        alerts   = self._check_alerts(metrics)

        # Generate insights (Claude or rule-based fallback)
        if self.api_key:
            try:
                insights = self._call_claude(metrics, ranking, alerts)
            except Exception as e:
                logger.warning(f"HeadAI | Claude failed: {e} — using rule-based insights")
                insights = self._rule_based_insights(metrics, ranking, alerts)
        else:
            insights = self._rule_based_insights(metrics, ranking, alerts)

        # Send Telegram alerts
        if alerts and self.tg_token and self.tg_chat_id:
            self._send_telegram(alerts, insights)

        self._last_ranking  = ranking
        self._last_alerts   = alerts
        self._last_insights = insights
        self._last_run      = datetime.now().strftime("%H:%M:%S")

        logger.info(f"HeadAI | analysis done | {len(alerts)} alerts | {len(insights)} insights")

        return {
            "ranking":   ranking,
            "alerts":    alerts,
            "insights":  insights,
            "timestamp": self._last_run,
        }

    # ── Ranking ───────────────────────────────────────────────────────────
    def _rank_bots(self, metrics: list[dict]) -> list[dict]:
        """Rank by total_pnl → win_rate → performance_mult."""
        ranked = sorted(
            metrics,
            key=lambda m: (
                m.get("total_pnl",       0),
                m.get("win_rate",        0),
                m.get("performance_mult", 0),
            ),
            reverse=True,
        )
        for i, bot in enumerate(ranked):
            bot["rank"] = i + 1
        return ranked

    # ── Alert checks ──────────────────────────────────────────────────────
    def _check_alerts(self, metrics: list[dict]) -> list[str]:
        alerts = []
        for m in metrics:
            aid = m.get("agent_id", "?")

            if m.get("status") == "error":
                alerts.append(f"🔴 {aid} ERROR: {m.get('last_error', 'unknown')}")

            if m.get("error_count", 0) >= ALERT_THRESHOLDS["error_count_critical"]:
                alerts.append(f"⚠️ {aid} has {m['error_count']} consecutive errors")

            total_pnl = m.get("total_pnl", 0)
            if total_pnl < ALERT_THRESHOLDS["pnl_loss_alert"]:
                alerts.append(f"🔴 {aid} total loss = ₹{total_pnl:,.0f}")

            if m.get("performance_mult") == ALERT_THRESHOLDS["perf_mult_warn"]:
                alerts.append(f"⚠️ {aid} Sharpe < 0 — consider pausing")

        return alerts

    # ── Claude insights ───────────────────────────────────────────────────
    def _call_claude(self, metrics: list, ranking: list, alerts: list) -> list[str]:
        """Call Claude Haiku to generate insights from bot performance data."""
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)

        # Build a concise summary for Claude
        summary_lines = []
        for m in ranking:
            summary_lines.append(
                f"  #{m.get('rank','-')} {m['agent_id']:20s} | market={m.get('market','?'):6s} | "
                f"status={m.get('status','?'):12s} | trades={m.get('trades',0):3d} | "
                f"pnl=₹{m.get('total_pnl',0):8,.0f} | win={m.get('win_rate',0):4.0f}% | "
                f"errors={m.get('error_count',0)}"
            )

        prompt = f"""You are a quantitative trading system monitor for an Indian stock market bot school.
Analyze these bot performance metrics and provide 4-6 concise, actionable insights.

=== BOT RANKINGS ===
{chr(10).join(summary_lines)}

=== ACTIVE ALERTS ===
{chr(10).join(alerts) if alerts else "None"}

Rules:
- Be specific (name the bot, give numbers)
- Focus on: what's working, what needs attention, what to investigate
- If a bot has 0 trades, note it might be filtering correctly or might be broken
- If errors, suggest likely causes
- Keep each insight under 100 characters

Respond with ONLY a JSON array of strings. No other text.
Example: ["Insight 1", "Insight 2"]"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        return json.loads(text)

    # ── Rule-based fallback insights ──────────────────────────────────────
    def _rule_based_insights(self, metrics: list, ranking: list, alerts: list) -> list[str]:
        insights = []

        if ranking:
            best  = ranking[0]
            worst = ranking[-1]
            insights.append(
                f"Top bot: {best['agent_id']} | ₹{best.get('total_pnl',0):,.0f} PnL"
            )
            if worst.get("total_pnl", 0) < 0:
                insights.append(
                    f"Weakest: {worst['agent_id']} | ₹{worst.get('total_pnl',0):,.0f} — review"
                )

        running    = sum(1 for m in metrics if m.get("status") == "running")
        errored    = sum(1 for m in metrics if m.get("status") == "error")
        mkt_closed = sum(1 for m in metrics if m.get("status") == "market_closed")
        insights.append(
            f"{running} running | {errored} error | {mkt_closed} market-closed"
        )

        zero_trades = [m["agent_id"] for m in metrics if m.get("trades", 0) == 0]
        if zero_trades:
            insights.append(f"No trades yet: {', '.join(zero_trades)}")

        if not self.api_key:
            insights.append("Add ANTHROPIC_API_KEY for AI-powered insights")

        return insights

    # ── Telegram ──────────────────────────────────────────────────────────
    def _send_telegram(self, alerts: list[str], insights: list[str]):
        """Send alert + top insight to Telegram."""
        if not self.tg_token or not self.tg_chat_id:
            return

        lines = ["*🤖 UltimateQuantSystem — HeadAI Alert*", ""]
        for alert in alerts[:5]:        # cap at 5 alerts
            lines.append(f"• {alert}")

        if insights:
            lines.append("")
            lines.append(f"💡 {insights[0]}")

        message = "\n".join(lines)
        url     = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"

        try:
            resp = requests.post(url, json={
                "chat_id":    self.tg_chat_id,
                "text":       message,
                "parse_mode": "Markdown",
            }, timeout=10)
            if resp.ok:
                logger.info("HeadAI | Telegram alert sent")
            else:
                logger.warning(f"HeadAI | Telegram failed: {resp.text}")
        except Exception as e:
            logger.warning(f"HeadAI | Telegram error: {e}")

    # ── Last report (for dashboard) ───────────────────────────────────────
    def last_report(self) -> dict:
        return {
            "ranking":  self._last_ranking,
            "alerts":   self._last_alerts,
            "insights": self._last_insights,
            "last_run": self._last_run,
        }
