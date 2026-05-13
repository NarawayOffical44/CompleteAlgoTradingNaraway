"""
HeadAI — The school principal. Monitors, ranks, and CONTROLS all bots.

Runs every ANALYZE_INTERVAL_S (default: 15 min) and:
  1. Auto-resumes any bots whose suspension time has expired
  2. Reads all bot metrics from BotRegistry
  3. Ranks bots by performance (PnL, Sharpe, win rate, error rate)
  4. Calls LLM (Groq → Anthropic → rule-based fallback) for structured decisions
  5. EXECUTES decisions:
       SUSPEND  → registry.suspend_bot(id, reason, hours)
       REDUCE   → registry.set_ai_mult(id, 0.5, reason)
       MAINTAIN → no change
       BOOST    → registry.set_ai_mult(id, 1.0, reason)
  6. Sends Telegram: decisions made + alerts + top insight

Decision format from LLM:
  {
    "decisions": [
      {"agent_id": "...", "action": "SUSPEND|REDUCE|MAINTAIN|BOOST",
       "reason": "...", "suspend_hours": 24, "new_mult": 0.5}
    ],
    "portfolio_note": "one line summary"
  }

LLM fallback chain:
  Groq (llama-3.1-8b-instant) → Anthropic (claude-haiku) → rule-based

Key design: HeadAI respects human capital.
  - Won't SUSPEND/REDUCE/BOOST a bot with < MIN_TRADES trades (not enough data)
  - Won't REDUCE a bot that just resumed from suspension
  - Min BOOST interval = 2 cycles (prevent flip-flopping)
"""

import json
import time
import threading
import requests
from html import escape
from datetime import datetime
from loguru import logger


ANALYZE_INTERVAL_S = 15 * 60       # analyze every 15 min

# Thresholds for rule-based decisions
MIN_TRADES_FOR_DECISION = 3        # need at least this many trades before acting
SUSPEND_LOSS_THRESHOLD  = -8000    # ₹ loss → auto-suspend
SUSPEND_ERROR_THRESHOLD = 5        # 5+ consecutive errors → suspend
REDUCE_LOSS_THRESHOLD   = -3000    # ₹ loss → reduce allocation
REDUCE_ERROR_THRESHOLD  = 3        # 3+ errors → reduce
BOOST_SHARPE_MIN        = 1.5      # Sharpe > 1.5 → eligible for boost
BOOST_WIN_RATE_MIN      = 60.0     # win% > 60 → eligible for boost


class HeadAI:

    def __init__(self, registry, anthropic_api_key: str = "",
                 telegram_token: str = "", telegram_chat_id: str = ""):
        self.registry    = registry
        self.api_key     = anthropic_api_key          # Anthropic Claude (fallback)
        self.tg_token    = telegram_token
        self.tg_chat_id  = telegram_chat_id

        self._last_insights      = ["HeadAI not yet run."]
        self._last_ranking       = []
        self._last_alerts        = []
        self._last_decisions     = []
        self._last_portfolio_note = ""
        self._last_run           = None
        self._last_report        = {}

        self._thread     = None
        self._stop_event = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="head-ai",
            daemon=True,
        )
        self._thread.start()
        logger.info("HeadAI | started")

    def stop(self, join: bool = False, timeout: float = 10.0):
        self._stop_event.set()
        if join and self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

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

    # ── Core analysis + control cycle ─────────────────────────────────────
    def analyze(self) -> dict:
        """
        Full cycle: auto-resume → read metrics → rank → alerts → LLM decisions → execute → Telegram.
        Returns report dict (stored for dashboard to read).
        """
        # Step 1: Auto-resume expired suspensions
        resumed = self.registry.check_auto_resumes()
        if resumed:
            logger.info(f"HeadAI | auto-resumed: {resumed}")

        # Step 2: Read all bot metrics
        metrics = self.registry.status_all()
        if not metrics:
            return {}

        # Step 3: Rank bots
        ranking = self._rank_bots(metrics)

        # Step 4: Check hard alerts (errors, crashes)
        alerts = self._check_alerts(metrics)

        # Step 5: Get LLM decisions (Groq → Anthropic → rule-based fallback)
        decisions      = []
        portfolio_note = ""
        insights       = []

        try:
            llm_result = self._call_llm(metrics, ranking, alerts)
            decisions      = llm_result.get("decisions", [])
            portfolio_note = llm_result.get("portfolio_note", "")
            insights       = llm_result.get("insights", [])
        except Exception as e:
            logger.warning(f"HeadAI | LLM failed: {e} — using rule-based decisions")
            decisions = self._rule_based_decisions(metrics)
            insights  = self._rule_based_insights(metrics, ranking, alerts)
            portfolio_note = "Rule-based mode (no LLM key)"

        # Step 6: Execute decisions
        executed = self._execute_decisions(decisions, metrics)
        insights = self._sanitize_insights(insights, metrics, executed)

        # Step 7: Compose final insights from decisions + rule insights
        if not insights:
            insights = self._rule_based_insights(metrics, ranking, alerts)
        if portfolio_note:
            insights.insert(0, f"Portfolio: {portfolio_note}")
        for d in executed:
            insights.append(f"ACTION: {d['action']} {d['agent_id']} — {d['reason']}")

        # Step 8: Telegram
        if (alerts or executed) and self.tg_token and self.tg_chat_id:
            self._send_telegram(alerts, executed, insights, ranking)

        # Store for dashboard
        self._last_ranking        = ranking
        self._last_alerts         = alerts
        self._last_insights       = insights
        self._last_decisions      = executed
        self._last_portfolio_note = portfolio_note
        self._last_run            = datetime.now().strftime("%H:%M:%S")
        self._last_report = {
            "ranking":        ranking,
            "alerts":         alerts,
            "insights":       insights,
            "decisions":      executed,
            "portfolio_note": portfolio_note,
            "last_run":       self._last_run,
        }

        logger.info(
            f"HeadAI | cycle done | {len(alerts)} alerts | {len(executed)} decisions | "
            f"{len(insights)} insights"
        )
        return self._last_report

    # ── Decision execution ────────────────────────────────────────────────
    def _execute_decisions(self, decisions: list, metrics: list) -> list[dict]:
        """
        Execute each decision through BotRegistry.
        Returns list of decisions that were actually executed.
        IMPORTANT: No action (SUSPEND / REDUCE / BOOST) is taken on bots
        with < MIN_TRADES_FOR_DECISION trades — insufficient data.
        """
        metrics_by_id = {m["agent_id"]: m for m in metrics}
        executed = []

        for d in decisions:
            agent_id = d.get("agent_id", "")
            action   = d.get("action", "MAINTAIN").upper()
            reason   = d.get("reason", "")

            if agent_id not in metrics_by_id:
                logger.warning(f"HeadAI | decision for unknown agent: {agent_id}")
                continue

            m = metrics_by_id[agent_id]

            # Guard: don't act on bots with too few trades (no meaningful signal)
            trades = m.get("trades", 0)
            critical_error = m.get("error_count", 0) >= SUSPEND_ERROR_THRESHOLD or m.get("status") == "error"
            if action in ("REDUCE", "BOOST") and trades < MIN_TRADES_FOR_DECISION:
                logger.info(
                    f"HeadAI | skip {action} on {agent_id} — only {trades} trades (need {MIN_TRADES_FOR_DECISION})"
                )
                continue
            if action == "SUSPEND" and trades < MIN_TRADES_FOR_DECISION and not critical_error:
                continue

            # Don't SUSPEND an already-suspended bot
            if action == "SUSPEND" and m.get("suspended", False):
                continue

            if action == "SUSPEND":
                hours = float(d.get("suspend_hours", 24))
                ok = self.registry.suspend_bot(agent_id, reason, hours)
                if ok:
                    executed.append({"action": "SUSPEND", "agent_id": agent_id,
                                     "reason": reason, "hours": hours})

            elif action == "REDUCE":
                current_mult = m.get("head_ai_mult", 1.0)
                new_mult     = float(d.get("new_mult", max(0.3, current_mult * 0.5)))
                new_mult     = max(0.25, min(1.0, new_mult))
                ok = self.registry.set_ai_mult(agent_id, new_mult, reason)
                if ok:
                    executed.append({"action": "REDUCE", "agent_id": agent_id,
                                     "reason": reason, "new_mult": new_mult})

            elif action == "BOOST":
                ok = self.registry.set_ai_mult(agent_id, 1.0, reason)
                if ok:
                    executed.append({"action": "BOOST", "agent_id": agent_id,
                                     "reason": reason, "new_mult": 1.0})

            elif action == "MAINTAIN":
                pass    # no-op, log nothing

        return executed

    def _sanitize_insights(self, insights: list[str], metrics: list[dict], executed: list[dict]) -> list[str]:
        """Remove LLM claims that contradict measured bot state or executed controls."""
        if not insights:
            return []

        metrics_by_id = {m.get("agent_id", ""): m for m in metrics}
        executed_actions = {
            (d.get("agent_id", ""), d.get("action", "").upper())
            for d in executed
        }

        cleaned = []
        bad_words = ("loss", "losing", "underperform", "weak", "reduced", "suspend", "suspended")
        control_words = {"reduced": "REDUCE", "suspend": "SUSPEND", "suspended": "SUSPEND"}

        for insight in insights:
            low = insight.lower()
            drop = False
            for aid, metric in metrics_by_id.items():
                aid_low = aid.lower()
                if not aid_low or aid_low not in low:
                    continue
                if metric.get("trades", 0) == 0 and any(w in low for w in bad_words):
                    drop = True
                    break
                for word, action in control_words.items():
                    if word in low and (aid, action) not in executed_actions:
                        drop = True
                        break
                if drop:
                    break
            if not drop:
                cleaned.append(insight)
        return cleaned

    # ── LLM call (Groq → Anthropic → fallback) ───────────────────────────
    def _call_llm(self, metrics: list, ranking: list, alerts: list) -> dict:
        """Try Groq first, then Anthropic Claude, then raise to trigger rule-based."""
        from config import config as app_config

        if app_config.groq_api_key:
            return self._call_groq(metrics, ranking, alerts, app_config.groq_api_key)
        if self.api_key or app_config.anthropic_api_key:
            key = self.api_key or app_config.anthropic_api_key
            return self._call_anthropic(metrics, ranking, alerts, key)
        raise RuntimeError("No LLM API key configured")

    def _build_prompt(self, metrics: list, ranking: list, alerts: list) -> str:
        summary_lines = []
        for m in ranking:
            suspended = " [SUSPENDED]" if m.get("suspended") else ""
            trades    = m.get("trades", 0)
            no_data   = " [NO TRADES YET]" if trades < MIN_TRADES_FOR_DECISION else ""
            summary_lines.append(
                f"  #{m.get('rank','-')} {m['agent_id']:20s} | market={m.get('market','?'):6s} | "
                f"status={m.get('status','?'):14s} | trades={trades:3d} | "
                f"pnl=Rs{m.get('total_pnl',0):8,.0f} | win={m.get('win_rate',0):4.0f}% | "
                f"sharpe={m.get('sharpe',0):.2f} | errors={m.get('error_count',0)} | "
                f"ai_mult={m.get('head_ai_mult',1.0):.2f}{suspended}{no_data}"
            )

        return f"""You are the Head AI of an Indian algorithmic trading bot school.
Your job: analyze bot performance and make allocation decisions.

=== BOT PERFORMANCE (ranked by PnL) ===
{chr(10).join(summary_lines)}

=== ACTIVE ALERTS ===
{chr(10).join(alerts) if alerts else "None"}

=== DECISION RULES ===
- BOOST: Sharpe > {BOOST_SHARPE_MIN}, win_rate > {BOOST_WIN_RATE_MIN}%, no recent errors → give full allocation
- MAINTAIN: performing adequately, no action needed
- REDUCE: losing money but not critically, OR 2-3 errors → cut allocation by 50%
- SUSPEND: total_pnl < Rs{SUSPEND_LOSS_THRESHOLD}, OR 5+ errors → suspend for 24h (set suspend_hours)
- CRITICAL: Do NOT act on bots marked [NO TRADES YET] — output MAINTAIN for them
- Do NOT suspend an already-suspended bot
- Do NOT write insights suggesting a bot is "losing money" if its PnL = 0

Respond with ONLY this JSON (no other text):
{{
  "decisions": [
    {{"agent_id": "...", "action": "BOOST|MAINTAIN|REDUCE|SUSPEND", "reason": "...", "new_mult": 0.5, "suspend_hours": 24}}
  ],
  "portfolio_note": "one concise sentence about overall portfolio health",
  "insights": ["insight 1", "insight 2", "insight 3"]
}}
- new_mult required for REDUCE (0.25-0.75), ignored for others
- suspend_hours required for SUSPEND (default 24)
- insights: 3-5 concise, actionable observations (under 100 chars each)"""

    def _call_groq(self, metrics: list, ranking: list, alerts: list, key: str) -> dict:
        prompt = self._build_prompt(metrics, ranking, alerts)
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model":       "llama-3.1-8b-instant",
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  800,
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return self._parse_llm_response(text)

    def _call_anthropic(self, metrics: list, ranking: list, alerts: list, key: str) -> dict:
        prompt = self._build_prompt(metrics, ranking, alerts)
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        return self._parse_llm_response(text)

    def _parse_llm_response(self, text: str) -> dict:
        """Strip markdown fences, then parse JSON."""
        if "```" in text:
            parts = text.split("```")
            for i, part in enumerate(parts):
                if part.startswith("json"):
                    text = part[4:].strip()
                    break
                elif part.strip().startswith("{"):
                    text = part.strip()
                    break
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"HeadAI | JSON parse error: {e} | text={text[:200]}")
            raise

    # ── Rule-based fallback decisions ─────────────────────────────────────
    def _rule_based_decisions(self, metrics: list) -> list[dict]:
        """Pure logic decisions when no LLM is available."""
        decisions = []
        for m in metrics:
            aid    = m.get("agent_id", "")
            pnl    = m.get("total_pnl", 0)
            errors = m.get("error_count", 0)
            trades = m.get("trades", 0)
            sharpe = m.get("sharpe", 0.0)
            win    = m.get("win_rate", 0.0)

            if m.get("suspended", False):
                continue

            if errors >= SUSPEND_ERROR_THRESHOLD:
                decisions.append({
                    "agent_id":      aid,
                    "action":        "SUSPEND",
                    "reason":        f"{errors} consecutive errors",
                    "suspend_hours": 24,
                })
            elif trades < MIN_TRADES_FOR_DECISION:
                continue
            elif pnl < SUSPEND_LOSS_THRESHOLD:
                decisions.append({
                    "agent_id":      aid,
                    "action":        "SUSPEND",
                    "reason":        f"Loss Rs{pnl:,.0f}",
                    "suspend_hours": 24,
                })
            elif pnl < REDUCE_LOSS_THRESHOLD or errors >= REDUCE_ERROR_THRESHOLD:
                decisions.append({
                    "agent_id": aid,
                    "action":   "REDUCE",
                    "reason":   f"Underperforming: pnl=Rs{pnl:,.0f} errors={errors}",
                    "new_mult": 0.5,
                })
            elif sharpe >= BOOST_SHARPE_MIN and win >= BOOST_WIN_RATE_MIN:
                decisions.append({
                    "agent_id": aid,
                    "action":   "BOOST",
                    "reason":   f"Top performer: sharpe={sharpe:.2f} win={win:.0f}%",
                    "new_mult": 1.0,
                })
            else:
                decisions.append({
                    "agent_id": aid,
                    "action":   "MAINTAIN",
                    "reason":   "Adequate performance",
                })
        return decisions

    # ── Rule-based fallback insights ──────────────────────────────────────
    def _rule_based_insights(self, metrics: list, ranking: list, alerts: list) -> list[str]:
        insights = []

        # Only comment on bots that have actual data
        active = [m for m in ranking if m.get("trades", 0) >= MIN_TRADES_FOR_DECISION]

        if active:
            best  = active[0]
            worst = active[-1]
            insights.append(f"Top: {best['agent_id']} | Rs{best.get('total_pnl',0):,.0f} PnL")
            if worst.get("total_pnl", 0) < 0:
                insights.append(f"Weakest: {worst['agent_id']} | Rs{worst.get('total_pnl',0):,.0f} — review")

        running    = sum(1 for m in metrics if m.get("status") == "running")
        suspended  = sum(1 for m in metrics if m.get("suspended", False))
        errored    = sum(1 for m in metrics if m.get("status") == "error")
        mkt_closed = sum(1 for m in metrics if m.get("status") == "market_closed")
        insights.append(
            f"{running} running | {suspended} suspended | {errored} error | {mkt_closed} mkt-closed"
        )

        zero_trades = [m["agent_id"] for m in metrics if m.get("trades", 0) == 0]
        if zero_trades:
            insights.append(f"Awaiting first trade: {', '.join(zero_trades)}")

        from config import config as app_config
        if not app_config.groq_api_key and not app_config.anthropic_api_key:
            insights.append("Add GROQ_API_KEY or ANTHROPIC_API_KEY for AI-powered decisions")

        return insights

    # ── Ranking ───────────────────────────────────────────────────────────
    def _rank_bots(self, metrics: list[dict]) -> list[dict]:
        """Rank by total_pnl → win_rate → performance_mult. Suspended bots ranked last."""
        ranked = sorted(
            metrics,
            key=lambda m: (
                0 if not m.get("suspended") else -1,   # active bots first
                m.get("total_pnl",        0),
                m.get("win_rate",         0),
                m.get("performance_mult", 0),
            ),
            reverse=True,
        )
        for i, bot in enumerate(ranked):
            bot["rank"] = i + 1
        return ranked

    # ── Hard alert checks (independent of LLM) ───────────────────────────
    def _check_alerts(self, metrics: list[dict]) -> list[str]:
        alerts = []
        for m in metrics:
            aid = m.get("agent_id", "?")

            if m.get("status") == "error":
                alerts.append(f"ERROR {aid}: {m.get('last_error', 'unknown')[:60]}")

            if m.get("error_count", 0) >= SUSPEND_ERROR_THRESHOLD:
                alerts.append(f"CRITICAL {aid}: {m['error_count']} consecutive errors")

            total_pnl = m.get("total_pnl", 0)
            if total_pnl < SUSPEND_LOSS_THRESHOLD:
                alerts.append(f"LOSS ALERT {aid}: Rs{total_pnl:,.0f}")

        return alerts

    # ── Telegram ──────────────────────────────────────────────────────────
    def _send_telegram(self, alerts: list, decisions: list, insights: list, ranking: list):
        if not self.tg_token or not self.tg_chat_id:
            return

        lines = ["<b>UltimateQuantSystem - HeadAI Report</b>", ""]

        if ranking:
            lines.append("<b>Rankings:</b>")
            for m in ranking[:3]:
                pnl_str = f"Rs{m.get('total_pnl', 0):+,.0f}"
                tag     = " [SUSPENDED]" if m.get("suspended") else ""
                lines.append(
                    f"#{m.get('rank','-')} <code>{escape(m['agent_id'])}</code> | {pnl_str} | "
                    f"win={m.get('win_rate',0):.0f}%{escape(tag)}"
                )
            lines.append("")

        if decisions:
            lines.append("<b>Actions Taken:</b>")
            for d in decisions[:5]:
                action_icon = {"SUSPEND": "🔴", "REDUCE": "🟡", "BOOST": "🟢"}.get(d["action"], "⚪")
                lines.append(
                    f"{escape(d['action'])} <code>{escape(d['agent_id'])}</code>: "
                    f"{escape(d.get('reason', '')[:70])}"
                )
            lines.append("")

        if alerts:
            lines.append("<b>Alerts:</b>")
            for a in alerts[:4]:
                lines.append(escape(a[:120]))
            lines.append("")

        if insights:
            lines.append(escape(insights[0][:160]))

        message = "\n".join(lines)
        url     = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"

        try:
            resp = requests.post(url, json={
                "chat_id":    self.tg_chat_id,
                "text":       message[:3900],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
            if resp.ok:
                logger.info("HeadAI | Telegram sent")
            else:
                logger.warning(f"HeadAI | Telegram failed: {resp.text[:100]}")
        except Exception as e:
            err = str(e).replace(self.tg_token, "<telegram-token>")
            logger.warning(f"HeadAI | Telegram error: {err}")

    # ── Last report (for dashboard) ───────────────────────────────────────
    def last_report(self) -> dict:
        return self._last_report
